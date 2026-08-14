"""网关端到端（Phase A+B 合流）——一条微信消息进，一条回复出。

链路上**只有微信那一端是假的**：真 GatewayRunner、真 GatewayStore（真 SQLite）、
真 AgentWsClient（真 WebSocket），对面是按契约应答的 Fake AI 端。

微信端用假驱动是有意为之：真微信只能在 Windows 上跑，而这条链路的所有逻辑
——换票、建连、会话映射、去抖、串行、守门、出站——都不该依赖它才能验证。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fake_agent_ws import FakeAgentServer, TurnScript

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    InboundMessage,
    LocalFileTicketProvider,
    is_timestamp_row,
    message_signature,
    split_new_messages,
    to_inbound,
)

T0 = datetime(2026, 8, 10, 14, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"
PEER = "候选人-王建国"


def wechat_card(segments: list[str], **body: Any) -> dict[str, Any]:
    return {
        "type": "info",
        "skill": "wechat_reply",
        "body": {
            "kind": "wechat_reply",
            "reply_segments": segments,
            "intent": "consult",
            "funnel_stage": "intent_check",
            "actions": [],
            **body,
        },
    }


class ScriptedSource:
    """假微信驱动：按 tick 顺序吐批次。"""

    def __init__(self, batches: list[list[InboundMessage]]) -> None:
        self._batches = list(batches)

    async def fetch(self, account_id: str) -> list[InboundMessage]:
        if not self._batches:
            return []
        return self._batches.pop(0)


class RecordingSink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, account_id: str, peer_id: str, text: str) -> str:
        self.sent.append((account_id, peer_id, text))
        return f"task-{len(self.sent)}"


def inbound(text: str, *, msg_id: str, at: datetime, from_self: bool = False) -> InboundMessage:
    return InboundMessage(
        account_id=ACCOUNT, peer_id=PEER, msg_id=msg_id, text=text, at=at, from_self=from_self
    )


class GatewayEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets_file = root / "tickets.json"
        self.tickets_file.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.now = T0

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _settings(self, server: FakeAgentServer, **overrides: Any) -> GatewaySettings:
        base: dict[str, Any] = {
            "ai_ws_url": server.url,
            "local_ticket_file": self.tickets_file,
            "debounce_seconds": 3.0,
            "turn_timeout_seconds": 10.0,
            # 时间窗给满：T0 是 UTC，换算到跑测试那台机器的本地时间是几点，不该影响这些用例。
            "active_hours": "00:00-23:59",
        }
        base.update(overrides)
        return GatewaySettings(**base)

    def _runner(self, server: FakeAgentServer, source, sink, **overrides: Any) -> GatewayRunner:
        return GatewayRunner(
            settings=self._settings(server, **overrides),
            ticket_provider=LocalFileTicketProvider(self.tickets_file),
            store=self.store,
            source=source,
            sink=sink,
            accounts=[ACCOUNT],
            clock=lambda: self.now,
        )

    async def _advance_and_tick(self, runner: GatewayRunner, seconds: int) -> int:
        self.now = self.now + timedelta(seconds=seconds)
        count = await runner.tick()
        await runner.drain()
        return count

    # ────────────────────────── 主路径 ──────────────────────────

    async def test_inbound_message_produces_wechat_reply(self) -> None:
        """完整回路：候选人发消息 → 换票建连 → 建会话 → 一轮 → 卡片 → 逐段发出。"""
        script = TurnScript(cards=[wechat_card(["你好，我们这边还在招普工", "方便说下你在哪个区吗"])])
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            source = ScriptedSource([[inbound("还招人吗", msg_id="m1", at=T0)], []])
            sink = RecordingSink()
            runner = self._runner(server, source, sink)
            try:
                self.assertEqual(await runner.tick(), 0)          # 去抖窗口内
                self.assertEqual(await self._advance_and_tick(runner, 10), 1)

                # 两段回复 = 两条微信消息，顺序不变、内容原样
                self.assertEqual(
                    [text for _, _, text in sink.sent],
                    ["你好，我们这边还在招普工", "方便说下你在哪个区吗"],
                )
                self.assertTrue(all(peer == PEER for _, peer, _ in sink.sent))

                # 会话映射已落库，下次同一个人接着聊
                self.assertIsNotNone(self.store.get_session(ACCOUNT, PEER))
                # 发送已记账，供频次守门
                self.assertEqual(self.store.sends_in_last_hour(ACCOUNT, self.now), 2)
            finally:
                await runner.stop()

    async def test_second_turn_reuses_same_session(self) -> None:
        """同一个候选人的第二轮必须复用同一会话——换会话等于丢掉上下文。"""
        script = TurnScript(cards=[wechat_card(["好的"])])
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            source = ScriptedSource([
                [inbound("还招人吗", msg_id="m1", at=T0)],
                [],
                [inbound("在武汉", msg_id="m2", at=T0 + timedelta(minutes=1))],
                [],
            ])
            runner = self._runner(server, source, RecordingSink())
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)
                first_session = self.store.get_session(ACCOUNT, PEER)

                await self._advance_and_tick(runner, 10)   # 第二条到达
                await self._advance_and_tick(runner, 10)   # 派发第二轮

                self.assertEqual(self.store.get_session(ACCOUNT, PEER), first_session)
                starts = [f for f in server.received if f.get("op") == "session.start"]
                self.assertEqual(len(starts), 1, "第二轮不该再建新会话")
            finally:
                await runner.stop()

    async def test_transcript_carries_speaker_and_meta(self) -> None:
        """发给 AI 端的必须是带说话人标注的上下文，并声明专员是否插过话。"""
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[wechat_card(["好"])])
        ) as server:
            source = ScriptedSource([[
                inbound("还招人吗", msg_id="m1", at=T0),
                inbound("稍等我看下", msg_id="m2", at=T0 + timedelta(minutes=2), from_self=True),
            ], []])
            runner = self._runner(server, source, RecordingSink())
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)

                sent = next(f for f in server.received if f.get("op") == "message.send")
                text = sent["data"]["text"]
                self.assertIn("[对方 · 14:00] 还招人吗", text)
                self.assertIn("[专员本人 · 14:02] 稍等我看下", text)
                self.assertNotIn(PEER, text)          # 不泄露联系人标识
                self.assertTrue(sent["data"]["wechat"]["operator_intervened"])
            finally:
                await runner.stop()

    async def test_operator_only_batch_never_triggers_a_turn(self) -> None:
        """★ 一批里全是专员本人说的话，绝不触发一轮。

        网关自己发出去的回复，下一轮轮询会被原样读回来（带 is_self）。要是拿它触发一轮，
        就是自己回自己、一直刷。这些话要留着并进对方下一次真回话的上下文里（§5.2）。
        """
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[wechat_card(["好的"])])
        ) as server:
            source = ScriptedSource([
                [inbound("我们这边还在招普工", msg_id="s1", at=T0, from_self=True)],
                [],
                [inbound("那我想问下工资", msg_id="m1", at=T0 + timedelta(minutes=1))],
                [],
            ])
            sink = RecordingSink()
            runner = self._runner(server, source, sink)
            try:
                await runner.tick()
                self.assertEqual(await self._advance_and_tick(runner, 10), 0, "自己说的话不能触发一轮")
                self.assertEqual(sink.sent, [])
                self.assertFalse([f for f in server.received if f.get("op") == "message.send"])

                await self._advance_and_tick(runner, 10)   # 对方真回话了
                self.assertEqual(await self._advance_and_tick(runner, 10), 1)

                sent = next(f for f in server.received if f.get("op") == "message.send")
                text = sent["data"]["text"]
                self.assertIn("我们这边还在招普工", text, "专员说过的话要并进上下文，别让 AI 重复问")
                self.assertIn("那我想问下工资", text)
            finally:
                await runner.stop()

    # ────────────────────────── 不回复与拉黑 ──────────────────────────

    async def test_no_reply_card_sends_nothing(self) -> None:
        card = wechat_card([], no_reply_reason="对方只发了表情", intent="smalltalk")
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[card])
        ) as server:
            sink = RecordingSink()
            runner = self._runner(server, ScriptedSource([[inbound("👌", msg_id="m1", at=T0)], []]), sink)
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)
                self.assertEqual(sink.sent, [])
            finally:
                await runner.stop()

    async def test_inbound_rejection_stops_everything_before_any_ai_call(self) -> None:
        """确定性拒绝闸在最前面：不建连、不发轮、不回消息，直接拉黑。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            sink = RecordingSink()
            runner = self._runner(
                server, ScriptedSource([[inbound("别再联系我了", msg_id="m1", at=T0)], []]), sink
            )
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)

                self.assertEqual(sink.sent, [])
                self.assertTrue(self.store.is_blacklisted(ACCOUNT, PEER))
                self.assertEqual(server.received, [], "拒绝的人不该触发任何 AI 调用")
            finally:
                await runner.stop()

    async def test_ai_blacklist_action_blocks_later_turns(self) -> None:
        """AI 端判定拒绝 → 拉黑生效，后续消息一律不再处理。"""
        card = wechat_card(
            [], no_reply_reason="对方拒绝", intent="reject", funnel_stage="closed",
            actions=[{"type": "blacklist", "reason": "对方明确表示不要再联系"}],
        )
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[card])
        ) as server:
            sink = RecordingSink()
            source = ScriptedSource([
                [inbound("我考虑一下吧不用了", msg_id="m1", at=T0)],
                [],
                [inbound("再问一句", msg_id="m2", at=T0 + timedelta(minutes=5))],
                [],
            ])
            runner = self._runner(server, source, sink)
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)
                self.assertTrue(self.store.is_blacklisted(ACCOUNT, PEER))

                sends_before = len([f for f in server.received if f.get("op") == "message.send"])
                await self._advance_and_tick(runner, 10)
                await self._advance_and_tick(runner, 10)
                sends_after = len([f for f in server.received if f.get("op") == "message.send"])

                self.assertEqual(sends_before, sends_after, "已拉黑的人不该再触发一轮")
                self.assertEqual(sink.sent, [])
            finally:
                await runner.stop()

    # ────────────────────────── 韧性 ──────────────────────────

    async def test_ai_error_sends_nothing_and_keeps_running(self) -> None:
        """AI 端报错 → 不发消息、不崩，下一条照常处理。"""
        scripts = iter([
            TurnScript(error={"code": "upstream_error", "message": "繁忙",
                              "fallback_card": {"type": "info", "body": {"text": "稍后重试"}}}),
            TurnScript(cards=[wechat_card(["你好"])]),
        ])
        current = {"script": next(scripts)}

        def script_for(_s: str, _t: str) -> TurnScript:
            return current["script"]

        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script_for) as server:
            sink = RecordingSink()
            source = ScriptedSource([
                [inbound("第一条", msg_id="m1", at=T0)],
                [],
                [inbound("第二条", msg_id="m2", at=T0 + timedelta(minutes=1))],
                [],
            ])
            runner = self._runner(server, source, sink)
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)
                self.assertEqual(sink.sent, [])         # 报错轮什么都不发

                current["script"] = next(scripts)
                await self._advance_and_tick(runner, 10)
                await self._advance_and_tick(runner, 10)
                self.assertEqual([t for _, _, t in sink.sent], ["你好"])
            finally:
                await runner.stop()

    async def test_reconnects_after_ai_端_drops_connection(self) -> None:
        """AI 端断线 → 现换新票重连 → 接回同一会话继续，不丢上下文。"""
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[wechat_card(["好的"])])
        ) as server:
            sink = RecordingSink()
            source = ScriptedSource([
                [inbound("第一条", msg_id="m1", at=T0)],
                [],
                [inbound("第二条", msg_id="m2", at=T0 + timedelta(minutes=1))],
                [],
            ])
            runner = self._runner(server, source, sink)
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)
                session_id = self.store.get_session(ACCOUNT, PEER)

                # 模拟断线：把连接踢掉
                await runner._pool.invalidate(ACCOUNT)

                await self._advance_and_tick(runner, 10)
                await self._advance_and_tick(runner, 10)

                auths = [f for f in server.received if f.get("op") == "auth"]
                resumes = [f for f in server.received if f.get("op") == "session.resume"]
                self.assertEqual(len(auths), 2, "重连必须重新鉴权（现换新票）")
                self.assertEqual(len(resumes), 1, "重连后应接回原会话而不是新建")
                self.assertEqual(resumes[0]["data"]["session_id"], session_id)
                self.assertEqual(self.store.get_session(ACCOUNT, PEER), session_id)
                self.assertEqual(len([t for _, _, t in sink.sent]), 2)
            finally:
                await runner.stop()

    async def test_stale_session_is_rebuilt(self) -> None:
        """库里存的会话 AI 端已经不认（被驱逐）→ 丢掉重建，不能卡死这个联系人。"""
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[wechat_card(["你好"])])
        ) as server:
            # 预置一个 AI 端根本不知道的 session_id
            self.store.put_session(ACCOUNT, PEER, "session-from-a-previous-life")
            sink = RecordingSink()
            runner = self._runner(server, ScriptedSource([[inbound("在吗", msg_id="m1", at=T0)], []]), sink)
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)

                self.assertNotEqual(self.store.get_session(ACCOUNT, PEER), "session-from-a-previous-life")
                self.assertEqual([t for _, _, t in sink.sent], ["你好"])
            finally:
                await runner.stop()

    async def test_bad_ticket_does_not_send_anything(self) -> None:
        """换不到有效票 → fail-closed，一条都不发。"""
        async with FakeAgentServer(valid_tickets={"tkt-only-valid-one"}) as server:
            sink = RecordingSink()
            runner = self._runner(server, ScriptedSource([[inbound("在吗", msg_id="m1", at=T0)], []]), sink)
            try:
                await runner.tick()
                await self._advance_and_tick(runner, 10)
                self.assertEqual(sink.sent, [])
            finally:
                await runner.stop()

    async def test_rate_limit_stops_sending(self) -> None:
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[wechat_card(["你好", "在哪个区"])])
        ) as server:
            sink = RecordingSink()
            runner = self._runner(
                server, ScriptedSource([[inbound("在吗", msg_id="m1", at=T0)], []]),
                sink, max_sends_per_hour=1,
            )
            try:
                self.store.record_send(ACCOUNT, PEER, T0 - timedelta(minutes=5))
                await runner.tick()
                await self._advance_and_tick(runner, 10)
                self.assertEqual(sink.sent, [], "超过频次上限就一条都不发")
            finally:
                await runner.stop()


    # ────────────────────────── 建连与保活 ──────────────────────────

    async def test_connects_before_any_inbound_message(self) -> None:
        """一条消息都没有也要连上：界面要能说清通没通，票据坏了要在启动时就炸。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, ScriptedSource([[]]), RecordingSink())
            try:
                await runner.ensure_connections()
                self.assertEqual(runner.connected_count(), 1)
                self.assertIsNone(runner.connect_error)
                self.assertEqual([f.get("op") for f in server.received], ["auth"])
            finally:
                await runner.stop()

    async def test_keepalive_pings_the_live_connection(self) -> None:
        """已经连着就只 ping，不重连——反代会掐空闲连接，ping 是用来续命的。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, ScriptedSource([[]]), RecordingSink())
            try:
                await runner.ensure_connections()
                await runner.ensure_connections()
                ops = [f.get("op") for f in server.received]
                self.assertEqual(ops, ["auth", "ping"], "第二次只该 ping，不该重新 auth")
                self.assertEqual(runner.connected_count(), 1)
            finally:
                await runner.stop()

    async def test_connect_failure_is_recorded_not_raised(self) -> None:
        """连不上只记账：AI 端挂了不该把入站轮询也带停，但状态栏得看得见原因。"""
        async with FakeAgentServer(valid_tickets={"别的票"}) as server:
            runner = self._runner(server, ScriptedSource([[]]), RecordingSink())
            try:
                await runner.ensure_connections()  # 不抛
                self.assertEqual(runner.connected_count(), 0)
                self.assertIsNotNone(runner.connect_error)
            finally:
                await runner.stop()


