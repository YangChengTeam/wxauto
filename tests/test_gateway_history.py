"""把聊天记录传给 AI 端（用户 2026-08-20：「windows 端要把聊天消息传给 ai agent」）。

AI 端每一轮只收得到「这一趟去抖攒下的那几条」，而它要判的是**这一步走完没有**——
对方上周答应过来看看、专员前天已经问过工种，这些都在更早的轮次里。所以这台机器把
它**亲眼读到过**的消息留一份，下一轮当历史一并送上去。

两条不能含糊的边界：
- 历史与本轮**分两条**送（``history`` vs ``text``）。AI 端有一堆确定性判定只该看这一轮，
  历史并进去，三天前那句「我暂时不找了」会每轮重新收口一次。
- 本轮那几条要从历史里摘掉——它们已经在本轮上下文里，再送一遍就是同一句话出现两次。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import json
import tempfile
from pathlib import Path
from typing import Any

from fake_agent_ws import FakeAgentServer, TurnScript
from test_gateway_e2e import (
    ACCOUNT,
    PEER,
    T0,
    RecordingSink,
    ScriptedSource,
    inbound,
    wechat_card,
)

from wxclient.gateway import GatewayRunner, GatewaySettings, InboundMessage, LocalFileTicketProvider
from wxclient.gateway.inbound import format_history, format_transcript
from wxclient.gateway.sessions import GatewayStore


def _row(msg_id: str, text: str, *, at: datetime, from_self: bool = False, at_text: str = "") -> dict:
    return {
        "msg_id": msg_id,
        "text": text,
        "at": at.isoformat(),
        "at_text": at_text,
        "from_self": from_self,
    }


class MessageLogStoreTests(unittest.TestCase):
    """本机那份聊天记录：记得住、认得出重复、清得掉。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_records_both_sides_and_ignores_duplicates(self) -> None:
        rows = [
            _row("m1", "还招人吗", at=T0),
            _row("m2", "在的，你做过什么", at=T0 + timedelta(minutes=1), from_self=True),
        ]
        self.assertEqual(self.store.record_messages(ACCOUNT, PEER, rows), 2)
        # RPA 重复推同一条不能记两遍——否则历史里同一句话越攒越多。
        self.assertEqual(self.store.record_messages(ACCOUNT, PEER, rows), 0)
        got = self.store.recent_messages(ACCOUNT, PEER)
        self.assertEqual([r["text"] for r in got], ["还招人吗", "在的，你做过什么"])
        self.assertEqual([r["from_self"] for r in got], [False, True])

    def test_recent_is_oldest_first_and_capped(self) -> None:
        for i in range(10):
            self.store.record_messages(ACCOUNT, PEER, [_row(f"m{i}", f"第{i}句", at=T0 + timedelta(minutes=i))])
        got = self.store.recent_messages(ACCOUNT, PEER, limit=3)
        self.assertEqual([r["text"] for r in got], ["第7句", "第8句", "第9句"])

    def test_exclude_drops_this_turn_and_still_fills_the_window(self) -> None:
        """★ 摘掉本轮那几条之后，窗口要照样是满的——否则每轮能看到的历史越来越少。"""
        for i in range(10):
            self.store.record_messages(ACCOUNT, PEER, [_row(f"m{i}", f"第{i}句", at=T0 + timedelta(minutes=i))])
        got = self.store.recent_messages(ACCOUNT, PEER, limit=3, exclude=["m9", "m8"])
        self.assertEqual([r["text"] for r in got], ["第5句", "第6句", "第7句"])

    def test_purge_drops_only_the_stale_ones(self) -> None:
        self.store.record_messages(ACCOUNT, PEER, [_row("old", "很久以前", at=T0 - timedelta(days=30))])
        self.store.record_messages(ACCOUNT, PEER, [_row("new", "刚才", at=T0)])
        self.assertEqual(self.store.purge_messages_before(T0 - timedelta(days=14)), 1)
        self.assertEqual([r["text"] for r in self.store.recent_messages(ACCOUNT, PEER)], ["刚才"])

    def test_other_peers_are_not_mixed_in(self) -> None:
        """★ 串了人比没有历史更糟：AI 端会拿甲说过的话去回乙。"""
        self.store.record_messages(ACCOUNT, PEER, [_row("m1", "我是甲", at=T0)])
        self.store.record_messages(ACCOUNT, "另一个人", [_row("m2", "我是乙", at=T0)])
        self.assertEqual([r["text"] for r in self.store.recent_messages(ACCOUNT, PEER)], ["我是甲"])


