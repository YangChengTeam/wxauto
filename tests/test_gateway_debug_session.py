"""调试会话（界面上那一页）——真 runner + 真 WS + 假 AI 端，微信那端全程不该被碰。

这一页的价值全在「它走的是线上那条路」。所以这里断言的不是界面长什么样，而是**路径**：
开的是哪种会话、发出去的是不是原话、有没有顺手往微信发东西。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fake_agent_ws import FakeAgentServer, TurnScript

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    LocalFileTicketProvider,
)
from wxclient.gateway.debugchat import (
    DebugReply,
    format_receipt,
    format_turn,
    summarize_command,
)
from wxclient.gateway.runner import DEBUG_PEER_ID

T0 = datetime(2026, 8, 17, 14, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"


def command_card(commands: list[dict[str, Any]], **body: Any) -> dict[str, Any]:
    return {
        "type": "info",
        "skill": "wechat_ops",
        "body": {"kind": "wechat_command", "wechat_commands": commands, **body},
    }


class SilentSource:
    async def fetch(self, account_id: str) -> list:
        return []


class RecordingSink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, account_id: str, peer_id: str, text: str) -> str:
        self.sent.append((account_id, peer_id, text))
        return f"task-{len(self.sent)}"


class DebugSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets_file = root / "tickets.json"
        self.tickets_file.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.sink = RecordingSink()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, server: FakeAgentServer, **overrides: Any) -> GatewayRunner:
        base: dict[str, Any] = {
            "ai_ws_url": server.url,
            "local_ticket_file": self.tickets_file,
            "turn_timeout_seconds": 10.0,
            "active_hours": "00:00-23:59",
        }
        base.update(overrides)
        return GatewayRunner(
            settings=GatewaySettings(**base),
            ticket_provider=LocalFileTicketProvider(self.tickets_file),
            store=self.store,
            source=SilentSource(),
            sink=self.sink,
            accounts=[ACCOUNT],
            clock=lambda: T0,
        )

    def _starts(self, server: FakeAgentServer) -> list[dict[str, Any]]:
        return [f.get("data") or {} for f in server.received if f.get("op") == "session.start"]

    def _sends(self, server: FakeAgentServer) -> list[dict[str, Any]]:
        return [f.get("data") or {} for f in server.received if f.get("op") == "message.send"]

    # ────────────────────────── 路径 ──────────────────────────

    async def test_it_opens_an_ops_session_and_never_sends_anything_to_wechat(self) -> None:
        """★ 调试会话开的是 wechat_ops，而且**一条微信都不发**。

        拿 wechat_reply 去说「发朋友圈」，AI 端会当成候选人在说话、拟一句回复；
        而回复真被发出去的话，调试就成了往某个真人那儿发消息——这两件事都不能发生。
        """
        script = TurnScript(cards=[command_card(
            [{"op": "post_moments", "args": {"content": "招 3 个普工"}}],
            reply="已经发出去了",
        )])
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            runner = self._runner(server)
            try:
                result = await runner.debug_turn("发条朋友圈，招 3 个普工")
            finally:
                await runner.stop()

        self.assertEqual([s.get("task_type") for s in self._starts(server)], ["wechat_ops"])
        self.assertEqual([s.get("text") for s in self._sends(server)], ["发条朋友圈，招 3 个普工"])
        self.assertEqual(self.sink.sent, [], "调试会话不该往微信发任何东西")
        self.assertTrue(result.ok)
        self.assertEqual(format_turn(result).commands, ("post_moments（content=招 3 个普工）",))

    async def test_it_keeps_the_same_session_until_reset(self) -> None:
        """上下文要留着（「给他发个消息」的「他」得接得上），点「新会话」才断。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"},
                                   turn_script=TurnScript(cards=[])) as server:
            runner = self._runner(server)
            try:
                await runner.debug_turn("在吗")
                await runner.debug_turn("给张三发条消息")
                self.assertEqual(len(self._starts(server)), 1, "同一个调试会话不该重开")

                runner.reset_debug_session()
                await runner.debug_turn("重新开始")
                self.assertEqual(len(self._starts(server)), 2)
            finally:
                await runner.stop()

    async def test_the_debug_session_does_not_collide_with_a_real_contact(self) -> None:
        """调试会话在会话表里占的那个「对端」不能是任何真实 wxid。"""
        self.assertTrue(DEBUG_PEER_ID.startswith("__") and DEBUG_PEER_ID.endswith("__"))
        async with FakeAgentServer(valid_tickets={"tkt-ok"},
                                   turn_script=TurnScript(cards=[])) as server:
            runner = self._runner(server)
            try:
                await runner.debug_turn("在吗")
            finally:
                await runner.stop()
        self.assertIsNotNone(self.store.get_session(ACCOUNT, DEBUG_PEER_ID))

    async def test_ai_side_error_comes_back_as_an_error_not_as_silence(self) -> None:
        """AI 端报错要原样浮到界面上。显示成「没有产出」会让人去查微信那头——找错方向。"""
        script = TurnScript(error={"code": "rate_limited", "message": "太快了"})
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            runner = self._runner(server)
            try:
                result = await runner.debug_turn("发条朋友圈")
            finally:
                await runner.stop()
        self.assertFalse(result.ok)
        self.assertIn("太快了", format_turn(result).error)