class WeChatIoMappingTests(unittest.TestCase):
    """微信侧字段映射的纯函数部分（真机验证前先把逻辑钉住）。"""

    def test_first_sight_of_peer_replies_to_nothing(self) -> None:
        """★ 网关首次见到某个联系人：只对齐游标，绝不照着历史逐条回复。"""
        history = [
            {"sender": "对方", "text": "三个月前的消息"},
            {"sender": "我", "text": "早就回过了"},
        ]
        fresh, cursor = split_new_messages(history, None)
        self.assertEqual(fresh, [])
        self.assertEqual(cursor, message_signature(history[-1]))

    def test_only_messages_after_cursor_are_new(self) -> None:
        history = [
            {"sender": "对方", "text": "老消息"},
            {"sender": "我", "text": "已回复"},
            {"sender": "对方", "text": "新问题"},
        ]
        cursor = message_signature(history[1])
        fresh, new_cursor = split_new_messages(history, cursor)
        self.assertEqual([m["text"] for m in fresh], ["新问题"])
        self.assertEqual(new_cursor, message_signature(history[-1]))

    def test_cursor_scrolled_out_does_not_backfill(self) -> None:
        """游标滚出读取窗口 → 保守跳过，不回溯几十条老消息。"""
        history = [{"sender": "对方", "text": "只剩这条"}]
        fresh, cursor = split_new_messages(history, "signature-that-no-longer-exists")
        self.assertEqual(fresh, [])
        self.assertEqual(cursor, message_signature(history[0]))

    def test_empty_history(self) -> None:
        self.assertEqual(split_new_messages([], "abc"), ([], "abc"))

    def test_self_sender_marks_operator_message(self) -> None:
        message = to_inbound("acct", "peer", {"sender": "我", "text": "稍等"}, seq=1)
        self.assertTrue(message.from_self)
        self.assertEqual(message.msg_id, "peer#1")

        other = to_inbound("acct", "peer", {"sender": "王建国", "text": "在吗"}, seq=2)
        self.assertFalse(other.from_self)

    def test_is_self_flag_wins_over_missing_sender(self) -> None:
        """★ 真机权威字段是 is_self；多数行根本没有 sender。

        认错的代价是网关把自己刚发出去的回复当成入站，自己回自己。
        """
        mine = to_inbound("acct", "peer", {"is_self": True, "text": "我们这边还在招"}, seq=1)
        self.assertTrue(mine.from_self)

        theirs = to_inbound("acct", "peer", {"is_self": False, "text": "还招人吗"}, seq=2)
        self.assertFalse(theirs.from_self)

    def test_timestamp_rows_are_not_messages(self) -> None:
        """聊天记录里夹的「18:28」这类时间分隔行不是消息，不能当入站、也不能钉游标。"""
        self.assertTrue(is_timestamp_row({"text": "18:28", "is_timestamp": True}))
        self.assertFalse(is_timestamp_row({"text": "还招人吗", "is_timestamp": False}))

    def test_identical_consecutive_messages_get_distinct_ids(self) -> None:
        """同一个人连发两条一模一样的话，两条都要能被处理。"""
        first = to_inbound("acct", "peer", {"sender": "对方", "text": "在吗"}, seq=1)
        second = to_inbound("acct", "peer", {"sender": "对方", "text": "在吗"}, seq=2)
        self.assertNotEqual(first.msg_id, second.msg_id)


