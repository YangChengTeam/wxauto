"""名单上的「清除」：删 AI 端这个联系人的会话与记忆，连同本机记下的聊天记录（用户 2026-10-13）。

契约：AI 服务仓 spec 147 ``handoff-gateway.md``。钉住的几条：
- 请求长什么样：``DELETE /v1/sessions/{id}?include_memory=true&peer=<会话名>`` + 现换的票；
- **先删 AI 端、成功了才动本机**——AI 端没删成，本机不许先把 session_id 忘了；
- 本机只清会话映射和聊天记录，**已读去重和游标不动**（动了旧消息会被再回一遍），名单不动；
- AI 端推来的 ``wechat.peer_erased`` 只认点到的那个会话，迟到的旧通知不抹新会话；
- AI 端回 ``not_found``（会话被删/被回收）要走「丢映射、开新会话」那条路，不许原地死磕。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    LocalFileTicketProvider,
    Watchlist,
)
from wxclient.gateway.agent_files import TICKET_HEADER
from wxclient.gateway.agent_sessions import (
    PEER_ERASED_EVENT,
    AgentSessionEraser,
    EraseError,
)
from wxclient.gateway.ticket import TicketError
from wxclient.gateway.wsclient import AgentWsClient, SessionGone, Unauthorized

T0 = datetime(2026, 10, 13, 14, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"
PEER = "王建国-仓库工"
RECEIPT = {"sessions": 1, "codex_threads": 1, "codex_rollout_files": 0, "workspaces": 1,
           "direct_messages": 0, "memories": 2, "delivery_intents": 1, "result_notices": 0}


class _Tickets:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def mint(self, wechat_account_id: str) -> str:
        if self.fail:
            raise TicketError("这台机器还没登录，换不到票")
        return "ticket-1"


def _eraser(handler, tickets: _Tickets | None = None) -> tuple[AgentSessionEraser, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    eraser = AgentSessionEraser(
        base_url="https://worker.example.cn/agent-ws", ticket_provider=tickets or _Tickets(), client=client
    )
    return eraser, seen


class EraserRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_sends_the_contracted_delete(self) -> None:
        eraser, seen = _eraser(lambda _r: httpx.Response(202, json={"status": "accepted", "erasure": RECEIPT}))

        receipt = await eraser.erase(ACCOUNT, PEER, "sess/1")

        self.assertEqual(receipt, RECEIPT)
        request = seen[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(request.url.raw_path.split(b"?")[0], b"/agent-ws/v1/sessions/sess%2F1", "★ session_id 要整段编码")
        self.assertEqual(request.url.params["include_memory"], "true", "★ 不带就只删会话、记忆留着")
        self.assertEqual(request.url.params["peer"], PEER)
        self.assertEqual(request.headers[TICKET_HEADER], "ticket-1")

    async def test_a_session_already_gone_counts_as_erased(self) -> None:
        eraser, _ = _eraser(lambda _r: httpx.Response(404, json={"error": {"code": "not_found"}}))
        self.assertEqual(await eraser.erase(ACCOUNT, PEER, "s1"), {})

    async def test_refusals_and_failures_are_raised_in_words(self) -> None:
        for status, words in ((403, "不归当前登录的账号"), (401, "重新登录"), (500, "500")):
            eraser, _ = _eraser(lambda _r, s=status: httpx.Response(s, json={"error": {"code": "x"}}))
            with self.subTest(status=status), self.assertRaises(EraseError) as caught:
                await eraser.erase(ACCOUNT, PEER, "s1")
            self.assertIn(words, str(caught.exception))

    async def test_no_ticket_means_no_request(self) -> None:
        eraser, seen = _eraser(lambda _r: httpx.Response(202, json={}), tickets=_Tickets(fail=True))
        with self.assertRaises(EraseError):
            await eraser.erase(ACCOUNT, PEER, "s1")
        self.assertEqual(seen, [])

    async def test_a_network_error_does_not_leak_details(self) -> None:
        def boom(_r: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("ticket-1 at https://worker.example.cn")

        eraser, _ = _eraser(boom)
        with self.assertRaises(EraseError) as caught:
            await eraser.erase(ACCOUNT, PEER, "s1")
        self.assertNotIn("ticket-1", str(caught.exception), "★ 异常原文可能带票据片段")


class _FakeEraser:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def erase(self, account_id: str, peer_id: str, session_id: str) -> dict[str, Any]:
        self.calls.append((account_id, peer_id, session_id))
        if self.fail is not None:
            raise self.fail
        return dict(RECEIPT)


class SilentSource:
    async def fetch(self, account_id: str) -> list:
        return []


class SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("清除不该往微信发任何东西")


class _RunnerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.watchlist = Watchlist()
        self.watchlist.apply_backend({"only": [PEER, "李四"]})
        self.store.put_session(ACCOUNT, PEER, "sess-old")
        self.store.put_session(ACCOUNT, "李四", "sess-lisi")
        self.store.record_messages(ACCOUNT, PEER, [
            {"msg_id": "m1", "at": "2026-10-13T05:00:00+00:00", "text": "我手机 138…"},
            {"msg_id": "m2", "at": "2026-10-13T05:01:00+00:00", "text": "好的", "from_self": True},
        ])
        self.store.record_messages(ACCOUNT, "李四", [{"msg_id": "n1", "at": "2026-10-13T05:00:00+00:00", "text": "在"}])
        self.store.mark_seen(ACCOUNT, PEER, ["m1", "m2"])

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, eraser: Any = None) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(
                ai_ws_url="ws://127.0.0.1:9/v1/ws",
                local_ticket_file=self.tickets,
                turn_timeout_seconds=10.0,
                active_hours="00:00-23:59",
            ),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=SilentSource(),
            sink=SilentSink(),
            accounts=[ACCOUNT],
            clock=lambda: T0,
            watchlist=self.watchlist,
            session_eraser=eraser,
        )


class ErasePeerTests(_RunnerCase):
    async def test_it_erases_the_ai_side_then_forgets_locally(self) -> None:
        eraser = _FakeEraser()
        result = await self._runner(eraser).erase_peer(PEER)

        self.assertEqual(eraser.calls, [(ACCOUNT, PEER, "sess-old")])
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["messages"], 2)
        self.assertEqual(result["erasure"]["memories"], 2)
        self.assertIsNone(self.store.get_session(ACCOUNT, PEER), "★ 下一条消息要开新会话")
        self.assertEqual(self.store.recent_messages(ACCOUNT, PEER), [], "★ 留着会被当 history 讲回去")

    async def test_it_keeps_what_is_not_the_conversation(self) -> None:
        await self._runner(_FakeEraser()).erase_peer(PEER)

        self.assertEqual(self.store.filter_unseen(ACCOUNT, PEER, ["m1", "m2"]), [],
                         "★ 已读去重不许动：动了微信里的旧消息会被再回一遍")
        self.assertEqual(sorted(self.watchlist.watched()), sorted([PEER, "李四"]), "★ 清除不是移出")
        self.assertEqual(self.store.get_session(ACCOUNT, "李四"), "sess-lisi", "★ 只动这一个人")
        self.assertEqual(len(self.store.recent_messages(ACCOUNT, "李四")), 1)

    async def test_an_ai_side_failure_leaves_everything_local_in_place(self) -> None:
        eraser = _FakeEraser(fail=EraseError("连不上 AI 端（ConnectError）"))
        with self.assertRaises(EraseError):
            await self._runner(eraser).erase_peer(PEER)

        self.assertEqual(self.store.get_session(ACCOUNT, PEER), "sess-old",
                         "★ 本机先忘了 session_id，AI 端那段就再也找不回来删了")
        self.assertEqual(len(self.store.recent_messages(ACCOUNT, PEER)), 2)

    async def test_without_a_session_it_only_clears_local_records(self) -> None:
        self.store.drop_session(ACCOUNT, PEER)
        eraser = _FakeEraser()
        result = await self._runner(eraser).erase_peer(PEER)

        self.assertEqual(eraser.calls, [], "没有 session_id 就没有可删的 AI 端会话")
        self.assertEqual(result["sessions"], 0)
        self.assertEqual(result["messages"], 2)


class PeerErasedPushTests(_RunnerCase):
    async def test_the_push_drops_the_named_session(self) -> None:
        runner = self._runner()
        runner._on_push(ACCOUNT, PEER_ERASED_EVENT, {"peer": PEER, "session_ids": ["sess-old"], "at": "x"})

        self.assertIsNone(self.store.get_session(ACCOUNT, PEER))
        self.assertEqual(self.store.recent_messages(ACCOUNT, PEER), [])
        self.assertEqual(self.store.get_session(ACCOUNT, "李四"), "sess-lisi")

    async def test_a_stale_push_does_not_wipe_a_newer_session(self) -> None:
        self.store.put_session(ACCOUNT, PEER, "sess-new")
        runner = self._runner()
        runner._on_push(ACCOUNT, PEER_ERASED_EVENT, {"peer": PEER, "session_ids": ["sess-old"]})

        self.assertEqual(self.store.get_session(ACCOUNT, PEER), "sess-new")
        self.assertEqual(len(self.store.recent_messages(ACCOUNT, PEER)), 2)


class SessionGoneTests(unittest.TestCase):
    def test_not_found_is_handled_like_a_lost_session(self) -> None:
        error = AgentWsClient._error_of({"code": "not_found", "message": "session not found"})
        self.assertIsInstance(error, SessionGone)
        self.assertIsInstance(error, Unauthorized, "★ 各调用点「丢映射、开新会话」那条路接的是 Unauthorized")


if __name__ == "__main__":
    unittest.main()
