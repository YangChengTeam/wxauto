"""候选人会话名两条路（api2 §4）：上报 + 同步。

**先证坏路被堵**：缺字段不瞎报、状态不认识不报、dry-run 不报、后端挂了不影响回执；
再证好路能通：报出去的字段与契约一致、同步会自动翻页并落本机。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from wxclient.gateway import GatewayRunner, GatewaySettings, LocalFileTicketProvider
from wxclient.gateway.commands import CommandResult
from wxclient.gateway.contacts import ContactDirectory
from wxclient.gateway.sessions import GatewayStore


def directory(handler, token: str = "tok") -> ContactDirectory:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ContactDirectory("https://api.example.com", lambda: token, client=client)


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_contract_fields(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"matched": True, "worker_id": "w-1"})

        got = await directory(handler).report(
            number="13800138000", remark="张三·普工", status="requested", command_id="cmd-1",
        )
        self.assertEqual(got, {"matched": True, "worker_id": "w-1"})
        self.assertTrue(seen["url"].endswith("/recruitment-agent/client/contacts"))
        self.assertEqual(seen["auth"], "Bearer tok")
        body = seen["body"]
        self.assertEqual(body["number"], "13800138000")
        self.assertEqual(body["remark"], "张三·普工")
        self.assertEqual(body["status"], "requested")
        self.assertEqual(body["command_id"], "cmd-1")
        self.assertTrue(body["occurred_at"].endswith("Z"))  # §4.1 必须带时区

    async def test_no_match_is_not_an_error(self) -> None:
        """归不了位也照收——后端明说不打回，客户端也别当失败。"""
        got = await directory(lambda _r: httpx.Response(200, json={"matched": False, "worker_id": None})).report(
            number="13800138000", remark="张三", status="accepted",
        )
        self.assertEqual(got, {"matched": False, "worker_id": None})

    async def test_missing_remark_is_skipped_not_faked(self) -> None:
        """★ AI 端下的 add_friend 常常没给 remark：如实跳过，绝不编一个备注名报上去。"""
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(1)
            return httpx.Response(201, json={})

        self.assertIsNone(await directory(handler).report(number="138", remark="", status="requested"))
        self.assertIsNone(await directory(handler).report(number="", remark="张三", status="requested"))
        self.assertEqual(called, [])

    async def test_unknown_status_is_skipped(self) -> None:
        """★ 只认 requested/accepted 两档，别的一律不报（状态错了比没报更糟）。"""
        called = []
        self.assertIsNone(await directory(lambda _r: called.append(1) or httpx.Response(201, json={})).report(
            number="138", remark="张三", status="added",
        ))
        self.assertEqual(called, [])

    async def test_not_logged_in_sends_nothing(self) -> None:
        called = []
        d = directory(lambda _r: called.append(1) or httpx.Response(201, json={}), token="")
        self.assertIsNone(await d.report(number="138", remark="张三", status="requested"))
        self.assertEqual(called, [])

    async def test_backend_failure_is_swallowed(self) -> None:
        """★ 后端 500 / 网络炸了都只返回 None——微信那边动作已经做完了，不能因此报错。"""
        self.assertIsNone(await directory(lambda _r: httpx.Response(500, text="boom")).report(
            number="138", remark="张三", status="requested",
        ))

        def explode(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        self.assertIsNone(await directory(explode).report(number="138", remark="张三", status="requested"))

    async def test_remark_is_clamped_to_contract_length(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"matched": False})

        await directory(handler).report(number="138", remark="很" * 300, status="accepted")
        self.assertEqual(len(seen["body"]["remark"]), 120)


class PullTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_cursor_until_exhausted(self) -> None:
        pages = [
            {"contacts": [{"number": "1", "session_name": "甲"}], "next_cursor": "c2", "has_more": True},
            {"contacts": [{"number": "2", "session_name": "乙"}], "next_cursor": None, "has_more": False},
        ]
        seen_params: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=pages[len(seen_params) - 1])

        rows = await directory(handler).pull(updated_since="2026-08-18T00:00:00Z")
        self.assertEqual([r["number"] for r in rows], ["1", "2"])
        self.assertEqual(seen_params[0]["updated_since"], "2026-08-18T00:00:00Z")
        self.assertEqual(seen_params[0]["limit"], "200")
        self.assertEqual(seen_params[1]["cursor"], "c2")  # 游标原样回传，不解析

    async def test_partial_failure_keeps_what_it_got(self) -> None:
        """第二页挂了 → 保住第一页，不是全丢（同步是补课，能补多少算多少）。"""
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"contacts": [{"number": "1"}], "next_cursor": "c2", "has_more": True})
            return httpx.Response(503, text="unavailable")

        self.assertEqual(len(await directory(handler).pull()), 1)

    async def test_limit_is_clamped(self) -> None:
        seen: list[dict[str, str]] = []
        await directory(lambda r: seen.append(dict(r.url.params)) or httpx.Response(200, json={})).pull(limit=9999)
        self.assertEqual(seen[0]["limit"], "200")


class ContactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "gateway.db"
        self.store = GatewayStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_saved_contacts_survive_reopen(self) -> None:
        """★ 换机/重装的整个意义就在这：备注名活过进程，同一个人不会被起第二个名字。"""
        self.store.save_contacts("acct-1", [
            {"number": "13800138000", "session_name": "张三·普工", "contact_status": "accepted",
             "worker_id": "w-1", "updated_at": "2026-08-18T09:12:00.000Z"},
        ])
        reopened = GatewayStore(self.path)
        self.assertEqual(reopened.contact_session_name("acct-1", "13800138000"), "张三·普工")
        self.assertEqual(reopened.contacts_synced_until("acct-1"), "2026-08-18T09:12:00.000Z")

    def test_same_number_updates_in_place(self) -> None:
        rows = [{"number": "138", "session_name": "旧名", "updated_at": "2026-08-18T01:00:00Z"}]
        self.store.save_contacts("acct-1", rows)
        self.store.save_contacts("acct-1", [{"number": "138", "session_name": "新名",
                                             "updated_at": "2026-08-18T02:00:00Z"}])
        self.assertEqual(self.store.contact_session_name("acct-1", "138"), "新名")
        self.assertEqual(self.store.contacts_synced_until("acct-1"), "2026-08-18T02:00:00Z")

    def test_rows_without_number_are_dropped(self) -> None:
        self.assertEqual(self.store.save_contacts("acct-1", [{"session_name": "没号码"}]), 0)

    def test_scoped_per_account(self) -> None:
        self.store.save_contacts("acct-1", [{"number": "138", "session_name": "甲",
                                             "updated_at": "2026-08-18T01:00:00Z"}])
        self.assertEqual(self.store.contact_session_name("acct-2", "138"), "")
        self.assertEqual(self.store.contacts_synced_until("acct-2"), "")


class FakeDirectory:
    """假的会话名通道：只记调用，不发请求。"""

    configured = True

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.reported: list[dict[str, Any]] = []
        self.pulled: list[str] = []
        self._rows = rows or []

    async def report(self, **kwargs: Any) -> dict[str, Any]:
        self.reported.append(kwargs)
        return {"matched": True, "worker_id": "w-1"}

    async def pull(self, *, updated_since: str = "", limit: int = 200) -> list[dict[str, Any]]:
        self.pulled.append(updated_since)
        return self._rows


class SilentSource:
    async def fetch(self, account_id: str) -> list:
        return []


class SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("这条测试不该往微信发东西")


class RunnerWiringTests(unittest.IsolatedAsyncioTestCase):
    """接线本身：加好友链走到一步就报一次，dry-run 一声不吭。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({"acct-1": "tkt"}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, directory: FakeDirectory) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(ai_ws_url="ws://127.0.0.1:1/ws", local_ticket_file=self.tickets),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=SilentSource(),
            sink=SilentSink(),
            accounts=["acct-1"],
            contacts=directory,
        )

    async def test_friend_state_triggers_one_report_with_original_args(self) -> None:
        d = FakeDirectory()
        command = {"command_id": "cmd-1", "op": "add_friend",
                   "args": {"number": "13800138000", "remark": "张三·普工"}}
        result = CommandResult("cmd-1", "done", result={"friend_state": "requested"})
        await self._runner(d)._report_contact(command, result)
        self.assertEqual(d.reported, [{"number": "13800138000", "remark": "张三·普工",
                                       "status": "requested", "command_id": "cmd-1"}])

    async def test_dry_run_reports_nothing(self) -> None:
        """★ 没真动微信就没有 friend_state → 一条都不该报，否则后端以为好友已经加上了。"""
        d = FakeDirectory()
        command = {"command_id": "cmd-1", "op": "add_friend", "args": {"number": "138", "remark": "张三"}}
        await self._runner(d)._report_contact(command, CommandResult("cmd-1", "done", result={}))
        await self._runner(d)._report_contact(command, None)
        self.assertEqual(d.reported, [])

    async def test_sync_is_incremental_after_first_run(self) -> None:
        rows = [{"number": "138", "session_name": "张三·普工", "contact_status": "accepted",
                 "updated_at": "2026-08-18T09:12:00.000Z"}]
        d = FakeDirectory(rows)
        runner = self._runner(d)
        await runner._sync_contacts()
        self.assertEqual(self.store.contact_session_name("acct-1", "138"), "张三·普工")
        await runner._sync_contacts()
        # 第一次没水位（全量），第二次带上本机已知的最新时间
        self.assertEqual(d.pulled, ["", "2026-08-18T09:12:00.000Z"])


if __name__ == "__main__":
    unittest.main()