class CommandRuntime:
    """假底座：只记「投了什么任务」，不碰真微信。两条指令测试共用。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
        self.calls.append((task_type, dict(payload or {})))
        return {"result": {"ok": True}}


class CommandChannelTests(unittest.IsolatedAsyncioTestCase):
    """AI 端推一条能力指令 → 网关执行 → 回执（066 design §3）。

    不继承 GatewayEndToEndTests：那会把父类十几条用例再跑一遍，白花一倍时间。
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets_file = root / "tickets.json"
        self.tickets_file.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.now = T0

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, server: FakeAgentServer) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(
                ai_ws_url=server.url, local_ticket_file=self.tickets_file,
                turn_timeout_seconds=10.0, active_hours="00:00-23:59",
            ),
            ticket_provider=LocalFileTicketProvider(self.tickets_file),
            store=self.store,
            source=ScriptedSource([[]]),
            sink=RecordingSink(),
            accounts=[ACCOUNT],
            clock=lambda: self.now,
        )

    async def test_push_command_executes_and_reports_back(self) -> None:
        from wxclient.gateway import CommandExecutor, OutboundGuard
        from wxclient.gateway.commands import COMMAND_EVENT
        from wxclient.queue import TaskType

        runtime = CommandRuntime()
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            runner._commands = CommandExecutor(
                runtime=runtime,
                store=self.store,
                guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
                account_id=ACCOUNT,
                allow_real_actions=True,
                confirmation="确认",
                clock=lambda: self.now,
            )
            try:
                await runner.ensure_connections()          # 先有连接才收得到推送
                runner._on_push(ACCOUNT, COMMAND_EVENT, {
                    "command_id": "cmd-1", "op": "send_message",
                    "args": {"to": "张三", "text": "面试改到明天下午三点"},
                })
                for _ in range(100):                        # 指令是丢给事件循环跑的
                    if runtime.calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(runtime.calls), 1)
                self.assertIs(runtime.calls[0][0], TaskType.SEND_MESSAGE)

                # 073：回执是**一串阶段**（接收 → 开始 → 终态），不是一锤子。
                # 只有终态才算这条指令结束；前两个是让人看见「在跑」而不是「丢了」。
                for _ in range(100):                        # 回执是 client → server 的 op 帧
                    receipts = [f for f in server.received if f.get("op") == "wechat.result"]
                    if any(r["data"]["status"] == "done" for r in receipts):
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(all(r["data"]["command_id"] == "cmd-1" for r in receipts))
                self.assertEqual(
                    [r["data"]["status"] for r in receipts],
                    ["received", "started", "done"],
                    "阶段序列不对：需求文档 §12.4 要 接收/开始/成功",
                )
            finally:
                await runner.stop()


    async def test_scheduled_command_is_parked_not_fired_now(self) -> None:
        """★ 077：带 schedule_at 的指令不许当场发出去。

        「明天上午九点提醒他」原来会被**立刻**执行——比不做还糟。
        """
        from wxclient.gateway import CommandExecutor, OutboundGuard
        from wxclient.gateway.commands import COMMAND_EVENT
        from wxclient.queue import TaskType

        runtime = CommandRuntime()
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            runner._commands = CommandExecutor(
                runtime=runtime,
                store=self.store,
                guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
                account_id=ACCOUNT,
                allow_real_actions=True,
                confirmation="确认",
                clock=lambda: self.now,
            )
            try:
                await runner.ensure_connections()
                later = (self.now + timedelta(hours=18)).isoformat()
                runner._on_push(ACCOUNT, COMMAND_EVENT, {
                    "command_id": "cmd-later", "op": "send_message",
                    "args": {"to": "张三", "text": "提醒您明天面试"},
                    "schedule_at": later,
                })
                for _ in range(50):
                    receipts = [f for f in server.received if f.get("op") == "wechat.result"]
                    if receipts:
                        break
                    await asyncio.sleep(0.02)

                self.assertEqual(runtime.calls, [], "定时指令不该当场执行")
                self.assertEqual(self.store.count_scheduled_commands(), 1)
                # 报「排上了」，否则卡上停在「已下发」，而它其实十八小时后才动。
                self.assertEqual(receipts[0]["data"]["status"], "scheduled")
                self.assertEqual(receipts[0]["data"]["scheduled_for"], later)

                # 到点之后由 schedule 循环放出去，走的还是同一条执行路径。
                self.now = self.now + timedelta(hours=18, minutes=1)
                self.assertEqual(await runner.schedule.tick(), 1)
                for _ in range(50):
                    if runtime.calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(len(runtime.calls), 1)
                self.assertIs(runtime.calls[0][0], TaskType.SEND_MESSAGE)
                self.assertEqual(self.store.count_scheduled_commands(), 0)
            finally:
                await runner.stop()


if __name__ == "__main__":
    unittest.main()
