"""候选人会话名两条路（api2 §4）：上报 + 同步。

**先证坏路被堵**：缺字段不瞎报、状态不认识不报、dry-run 不报、后端挂了不影响回执；
再证好路能通：报出去的字段与契约一致、同步会自动翻页并落本机。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from wxclient.gateway import GatewayRunner, GatewaySettings, LocalFileTicketProvider
from wxclient.gateway.commands import CommandResult
from wxclient.gateway.contacts import ContactDirectory
from wxclient.gateway.runner import ACCEPTED_REPORT_DELAY_SECONDS
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

    fail_next = False
    """下一次 `report` 返回 None（后端不可达时的真实形状）。用来验「报不出去不销账」。"""

    async def report(self, **kwargs: Any) -> dict[str, Any] | None:
        self.reported.append(kwargs)
        if self.fail_next:
            self.fail_next = False
            return None
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
        self.now = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, directory: FakeDirectory) -> GatewayRunner:
        # 时钟可拨：好友通过的上报要等半小时，测试不可能真等（见 `self.now`）。
        return GatewayRunner(
            settings=GatewaySettings(ai_ws_url="ws://127.0.0.1:1/ws", local_ticket_file=self.tickets),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=SilentSource(),
            sink=SilentSink(),
            accounts=["acct-1"],
            contacts=directory,
            clock=lambda: self.now,
        )

    def _wait_out_the_delay(self) -> None:
        """把表拨过那半小时。"""
        self.now = self.now + timedelta(seconds=ACCEPTED_REPORT_DELAY_SECONDS + 1)

    async def test_friend_state_triggers_one_report_with_original_args(self) -> None:
        d = FakeDirectory()
        command = {"command_id": "cmd-1", "op": "add_friend",
                   "args": {"number": "13800138000", "remark": "张三·普工"}}
        result = CommandResult("cmd-1", "done", result={"friend_state": "requested"})
        await self._runner(d)._report_contact(command, result)
        self.assertEqual(d.reported, [{"number": "13800138000", "remark": "张三·普工",
                                       "status": "requested", "profile_name": "",
                                       "command_id": "cmd-1"}])

    async def test_the_nickname_read_off_the_card_rides_along(self) -> None:
        """★ 候选人档案里的 `wechatName` 只有这一条来源（api2 §4.1）。

        名片那一趟本来就读到了昵称，回执里也带回来了——原来在这里被整个丢掉，
        于是那个字段永远是空的。
        """
        d = FakeDirectory()
        await self._runner(d)._report_contact(
            {"command_id": "cmd-1", "op": "add_friend",
             "args": {"number": "13800138000", "remark": "张三·普工"}},
            CommandResult("cmd-1", "done",
                          result={"friend_state": "requested", "profile_name": "阿三"}),
        )
        self.assertEqual(d.reported[0]["profile_name"], "阿三")

    async def test_an_existing_friend_is_reported_accepted_under_his_real_session_name(self) -> None:
        """★ 这个号早就是好友（2026-08-26 用户场景）：

        ① 状态是 `accepted` 而不是 `requested`——没有人要去"通过"，报成待通过这个人就永远卡着；
        ② 会话名报**微信里现在显示的那个**，不是我们提议的备注：那一轮压根没设过备注，
           报 `张三·叉车工` 等于给后端一个微信里不存在的名字，下一条指令就寻不到窗口。
        """
        d = FakeDirectory()
        await self._runner(d)._report_contact(
            {"command_id": "cmd-2", "op": "add_friend",
             "args": {"number": "13800138000", "remark": "张三·叉车工"}},
            CommandResult("cmd-2", "done", result={
                "friend_state": "accepted", "already_friend": True,
                "session_name": "张三·仓库工", "profile_name": "阿三",
            }),
        )
        self.assertEqual(d.reported, [{"number": "13800138000", "remark": "张三·仓库工",
                                       "status": "accepted", "profile_name": "阿三",
                                       "command_id": "cmd-2"}])

    async def test_the_remark_the_client_filled_in_is_the_one_reported(self) -> None:
        """★ 2026-08-27 真机：AI 端下发的指令**没带 remark**，本机按后端已知的会话名补上了，
        微信里备注也设对了 —— 可上报读的是原指令的 `args`，于是判成「缺 remark」整条跳过
        （日志：`会话名上报被跳过：缺 number 或 remark，has_number=true，has_remark=false`）。

        后果不是少一条日志：后端从此不知道这个人在微信里叫什么，
        名单是按会话名派生的，他也就永远进不了名单、说话没人读。
        补出来的名字只写在**回执**里（`session_name`），所以上报必须以回执为准。
        """
        d = FakeDirectory()
        await self._runner(d)._report_contact(
            {"command_id": "cmd-9", "op": "add_friend",
             "args": {"number": "13800138000"}},          # ← 指令里一个 remark 都没有
            CommandResult("cmd-9", "done", result={
                "friend_state": "requested", "session_name": "候选人-张建国",
            }),
        )
        self.assertEqual(d.reported, [{"number": "13800138000", "remark": "候选人-张建国",
                                       "status": "requested", "profile_name": "",
                                       "command_id": "cmd-9"}])

    async def test_dry_run_reports_nothing(self) -> None:
        """★ 没真动微信就没有 friend_state → 一条都不该报，否则后端以为好友已经加上了。"""
        d = FakeDirectory()
        command = {"command_id": "cmd-1", "op": "add_friend", "args": {"number": "138", "remark": "张三"}}
        await self._runner(d)._report_contact(command, CommandResult("cmd-1", "done", result={}))
        await self._runner(d)._report_contact(command, None)
        self.assertEqual(d.reported, [])

    async def test_their_first_message_means_they_accepted(self) -> None:
        """★ 对方通过了我们的申请 —— 收到他第一条消息就认出来（api2 §4.1）。

        用「来消息」当信号是因为它**免费且确定**：私聊收得到就已经是好友。
        另一条路是去查好友列表，那要真动微信、占节拍配额，为一个白捡的事实不值当。

        ★ 认出来 ≠ 当场报：报要等半小时（用户 2026-08-29），见下面那条。
        """
        d = FakeDirectory()
        runner = self._runner(d)
        await runner._report_contact(
            {"command_id": "cmd-1", "op": "add_friend",
             "args": {"number": "13800138000", "remark": "张三·普工"}},
            CommandResult("cmd-1", "done", result={"friend_state": "requested"}),
        )
        d.reported.clear()

        await runner._note_friend_accepted("acct-1", "张三·普工")
        self.assertEqual(d.reported, [], "刚通过就报 = 人家刚点完同意系统马上动起来")
        self.assertEqual(self.store.pending_accepted_count("acct-1"), 1)

        self._wait_out_the_delay()
        await runner.tick()

        self.assertEqual(len(d.reported), 1)
        sent = d.reported[0]
        self.assertEqual(sent["number"], "13800138000")
        self.assertEqual(sent["remark"], "张三·普工")
        self.assertEqual(sent["status"], "accepted")
        self.assertEqual(self.store.pending_accepted_count("acct-1"), 0)

    async def test_the_report_says_when_it_actually_happened(self) -> None:
        """★ `occurred_at` 是**真正通过的那一刻**，不是补报的时刻。

        延迟是我们这边有意等的，不该让后端以为人家是半小时后才点的同意。
        """
        d = FakeDirectory()
        runner = self._runner(d)
        self.store.note_friend_request("acct-1", "13800138000", "张三·普工")
        accepted_at = self.now

        await runner._note_friend_accepted("acct-1", "张三·普工")
        self._wait_out_the_delay()
        await runner.tick()

        self.assertEqual(d.reported[0]["occurred_at"], accepted_at.isoformat())

    async def test_talking_a_lot_does_not_push_the_report_further_out(self) -> None:
        """★ 重复认出不刷新计时——否则话多的人可以让这条上报永远发不出去。"""
        d = FakeDirectory()
        runner = self._runner(d)
        self.store.note_friend_request("acct-1", "13800138000", "张三·普工")

        await runner._note_friend_accepted("acct-1", "张三·普工")
        for _ in range(5):                      # 他一直在说话
            self.now = self.now + timedelta(minutes=5)
            await runner._note_friend_accepted("acct-1", "张三·普工")
        self.assertEqual(self.store.pending_accepted_count("acct-1"), 1)

        await runner.tick()                     # 从第一次认出算已经过了 25 分钟：还不到点
        self.assertEqual(d.reported, [])
        self.now = self.now + timedelta(minutes=6)
        await runner.tick()
        self.assertEqual(len(d.reported), 1)

    async def test_a_failed_report_stays_in_the_queue(self) -> None:
        """报不出去就留着下一轮——**不丢**。后端下线半小时不该让这个人凭空消失。"""
        d = FakeDirectory()
        d.fail_next = True
        runner = self._runner(d)
        self.store.note_friend_request("acct-1", "13800138000", "张三·普工")

        await runner._note_friend_accepted("acct-1", "张三·普工")
        self._wait_out_the_delay()
        await runner.tick()
        self.assertEqual(self.store.pending_accepted_count("acct-1"), 1, "报失败了不能销账")

        await runner.tick()
        self.assertEqual(len(d.reported), 2, "下一趟要再试一次")
        self.assertEqual(self.store.pending_accepted_count("acct-1"), 0)

    async def test_acceptance_is_reported_only_once(self) -> None:
        """报完就落地。他后面再说十句话，也不该再报十遍。"""
        d = FakeDirectory()
        runner = self._runner(d)
        self.store.note_friend_request("acct-1", "13800138000", "张三·普工")

        await runner._note_friend_accepted("acct-1", "张三·普工")
        await runner._note_friend_accepted("acct-1", "张三·普工")
        self._wait_out_the_delay()
        await runner.tick()
        await runner.tick()

        self.assertEqual(len(d.reported), 1)

    async def test_a_stranger_message_reports_nothing(self) -> None:
        """没给他发过申请的人来消息 —— 与好友状态无关，一条都不报。"""
        d = FakeDirectory()
        await self._runner(d)._note_friend_accepted("acct-1", "查无此人")
        self.assertEqual(d.reported, [])

    async def test_accepting_their_application_reports_the_ones_we_can_place(self) -> None:
        """我方通过对方申请：只拿得到显示名，号从本机映射里补；补不出来就不报。

        ★ 不许编号：后端拿 `number` 归位候选人，编的会归到别人头上。
        """
        d = FakeDirectory()
        self.store.save_contacts("acct-1", [
            {"number": "13800138000", "session_name": "张三·普工",
             "updated_at": "2026-08-18T09:00:00.000Z"},
        ])
        await self._runner(d)._report_contact(
            {"command_id": "cmd-9", "op": "accept_new_friends", "args": {"limit": 5}},
            CommandResult("cmd-9", "done",
                          result={"items": ["张三·普工", "不认识的人"], "friend_state": "accepted"}),
        )
        # 我方通过对方申请也是「好友申请通过」，同样等半小时再报。
        self.assertEqual(d.reported, [])
        self._wait_out_the_delay()
        await self._runner(d).tick()
        self.assertEqual(
            [(r["number"], r["remark"], r["status"], r["command_id"]) for r in d.reported],
            [("13800138000", "张三·普工", "accepted", "cmd-9")],
        )

    async def test_local_rows_do_not_move_the_sync_watermark(self) -> None:
        """★ 水位只认服务端给的时间。

        本机自己记的「刚发出的申请」没有服务端时间；它若被算进水位（老实现取
        `MAX(updated_at)`、缺了还拿本机时钟顶替），比它早的行以后永远同步不回来。
        """
        d = FakeDirectory([{"number": "138", "session_name": "张三",
                            "updated_at": "2026-08-18T09:12:00.000Z"}])
        runner = self._runner(d)
        await runner._sync_contacts_once()
        self.store.note_friend_request("acct-1", "13900139000", "李四·叉车")
        self.store.save_contacts("acct-1", [{"number": "137", "session_name": "王五"}])  # 后端没给时间

        await runner._sync_contacts_once()

        self.assertEqual(d.pulled, ["", "2026-08-18T09:12:00.000Z"])

    async def test_sync_is_incremental_after_first_run(self) -> None:
        rows = [{"number": "138", "session_name": "张三·普工", "contact_status": "accepted",
                 "updated_at": "2026-08-18T09:12:00.000Z"}]
        d = FakeDirectory(rows)
        runner = self._runner(d)
        await runner._sync_contacts_once()
        self.assertEqual(self.store.contact_session_name("acct-1", "138"), "张三·普工")
        await runner._sync_contacts_once()
        # 第一次没水位（全量），第二次带上本机已知的最新时间
        self.assertEqual(d.pulled, ["", "2026-08-18T09:12:00.000Z"])


if __name__ == "__main__":
    unittest.main()
