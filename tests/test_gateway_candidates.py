"""候选人资料 / AI 状态回写（api2 §9）——闭环最后一段。

**先证坏路被堵**：只走流程不真发时一律拒、状态词不认识不发、必填原因缺了不发、
后端挂了不影响微信侧；再证好路能通：路径/鉴权/请求体与契约一致。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from wxclient.gateway.candidates import CandidateDirectory, CandidateUpdateError
from wxclient.gateway.commands import CommandExecutor
from wxclient.gateway.guard import OutboundGuard
from wxclient.gateway.sessions import GatewayStore


def directory(handler, token: str = "tok", base: str = "https://api.example.com") -> CandidateDirectory:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CandidateDirectory(base, lambda: token, client=client)


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_contract_shape(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"aiRecruitment": {"status": "ai_managed", "version": 5}})

        got = await directory(handler).update(
            "cand-1",
            profile={"wechatAdded": True},
            ai_status={"status": "ai_managed"},
        )
        self.assertEqual(got, {"updated": True, "version": 5})
        self.assertEqual(seen["method"], "PATCH")
        self.assertTrue(seen["url"].endswith("/recruitment-agent/client/candidates/cand-1"))
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["body"], {"profile": {"wechatAdded": True},
                                        "aiRecruitment": {"status": "ai_managed"}})

    async def test_unknown_status_never_leaves_the_machine(self) -> None:
        """状态词不认识 = 上游写错了。当场拒，别换一趟 400 回来。"""
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(str(request.url))
            return httpx.Response(200, json={})

        with self.assertRaises(CandidateUpdateError):
            await directory(handler).update("cand-1", ai_status={"status": "托管中"})
        self.assertEqual(called, [])

    async def test_reason_required_statuses(self) -> None:
        """§9.2：human_takeover / blocked 必须带原因。"""
        for status in ("human_takeover", "blocked"):
            with self.assertRaises(CandidateUpdateError):
                await directory(lambda _r: httpx.Response(200, json={})).update(
                    "cand-1", ai_status={"status": status}
                )

    async def test_profile_whitelist(self) -> None:
        """`profile` 只写我们该写的那几样；别的字段由人在网页上改。"""
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        await directory(handler).update(
            "cand-1", profile={"wechatAdded": True, "phone": "13800138000", "blacklisted": True}
        )
        self.assertEqual(seen["body"], {"profile": {"wechatAdded": True}})

    async def test_missing_candidate_id_is_rejected(self) -> None:
        with self.assertRaises(CandidateUpdateError):
            await directory(lambda _r: httpx.Response(200, json={})).update("", ai_status={"status": "ai_managed"})

    async def test_nothing_to_change_is_rejected(self) -> None:
        with self.assertRaises(CandidateUpdateError):
            await directory(lambda _r: httpx.Response(200, json={})).update("cand-1")

    async def test_backend_down_does_not_raise(self) -> None:
        """网络抖动不该炸到微信侧——如实回「没写成」，让上游决定重不重试。"""
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        got = await directory(handler).update("cand-1", ai_status={"status": "ai_managed"})
        self.assertEqual(got["updated"], False)

    async def test_version_conflict_is_not_a_success(self) -> None:
        got = await directory(lambda _r: httpx.Response(409, json={"code": "AI_STATUS_VERSION_CONFLICT"})).update(
            "cand-1", ai_status={"status": "ai_managed"}
        )
        self.assertEqual(got["updated"], False)

    async def test_not_logged_in_is_quiet(self) -> None:
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(str(request.url))
            return httpx.Response(200, json={})

        got = await directory(handler, token="").update("cand-1", ai_status={"status": "ai_managed"})
        self.assertEqual(got["updated"], False)
        self.assertEqual(called, [])


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    """`op: update_candidate` 在指令执行器里的两条闸。"""

    def _executor(self, *, allow_real: bool, candidates) -> CommandExecutor:
        tmp = tempfile.mkdtemp()
        store = GatewayStore(Path(tmp) / "gw.db")
        return CommandExecutor(
            runtime=None,
            store=store,
            guard=OutboundGuard(store),
            account_id="acc",
            allow_real_actions=allow_real,
            candidates=candidates,
        )

    async def test_dry_run_never_writes_business_data(self) -> None:
        """★ 只走流程那台机器一条微信都没发。把人标成「AI 托管中」= 进度比现实超前一格。"""
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(str(request.url))
            return httpx.Response(200, json={})

        result = await self._executor(allow_real=False, candidates=directory(handler)).execute(
            {"command_id": "c1", "op": "update_candidate",
             "args": {"candidate_id": "cand-1", "ai_status": {"status": "ai_managed"}}}
        )
        self.assertEqual(result.status, "rejected")
        self.assertEqual(called, [])

    async def test_writes_when_real(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"aiRecruitment": {"status": "ai_managed", "version": 2}})

        result = await self._executor(allow_real=True, candidates=directory(handler)).execute(
            {"command_id": "c2", "op": "update_candidate",
             "args": {"candidate_id": "cand-1", "ai_status": {"status": "ai_managed"}}}
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(result.result, {"updated": True, "version": 2})

    async def test_bad_shape_is_rejected_not_failed(self) -> None:
        """形状不对重试多少次都一样 → rejected（failed 会被上游当成「等会儿再试」）。"""
        result = await self._executor(
            allow_real=True, candidates=directory(lambda _r: httpx.Response(200, json={}))
        ).execute({"command_id": "c3", "op": "update_candidate", "args": {"candidate_id": ""}})
        self.assertEqual(result.status, "rejected")

    async def test_no_business_url_is_rejected(self) -> None:
        result = await self._executor(
            allow_real=True,
            candidates=directory(lambda _r: httpx.Response(200, json={}), base=""),
        ).execute({"command_id": "c4", "op": "update_candidate",
                   "args": {"candidate_id": "cand-1", "ai_status": {"status": "ai_managed"}}})
        self.assertEqual(result.status, "rejected")


class InboundActionTests(unittest.IsolatedAsyncioTestCase):
    """入站对话那条路：卡上的 `set_ai_status` → 本机反查候选人 → PATCH。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gw.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sender(self, candidates, sink=None):
        from wxclient.gateway.outbound import OutboundSender

        class FakeSink:
            def __init__(self) -> None:
                self.sent: list[tuple[str, str, str]] = []

            async def send(self, account_id: str, peer_id: str, text: str) -> str:
                self.sent.append((account_id, peer_id, text))
                return f"t{len(self.sent)}"

        return OutboundSender(
            sink=sink or FakeSink(),
            guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
            store=self.store,
            candidates=candidates,
        )

    def _card(self, actions):
        return {"type": "info", "body": {"kind": "wechat_reply", "reply_segments": [],
                                         "no_reply_reason": "已转人工", "intent": "consult",
                                         "funnel_stage": "qa", "actions": actions}}

    def _seed(self, session_name: str, worker_id: str | None) -> None:
        self.store.save_contacts("acc", [{
            "number": "13800138000", "session_name": session_name,
            "contact_status": "accepted", "worker_id": worker_id, "updated_at": "2026-08-19T00:00:00Z",
        }])

    async def test_resolves_candidate_locally_and_writes(self) -> None:
        """★ 候选人 ID 由**本机**按会话名反查——AI 端不知道对面是谁，也不该知道。"""
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={})

        self._seed("张三·普工", "w-77")
        await self._sender(directory(handler)).deliver(
            "acc", "张三·普工",
            self._card([{"type": "set_ai_status", "reason": "要真人确认社保",
                         "payload": {"status": "human_takeover"}}]),
        )
        self.assertTrue(seen["url"].endswith("/candidates/w-77"))
        self.assertEqual(seen["body"], {"aiRecruitment": {"status": "human_takeover",
                                                          "reason": "要真人确认社保"}})

    async def test_unknown_peer_is_skipped_not_guessed(self) -> None:
        """反查不到 = 这个人还没上报过会话名。如实跳过，绝不拿别人的 ID 顶上。"""
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(str(request.url))
            return httpx.Response(200, json={})

        self._seed("张三·普工", None)
        await self._sender(directory(handler)).deliver(
            "acc", "李四", self._card([{"type": "set_ai_status", "payload": {"status": "ai_managed"}}]),
        )
        self.assertEqual(called, [])

    async def test_no_status_action_no_request(self) -> None:
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(str(request.url))
            return httpx.Response(200, json={})

        self._seed("张三·普工", "w-77")
        await self._sender(directory(handler)).deliver(
            "acc", "张三·普工", self._card([{"type": "blacklist", "reason": "骚扰"}]),
        )
        self.assertEqual(called, [])

    async def test_bad_status_does_not_break_delivery(self) -> None:
        """★ 状态写不出去不该连累这一轮的回复——两件事，别互相拖累。"""
        self._seed("张三·普工", "w-77")
        card = {"type": "info", "body": {"kind": "wechat_reply", "reply_segments": ["在的"],
                                         "intent": "consult", "funnel_stage": "qa",
                                         "actions": [{"type": "set_ai_status",
                                                      "payload": {"status": "托管中"}}]}}
        result = await self._sender(directory(lambda _r: httpx.Response(200, json={}))).deliver(
            "acc", "张三·普工", card,
        )
        self.assertTrue(result.delivered)


if __name__ == "__main__":
    unittest.main()
