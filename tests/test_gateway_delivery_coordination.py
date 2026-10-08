"""开场发送协调（AI 端 spec 138）——网关这一侧。

全部在 Linux 跑，不碰真微信。断言三件事：
① 逐条来源按本机事实认（控件类型、出站台账、好友关系台账），不按正文猜；
② 好友关系周期本机持久、只在真加上好友时换；
③ 开场指令执行前复核、先落「在执行」再动微信、回执如实三分（发了 / 没发 / 不知道）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wxclient.gateway import CommandExecutor, GatewayStore, OutboundGuard
from wxclient.gateway.capabilities import (
    CAP_DELIVERY_COORDINATION,
    capabilities_frame,
    capability_status,
    probe_capabilities,
)
from wxclient.gateway.coordination import (
    RELATIONSHIP_ESTABLISHED,
    build_delivery_context,
    wire_message_id,
)
from wxclient.gateway.inbound import InboundMessage, message_from_row, message_to_row
from wxclient.gateway.wechat_io import to_inbound

BEIJING = timezone(timedelta(hours=8))
WORKDAY = datetime(2026, 10, 8, 14, 0, tzinfo=BEIJING)
ACCOUNT = "zk_weixin"
REF = "a" * 32
PEER = "张三-普工"
ACCEPT = "我通过了你的朋友验证请求，现在我们可以开始聊天了"
UIA_OK = {"logged_in": True, "uia_exposed": True}


def _msg(seq: int, text: str, **kw: Any) -> InboundMessage:
    return InboundMessage(account_id=ACCOUNT, peer_id=PEER, msg_id=f"{PEER}#{seq}", text=text,
                          at=WORKDAY + timedelta(seconds=seq), **kw)


class SourceTests(unittest.TestCase):
    def test_来源按本机事实认_不按正文(self) -> None:
        ctx = build_delivery_context([
            _msg(1, "你已添加了张三，现在可以开始聊天了", system_tip=True),
            _msg(2, "工资多少"),
            _msg(3, "我来接待", from_self=True),
            _msg(4, "您好", from_self=True, from_gateway=True),
        ])
        self.assertEqual([m["source_kind"] for m in ctx["messages"]], ["system", "user", "operator", "assistant"])
        self.assertTrue(all(m["occurred_at"].endswith("Z") for m in ctx["messages"]))
        self.assertTrue(all(m["message_id"].startswith("m-") and len(m["message_id"]) == 34 for m in ctx["messages"]))

    def test_没有刚加上的好友关系_手打的通过同文仍是对方说的话(self) -> None:
        ctx = build_delivery_context([_msg(1, ACCEPT)])
        self.assertEqual(ctx["messages"][0]["source_kind"], "user")
        self.assertNotIn("event_kind", ctx["messages"][0])

    def test_刚加上好友那一批_带好友建立事件且通过同文按系统行报(self) -> None:
        established = WORKDAY - timedelta(minutes=1)
        ctx = build_delivery_context([_msg(1, ACCEPT), _msg(2, "包住吗")], relationship=("rel-1", established))
        first, accept, question = ctx["messages"]
        self.assertEqual(first, {"message_id": "rel-1", "source_kind": "system", "event_kind": RELATIONSHIP_ESTABLISHED,
                                 "text": "", "occurred_at": "2026-10-08T05:59:00.000Z"})
        self.assertEqual(accept["source_kind"], "system")
        self.assertEqual(question["source_kind"], "user")

    def test_同一条消息的线上ID稳定_不同会话不同(self) -> None:
        self.assertEqual(wire_message_id(ACCOUNT, PEER, "x#1"), wire_message_id(ACCOUNT, PEER, "x#1"))
        self.assertNotEqual(wire_message_id(ACCOUNT, PEER, "x#1"), wire_message_id(ACCOUNT, "李四", "x#1"))

    def test_超长批次留住事件和最新的话(self) -> None:
        many = [_msg(i, f"第{i}句") for i in range(100)]
        ctx = build_delivery_context(many, relationship=("rel-1", WORKDAY))
        self.assertEqual(len(ctx["messages"]), 64)
        self.assertEqual(ctx["messages"][0]["message_id"], "rel-1")
        self.assertEqual(ctx["messages"][-1]["text"], "第99句")

    def test_系统提示行按控件类型认_且能落盘往返(self) -> None:
        tip = to_inbound(ACCOUNT, PEER, {"class_name": "mmui::ChatItemView", "text": "以上是打招呼的内容"}, seq=1)
        bubble = to_inbound(ACCOUNT, PEER, {"class_name": "mmui::ChatTextItemView", "text": "以上是打招呼的内容"}, seq=2)
        stamp = to_inbound(ACCOUNT, PEER, {"class_name": "mmui::ChatItemView", "text": "18:28", "is_timestamp": True}, seq=3)
        self.assertTrue(tip.system_tip)
        self.assertFalse(bubble.system_tip)
        self.assertFalse(stamp.system_tip)
        self.assertTrue(message_from_row(ACCOUNT, PEER, message_to_row(tip)).system_tip)


class RelationshipLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "gateway.db"
        self.store = GatewayStore(self.path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_重启后同一次加好友还是同一个id_交给AI端后不再重复报(self) -> None:
        rel = self.store.open_relationship(ACCOUNT, PEER, at=WORKDAY)
        again = GatewayStore(self.path)
        self.assertEqual(again.pending_relationship(ACCOUNT, PEER, now=WORKDAY)[0], rel)
        again.mark_relationship_announced(ACCOUNT, PEER, rel)
        self.assertIsNone(again.pending_relationship(ACCOUNT, PEER, now=WORKDAY))

    def test_删了又加才换新id_旧id的销账不能销掉新的(self) -> None:
        first = self.store.open_relationship(ACCOUNT, PEER, at=WORKDAY)
        second = self.store.open_relationship(ACCOUNT, PEER, at=WORKDAY + timedelta(days=1))
        self.assertNotEqual(first, second)
        self.store.mark_relationship_announced(ACCOUNT, PEER, first)
        self.assertEqual(self.store.pending_relationship(ACCOUNT, PEER, now=WORKDAY + timedelta(days=1))[0], second)

    def test_太久以前的加好友不再当刚加上(self) -> None:
        self.store.open_relationship(ACCOUNT, PEER, at=WORKDAY)
        self.assertIsNone(self.store.pending_relationship(ACCOUNT, PEER, now=WORKDAY + timedelta(days=4)))

    def test_对方开口判断不算系统行和通过同文(self) -> None:
        self.store.record_messages(ACCOUNT, PEER, [
            {"msg_id": "1", "at": WORKDAY.isoformat(), "text": ACCEPT, "raw_text": ACCEPT},
            {"msg_id": "2", "at": (WORKDAY + timedelta(seconds=5)).isoformat(), "text": "你已添加了", "system_tip": True},
            {"msg_id": "3", "at": (WORKDAY + timedelta(seconds=9)).isoformat(), "text": "好", "from_self": True},
        ])
        self.assertIsNone(self.store.last_inbound_at(ACCOUNT, PEER, ignore_texts=[ACCEPT]))
        self.store.record_messages(ACCOUNT, PEER, [{"msg_id": "4", "at": (WORKDAY + timedelta(seconds=20)).isoformat(), "text": "工资多少"}])
        self.assertEqual(self.store.last_inbound_at(ACCOUNT, PEER, ignore_texts=[ACCEPT]), WORKDAY + timedelta(seconds=20))


class CapabilityTests(unittest.TestCase):
    def test_认得出微信号才报协调_并带上号(self) -> None:
        caps = probe_capabilities(uia=UIA_OK, allow_real_actions=True, has_file_channel=True, account_ref=REF)
        self.assertTrue(caps[CAP_DELIVERY_COORDINATION].available)
        self.assertEqual(capabilities_frame(caps, REF)["delivery_identity"], {"account_id": REF})

    def test_只走流程_不报协调也不带号(self) -> None:
        caps = probe_capabilities(uia=UIA_OK, allow_real_actions=False, has_file_channel=True, account_ref=REF)
        self.assertFalse(caps[CAP_DELIVERY_COORDINATION].available)
        self.assertNotIn("delivery_identity", capabilities_frame(caps, REF))

    def test_认不出号_干脆不声明_企业端状态不因此降级(self) -> None:
        caps = probe_capabilities(uia=UIA_OK, allow_real_actions=True, has_file_channel=True, account_ref="")
        self.assertNotIn(CAP_DELIVERY_COORDINATION, caps)
        self.assertNotIn("delivery_identity", capabilities_frame(caps, ""))
        self.assertEqual(capability_status(caps), "passed")


class FakeRuntime:
    def __init__(self, *, status: str = "succeeded", result: dict[str, Any] | None = None, boom: bool = False) -> None:
        self.calls: list[Any] = []
        self.status = status
        self.result = result or {"ok": True}
        self.boom = boom

    async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
        if self.boom:
            raise KeyboardInterrupt  # 模拟进程在发到一半时没了
        self.calls.append((task_type, dict(payload or {})))
        return {"status": self.status, "result": dict(self.result), "task_id": "t1"}


class DeliveryCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.ref = REF

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, runtime: FakeRuntime, **kw: Any) -> CommandExecutor:
        return CommandExecutor(runtime=runtime, store=self.store,
                               guard=OutboundGuard(self.store, active_hours=kw.pop("active_hours", "00:00-24:00")),
                               account_id=ACCOUNT, allow_real_actions=True, confirmation="确认",
                               clock=lambda: WORKDAY, account_ref=lambda: self.ref, **kw)

    def _command(self, **over: Any) -> dict[str, Any]:
        cmd = {"command_id": "delivery:1", "op": "send_message", "issued_at": "2026-10-08T05:59:00.000Z",
               "args": {"to": PEER, "text": "您好，我是招聘助理", "delivery_context": {"account_id": REF, "intent_id": "k", "fence": 1}}}
        cmd.update(over)
        return cmd

    async def test_真发出去_回sent和消息凭据(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute(self._command())
        self.assertEqual(result.status, "done")
        self.assertEqual(result.result["delivery_status"], "sent")
        self.assertTrue(result.result["message_id"])
        self.assertEqual(len(runtime.calls), 1)

    async def test_换了微信号_动都不动微信_回明确没发(self) -> None:
        self.ref = "b" * 32
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute(self._command())
        self.assertEqual((result.status, result.result["delivery_status"], result.result["reason_code"]), ("rejected", "not_sent", "ACCOUNT_MISMATCH"))
        self.assertEqual(runtime.calls, [])

    async def test_派发后对方已开口_开场撤掉_回明确没发(self) -> None:
        self.store.record_messages(ACCOUNT, PEER, [{"msg_id": "1", "at": "2026-10-08T06:00:00+00:00", "text": "工资多少"}])
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute(self._command())
        self.assertEqual(result.result["reason_code"], "PEER_REPLIED")
        self.assertEqual(runtime.calls, [])

    async def test_通过同文和系统行不算对方开口(self) -> None:
        self.store.record_messages(ACCOUNT, PEER, [
            {"msg_id": "1", "at": "2026-10-08T06:00:00+00:00", "text": ACCEPT, "raw_text": ACCEPT},
            {"msg_id": "2", "at": "2026-10-08T06:00:01+00:00", "text": "你已添加了", "system_tip": True},
        ])
        result = await self._executor(FakeRuntime()).execute(self._command())
        self.assertEqual(result.result["delivery_status"], "sent")

    async def test_守门拦下_回明确没发(self) -> None:
        result = await self._executor(FakeRuntime(), active_hours="00:00-00:01").execute(self._command())
        self.assertEqual((result.status, result.result["delivery_status"]), ("rejected", "not_sent"))

    async def test_查无此人_回明确没发(self) -> None:
        runtime = FakeRuntime(status="failed", result={"reason_code": "CONTACT_NOT_FOUND"})
        result = await self._executor(runtime).execute(self._command())
        self.assertEqual((result.result["delivery_status"], result.result["reason_code"]), ("not_sent", "CONTACT_NOT_FOUND"))

    async def test_半路失败或超时_不说发了也不说没发(self) -> None:
        for runtime in (FakeRuntime(status="failed"), FakeRuntime(status="running", result={"x": 1})):
            self.store = GatewayStore(Path(self._tmp.name) / f"{id(runtime)}.db")
            result = await self._executor(runtime).execute(self._command())
            self.assertEqual(result.status, "failed")
            self.assertNotIn("delivery_status", result.result or {})

    async def test_发到一半进程没了_重推同号不再发(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            await self._executor(FakeRuntime(boom=True)).execute(self._command())
        runtime = FakeRuntime()
        replay = await self._executor(runtime).execute(self._command())
        self.assertEqual(replay.status, "failed")
        self.assertNotIn("delivery_status", replay.result or {})
        self.assertEqual(runtime.calls, [])

    async def test_同号重推回原结果不重发(self) -> None:
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        first = await executor.execute(self._command())
        again = await executor.execute(self._command())
        self.assertEqual(again.result, first.result)
        self.assertEqual(len(runtime.calls), 1)


if __name__ == "__main__":
    unittest.main()


class RunnerTurnTests(unittest.IsolatedAsyncioTestCase):
    """真 runner + 假 AI 端：报过协调能力的连接上，每轮带逐条来源、顶层正文留空；好友建立事件只报一次。"""

    async def asyncSetUp(self) -> None:
        import json

        from wxclient.gateway.capabilities import (
            CAP_DELIVERY_RECEIPT,
            CAP_PRIVATE_MESSAGE,
            Capability,
        )
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.now = WORKDAY.astimezone(timezone.utc)
        self.caps = {key: Capability(key=key, available=True)
                     for key in (CAP_PRIVATE_MESSAGE, CAP_DELIVERY_RECEIPT, CAP_DELIVERY_COORDINATION)}

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, server, batches):
        from wxclient.gateway import (
            GatewayRunner,
            GatewaySettings,
            LocalFileTicketProvider,
        )

        class Source:
            async def fetch(self, account_id):
                return batches.pop(0) if batches else []

        class Sink:
            async def send(self, account_id, peer_id, text):
                return "t"

        return GatewayRunner(
            settings=GatewaySettings(ai_ws_url=server.url, local_ticket_file=self.tickets, debounce_seconds=0.0,
                                     turn_timeout_seconds=10.0, active_hours="00:00-23:59"),
            ticket_provider=LocalFileTicketProvider(self.tickets), store=self.store, source=Source(), sink=Sink(),
            accounts=[ACCOUNT], clock=lambda: self.now, capabilities=lambda: self.caps, account_ref=lambda: REF,
        )

    async def test_报过协调能力后每轮带来源_好友事件只报一次(self) -> None:
        from fake_agent_ws import FakeAgentServer, TurnScript
        card = {"type": "info", "skill": "wechat_reply", "body": {"kind": "wechat_reply", "reply_segments": [], "actions": []}}
        relationship = self.store.open_relationship(ACCOUNT, PEER, at=self.now)
        batches = [[_msg(1, ACCEPT)], [_msg(2, "工资多少")]]
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[card, card])) as server:
            runner = self._runner(server, batches)
            try:
                await runner.ensure_connections()
                await (await runner._pool.acquire(ACCOUNT)).ping()  # 等假 AI 端读完已发出的帧
                reports = [f["data"] for f in server.received if f.get("op") == "wechat.capabilities"]
                self.assertEqual(reports[-1]["delivery_identity"], {"account_id": REF})
                for _ in range(2):
                    self.now += timedelta(seconds=5)
                    await runner.tick()
                    await runner.drain()
                sends = [f["data"] for f in server.received if f.get("op") == "message.send"]
                self.assertEqual(len(sends), 2)
                first, second = (s["wechat"]["delivery_context"]["messages"] for s in sends)
                self.assertTrue(all(s["text"] == "" for s in sends))
                self.assertEqual([m["source_kind"] for m in first], ["system", "system"])
                self.assertEqual(first[0]["message_id"], relationship)
                self.assertEqual(first[0]["event_kind"], RELATIONSHIP_ESTABLISHED)
                self.assertEqual([m["source_kind"] for m in second], ["user"])
                self.assertIsNone(self.store.pending_relationship(ACCOUNT, PEER, now=self.now))
            finally:
                await runner.stop()

    async def test_没报过协调能力就不带来源(self) -> None:
        from fake_agent_ws import FakeAgentServer, TurnScript
        self.caps.pop(CAP_DELIVERY_COORDINATION)
        card = {"type": "info", "skill": "wechat_reply", "body": {"kind": "wechat_reply", "reply_segments": [], "actions": []}}
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[card])) as server:
            runner = self._runner(server, [[_msg(1, "工资多少")]])
            try:
                await runner.ensure_connections()
                self.now += timedelta(seconds=5)
                await runner.tick()
                await runner.drain()
                send = next(f["data"] for f in server.received if f.get("op") == "message.send")
                self.assertNotIn("delivery_context", send["wechat"])
                self.assertIn("工资多少", send["text"])
            finally:
                await runner.stop()