class HistoryFormatTests(unittest.TestCase):
    """历史与本轮必须**逐字同形**：AI 端那头是同一个解析器。"""

    def test_same_shape_as_the_turn_transcript(self) -> None:
        message = InboundMessage(
            account_id=ACCOUNT, peer_id=PEER, msg_id="m1", text="还招人吗", at=T0, at_text="14:00"
        )
        rows = [_row("m1", "还招人吗", at=T0, at_text="14:00")]
        self.assertEqual(format_history(rows), format_transcript([message]))

    def test_speaker_and_time_are_spelled_out(self) -> None:
        rows = [
            _row("m1", "还招人吗", at=T0, at_text="昨天 20:11"),
            _row("m2", "在的", at=T0, from_self=True),   # 没有微信标的时间 → 回落到读到那一刻的时分
        ]
        self.assertEqual(
            format_history(rows).splitlines(),
            ["[对方 · 昨天 20:11] 还招人吗", "[专员本人 · 14:00] 在的"],
        )

    def test_blank_messages_are_dropped(self) -> None:
        """空行会被 AI 端那头并进上一条，把两句话糊成一句。"""
        self.assertEqual(format_history([_row("m1", "   ", at=T0)]), "")


class HistoryReachesTheAgentTests(unittest.IsolatedAsyncioTestCase):
    """整条路：第一轮说过的话，第二轮以 ``history`` 送上去。

    壳是照 `test_gateway_e2e` 那套搭的（真 runner / 真 store / 真 WebSocket，只有微信是假的），
    但**不继承**它——继承会把它那一整套 e2e 用例在这里再跑一遍。
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

    def _runner(self, server: FakeAgentServer, source: Any, sink: Any) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(
                ai_ws_url=server.url,
                local_ticket_file=self.tickets_file,
                debounce_seconds=3.0,
                turn_timeout_seconds=10.0,
                active_hours="00:00-23:59",
            ),
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

    def _send_frames(self, server: FakeAgentServer) -> list[dict]:
        return [f for f in server.received if f.get("op") == "message.send"]

    async def test_second_turn_carries_the_first_turn_as_history(self) -> None:
        script = TurnScript(cards=[wechat_card(["在的，你做过什么工种"])])
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            source = ScriptedSource([
                [inbound("还招人吗", msg_id="m1", at=T0)],
                [],
                [inbound("我做过焊工", msg_id="m2", at=T0 + timedelta(minutes=5))],
                [],
            ])
            runner = self._runner(server, source, RecordingSink())
            try:
                await self._advance_and_tick(runner, 0)
                await self._advance_and_tick(runner, 10)     # 第一轮
                await self._advance_and_tick(runner, 300)
                await self._advance_and_tick(runner, 10)     # 第二轮
            finally:
                await runner.stop()

            frames = self._send_frames(server)
            self.assertEqual(len(frames), 2)
            # 第一轮之前什么都没聊过 → 没有历史（不能凭空造一段）。
            self.assertEqual(frames[0]["data"].get("history", ""), "")
            history = frames[1]["data"]["history"]
            # 第一轮对方说的、以及我们回出去的，都在历史里。
            self.assertIn("[对方 · 14:00] 还招人吗", history)
            # ★ 本轮那条**不在**历史里——它已经在 text 里，再来一遍就是同一句话出现两次。
            self.assertNotIn("我做过焊工", history)
            self.assertIn("我做过焊工", frames[1]["data"]["text"])

    async def test_operator_own_words_survive_into_later_turns(self) -> None:
        """★ 专员自己手打的那几条，隔一轮之后仍然看得见。

        它们进不了待处理表（一批全是自己说的话不欠对方回复），原来只靠内存缓冲带一轮——
        带过那一轮就没了，进程重启更是直接消失。于是 AI 端过两轮又把专员刚问过的问一遍。
        现在它们和别的消息一样记在本机，往后每一轮都当历史送上去。
        """
        script = TurnScript(cards=[wechat_card(["好的"])])
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            source = ScriptedSource([
                [inbound("你在哪个区", msg_id="s1", at=T0, from_self=True)],
                [],
                [inbound("我在园区", msg_id="m1", at=T0 + timedelta(minutes=5))],
                [],
                [inbound("那边还招吗", msg_id="m2", at=T0 + timedelta(minutes=15))],
                [],
            ])
            runner = self._runner(server, source, RecordingSink())
            try:
                await self._advance_and_tick(runner, 0)
                await self._advance_and_tick(runner, 10)      # 专员自说自话那批不起轮
                await self._advance_and_tick(runner, 300)
                await self._advance_and_tick(runner, 10)      # 第一轮（专员那条随缓冲并进本轮）
                await self._advance_and_tick(runner, 600)
                await self._advance_and_tick(runner, 10)      # 第二轮
            finally:
                await runner.stop()

            frames = self._send_frames(server)
            self.assertEqual(len(frames), 2)
            # 第一轮：专员那条走的是**内存缓冲**，在本轮上下文里（所以不该重复出现在历史里）。
            self.assertIn("你在哪个区", frames[0]["data"]["text"])
            self.assertNotIn("你在哪个区", frames[0]["data"].get("history", ""))
            # ★ 第二轮：缓冲早清了，靠本机这份记录才还看得见。
            self.assertIn("[专员本人 · 14:00] 你在哪个区", frames[1]["data"]["history"])
            self.assertIn("我在园区", frames[1]["data"]["history"])


if __name__ == "__main__":
    unittest.main()