class CommandReceiptTests(unittest.TestCase):
    """★ 回执必须能浮到界面上——这一页存在的全部意义就是「到底做没做」。"""

    def test_a_rejected_command_reaches_the_listener_with_its_reason(self) -> None:
        """2026-08-17 真机：AI 端回的是「没有在线的网关」，界面上却只有一句「已下发」。

        病根有两处，这条钉住其中一处：回执走 `card.update` 单独推回来，
        **不在那一轮里**（一轮早就 done 了），此前无人认领 → 只记一条 debug 日志就丢了。
        """
        seen: list[dict] = []
        runner = _bare_runner()
        runner.set_command_listener(seen.append)
        runner._on_push("acc", "card.update", {
            "session_id": "s1",
            "card": {"card_id": "c1", "body": {
                "kind": "wechat_command_result", "command_id": "cmd_1",
                "op": "send_message", "status": "rejected",
                "reason": "微信端没有在线的网关：请确认那台电脑上的劳小智微信助手正在运行",
            }},
        })
        self.assertEqual(len(seen), 1)
        receipt = format_receipt(seen[0])
        self.assertIsNotNone(receipt)
        self.assertIs(receipt.ok, False)
        self.assertEqual(receipt.label, "没有下发")
        self.assertIn("没有在线的网关", receipt.reason)

    def test_an_unknown_status_is_never_painted_as_success(self) -> None:
        """认不出的状态留在中间态。涂成绿勾＝把「不知道」显示成「成了」。"""
        receipt = format_receipt({"kind": "wechat_command_result", "op": "post_moments",
                                  "status": "some_new_state"})
        self.assertIsNone(receipt.ok)
        self.assertFalse(receipt.settled)

    def test_success_is_success(self) -> None:
        receipt = format_receipt({"kind": "wechat_command_result", "op": "send_message",
                                  "status": "ok"})
        self.assertIs(receipt.ok, True)
        self.assertTrue(receipt.settled)

    def test_other_card_updates_are_ignored(self) -> None:
        """别的 card.update（比如普通卡片刷新）不该被当成指令回执。"""
        seen: list[dict] = []
        runner = _bare_runner()
        runner.set_command_listener(seen.append)
        runner._on_push("acc", "card.update", {"card": {"body": {"kind": "wechat_reply"}}})
        self.assertEqual(seen, [])


def _bare_runner() -> GatewayRunner:
    """只为了调 `_on_push`：不连网、不建会话。"""
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    return GatewayRunner(
        settings=GatewaySettings(ai_ws_url="ws://127.0.0.1:1/v1/ws"),
        ticket_provider=LocalFileTicketProvider(Path(tmp.name) / "t.json"),
        store=GatewayStore(Path(tmp.name) / "g.db"),
        source=SilentSource(),
        sink=RecordingSink(),
        accounts=["acc"],
        clock=lambda: T0,
    )


class DebugReplyFormattingTests(unittest.TestCase):
    """``format_turn`` 是纯函数：给什么形状的卡，界面上就该出现什么。"""

    class Result:
        def __init__(self, *, cards=(), text="", error=None) -> None:
            self.cards = list(cards)
            self.text = text
            self.error = error

        @property
        def card(self):
            return self.cards[0] if self.cards else None

    def test_reply_and_commands_and_skipped_all_show_up(self) -> None:
        card = command_card(
            [{"op": "send_message", "args": {"to": "张三", "text": "明天十点面试"}}],
            reply="已经给张三发过去了",
            skipped=["李四：没找到这个人"],
        )
        got = format_turn(self.Result(cards=[card]))
        self.assertEqual(got.text, "已经给张三发过去了")
        self.assertEqual(got.commands, ("send_message（to=张三，text=明天十点面试）",))
        self.assertEqual(got.skipped, ("李四：没找到这个人",))

    def test_a_question_back_is_shown_as_the_reply(self) -> None:
        """缺参数时 AI 端回问一句（``ask``），那句就是这一轮的产出——不能显示成空。"""
        card = command_card([], ask="发给谁？")
        self.assertEqual(format_turn(self.Result(cards=[card])).text, "发给谁？")

    def test_it_falls_back_to_the_streamed_text_when_there_is_no_card(self) -> None:
        got = format_turn(self.Result(text="  在的，你说  "))
        self.assertEqual(got, DebugReply(text="在的，你说"))

    def test_an_unknown_shape_is_shown_raw_instead_of_claimed_empty(self) -> None:
        """★ 认不出来就把原文摆出来。说「AI 没有回复」是在替对面说它没说过的话。"""
        got = format_turn(self.Result(cards=[{"type": "info", "body": {"kind": "什么卡"}}]))
        self.assertEqual(got.text, "")
        self.assertIn("什么卡", got.raw)

    def test_long_arguments_are_trimmed_so_one_command_stays_one_line(self) -> None:
        line = summarize_command({"op": "post_moments", "args": {"content": "招" * 200}})
        self.assertTrue(line.endswith("…）"))
        self.assertLess(len(line), 120)

    def test_a_command_without_args_still_reads_as_a_command(self) -> None:
        self.assertEqual(summarize_command({"op": "check_new_friends"}), "check_new_friends")


if __name__ == "__main__":
    unittest.main()
