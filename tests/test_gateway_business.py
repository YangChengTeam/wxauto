"""网关业务层测试（Phase B）——去重/去抖/串行/守门/出站，全部在 Linux 跑，不碰真微信。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wxclient.gateway import (
    Debouncer,
    GatewayStore,
    InboundLoop,
    InboundMessage,
    OutboundGuard,
    OutboundSender,
    extract_reply,
    format_transcript,
    looks_like_rejection,
    within_active_hours,
)

T0 = datetime(2026, 8, 10, 14, 0, 0, tzinfo=timezone.utc)


def msg(
    text: str,
    *,
    msg_id: str = "m1",
    peer: str = "peer-1",
    account: str = "acct-1",
    at: datetime | None = None,
    from_self: bool = False,
) -> InboundMessage:
    return InboundMessage(
        account_id=account,
        peer_id=peer,
        msg_id=msg_id,
        text=text,
        at=at or T0,
        from_self=from_self,
    )


class FakeSource:
    """Fake 微信驱动：按 tick 顺序吐出预设批次。"""

    def __init__(self, batches: list[list[InboundMessage]]) -> None:
        self._batches = list(batches)
        self.calls = 0

    async def fetch(self, account_id: str) -> list[InboundMessage]:
        self.calls += 1
        if not self._batches:
            return []
        return [m for m in self._batches.pop(0) if m.account_id == account_id]


class FakeSink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, account_id: str, peer_id: str, text: str) -> str:
        self.sent.append((account_id, peer_id, text))
        return f"task-{len(self.sent)}"


def reply_card(segments: list[str], *, actions: list[dict[str, Any]] | None = None,
               no_reply_reason: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "wechat_reply",
        "reply_segments": segments,
        "intent": "consult",
        "funnel_stage": "intent_check",
        "actions": actions or [],
    }
    if no_reply_reason is not None:
        body["no_reply_reason"] = no_reply_reason
    return {"type": "info", "body": body}


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ────────────────────────── 会话映射持久化 ──────────────────────────


class SessionStoreTests(StoreTestCase):
    async def test_session_roundtrip_survives_reopen(self) -> None:
        """进程重启后映射必须还在——丢了就等于把聊了三天的人当陌生人。"""
        path = Path(self._tmp.name) / "gateway.db"
        self.store.put_session("acct-1", "peer-1", "sess-1")

        reopened = GatewayStore(path)
        self.assertEqual(reopened.get_session("acct-1", "peer-1"), "sess-1")

    async def test_session_is_scoped_per_account_and_peer(self) -> None:
        self.store.put_session("acct-1", "peer-1", "sess-1")
        self.assertIsNone(self.store.get_session("acct-1", "peer-2"))
        self.assertIsNone(self.store.get_session("acct-2", "peer-1"))

    async def test_put_session_overwrites(self) -> None:
        self.store.put_session("acct-1", "peer-1", "sess-1")
        self.store.put_session("acct-1", "peer-1", "sess-2")
        self.assertEqual(self.store.get_session("acct-1", "peer-1"), "sess-2")

    async def test_drop_session(self) -> None:
        self.store.put_session("acct-1", "peer-1", "sess-1")
        self.store.drop_session("acct-1", "peer-1")
        self.assertIsNone(self.store.get_session("acct-1", "peer-1"))

    async def test_dedup_cursor(self) -> None:
        self.assertEqual(self.store.filter_unseen("a", "p", ["m1", "m2"]), ["m1", "m2"])
        self.store.mark_seen("a", "p", ["m1"])
        self.assertEqual(self.store.filter_unseen("a", "p", ["m1", "m2"]), ["m2"])

    async def test_send_counter_windows_by_hour(self) -> None:
        now = T0
        self.store.record_send("a", "p", now - timedelta(minutes=30))
        self.store.record_send("a", "p", now - timedelta(minutes=90))
        self.assertEqual(self.store.sends_in_last_hour("a", now), 1)


# ────────────────────────── 去抖与转写 ──────────────────────────


class DebouncerTests(unittest.TestCase):
    def test_merges_burst_into_one_batch(self) -> None:
        """连发三条只应产生一个批次——否则会回三次，像机器人。"""
        d = Debouncer(window_seconds=3)
        d.add(msg("在吗", msg_id="m1"), T0)
        d.add(msg("还招人吗", msg_id="m2"), T0 + timedelta(seconds=1))
        d.add(msg("我做过普工", msg_id="m3"), T0 + timedelta(seconds=2))

        self.assertEqual(d.take_due(T0 + timedelta(seconds=3)), [])  # 距最后一条才 1s
        due = d.take_due(T0 + timedelta(seconds=5))
        self.assertEqual(len(due), 1)
        self.assertEqual([m.msg_id for m in due[0][1]], ["m1", "m2", "m3"])

    def test_separate_peers_do_not_merge(self) -> None:
        d = Debouncer(window_seconds=3)
        d.add(msg("甲", msg_id="m1", peer="p1"), T0)
        d.add(msg("乙", msg_id="m2", peer="p2"), T0)
        due = dict(d.take_due(T0 + timedelta(seconds=5)))
        self.assertEqual(len(due), 2)

    def test_take_due_clears_buffer(self) -> None:
        d = Debouncer(window_seconds=1)
        d.add(msg("x"), T0)
        self.assertEqual(len(d.take_due(T0 + timedelta(seconds=2))), 1)
        self.assertEqual(d.take_due(T0 + timedelta(seconds=9)), [])


class TranscriptTests(unittest.TestCase):
    def test_marks_speaker_and_hides_wxid(self) -> None:
        """必须能分清对方与专员本人，且不泄露 wxid。"""
        text = format_transcript([
            msg("还招人吗", msg_id="m1", at=T0),
            msg("稍等我看下", msg_id="m2", at=T0 + timedelta(minutes=2), from_self=True),
        ])
        self.assertEqual(text, "[对方 · 14:00] 还招人吗\n[专员本人 · 14:02] 稍等我看下")
        self.assertNotIn("peer-1", text)


# ────────────────────────── 确定性守门 ──────────────────────────


class RejectionTests(unittest.TestCase):
    def test_detects_rejections(self) -> None:
        for text in [
            "别加我了",
            "不要再联系我",
            "别烦我",
            "把我删了",
            "再发我就举报你",
            "退订",
            "stop",
        ]:
            self.assertTrue(looks_like_rejection(text), text)

    def test_does_not_flag_normal_messages(self) -> None:
        for text in [
            "还招人吗",
            "我想了解一下这个岗位",
            "工资怎么算",
            "不需要经验吗？",       # 疑问句里的否定不是拒绝
            "在哪里上班",
            "",
        ]:
            self.assertFalse(looks_like_rejection(text), text)

    def test_soft_close_is_not_a_hard_rejection(self) -> None:
        """★「已经找到工作了」不是硬拒绝——这道闸拦下就等于永久拉黑一个以后还要触达的人。

        它归 AI 端处置（礼貌收尾、漏斗标 closed、不提案拉黑），所以必须能走到派发这一步。
        """
        for text in ["我已经找到工作了", "暂时没兴趣", "现在不找工作了"]:
            self.assertFalse(looks_like_rejection(text), text)


class ActiveHoursTests(unittest.TestCase):
    def test_normal_window(self) -> None:
        window = "09:00-21:00"
        self.assertTrue(within_active_hours(datetime(2026, 8, 10, 14, 0), window))
        self.assertFalse(within_active_hours(datetime(2026, 8, 10, 7, 0), window))
        self.assertFalse(within_active_hours(datetime(2026, 8, 10, 23, 0), window))

    def test_window_crossing_midnight(self) -> None:
        window = "22:00-02:00"
        self.assertTrue(within_active_hours(datetime(2026, 8, 10, 23, 0), window))
        self.assertTrue(within_active_hours(datetime(2026, 8, 10, 1, 0), window))
        self.assertFalse(within_active_hours(datetime(2026, 8, 10, 12, 0), window))

    def test_bad_format_rejected(self) -> None:
        with self.assertRaises(ValueError):
            within_active_hours(datetime(2026, 8, 10, 12, 0), "早上到晚上")


class GuardTests(StoreTestCase):
    def _guard(self, **kwargs: Any) -> OutboundGuard:
        return OutboundGuard(self.store, **kwargs)

    async def test_allows_normal(self) -> None:
        self.assertTrue(self._guard().check("a", "p", now=datetime(2026, 8, 10, 14, 0)).allowed)

    async def test_blocks_blacklisted(self) -> None:
        self.store.blacklist("a", "p", "说了别联系")
        decision = self._guard().check("a", "p", now=datetime(2026, 8, 10, 14, 0))
        self.assertFalse(decision.allowed)
        self.assertIn("黑名单", decision.reason)

    async def test_blocks_outside_active_hours(self) -> None:
        decision = self._guard().check("a", "p", now=datetime(2026, 8, 10, 3, 0))
        self.assertFalse(decision.allowed)

    async def test_blocks_over_rate_limit(self) -> None:
        now = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        guard = self._guard(max_sends_per_hour=2)
        self.store.record_send("a", "p", now - timedelta(minutes=1))
        self.store.record_send("a", "p", now - timedelta(minutes=2))
        decision = guard.check("a", "p", now=now)
        self.assertFalse(decision.allowed)
        self.assertIn("上限", decision.reason)


# ────────────────────────── 出站 ──────────────────────────


class ExtractReplyTests(unittest.TestCase):
    def test_extracts_segments_and_actions(self) -> None:
        segments, actions, reason = extract_reply(
            reply_card(["你好", "在哪个区"], actions=[{"type": "upsert_lead"}])
        )
        self.assertEqual(segments, ["你好", "在哪个区"])
        self.assertEqual(actions, [{"type": "upsert_lead"}])
        self.assertEqual(reason, "")

    def test_empty_segments_yield_reason(self) -> None:
        _, _, reason = extract_reply(reply_card([], no_reply_reason="对方只发了表情"))
        self.assertEqual(reason, "对方只发了表情")

    def test_notice_card_is_not_sent(self) -> None:
        segments, _, reason = extract_reply(
            {"type": "info", "body": {"kind": "notice", "note": "当前没有在招岗位"}}
        )
        self.assertEqual(segments, [])
        self.assertIn("当前没有在招岗位", reason)

    def test_malformed_card_never_guesses(self) -> None:
        for card in [None, {}, {"body": "x"}, {"body": {"kind": "other"}}]:
            segments, _, reason = extract_reply(card)  # type: ignore[arg-type]
            self.assertEqual(segments, [])
            self.assertTrue(reason)

    def test_blank_segments_dropped(self) -> None:
        segments, _, _ = extract_reply(reply_card(["你好", "   ", ""]))
        self.assertEqual(segments, ["你好"])


class OutboundSenderTests(StoreTestCase):
    def _sender(self, sink: FakeSink, **guard_kwargs: Any) -> OutboundSender:
        now = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        return OutboundSender(
            sink=sink,
            guard=OutboundGuard(self.store, **guard_kwargs),
            store=self.store,
            clock=lambda: now,
        )

    async def test_sends_each_segment_as_one_message(self) -> None:
        sink = FakeSink()
        result = await self._sender(sink).deliver("a", "p", reply_card(["第一句", "第二句"]))
        self.assertTrue(result.delivered)
        self.assertEqual([text for _, _, text in sink.sent], ["第一句", "第二句"])
        self.assertEqual(self.store.sends_in_last_hour("a", datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)), 2)

    async def test_empty_reply_sends_nothing(self) -> None:
        sink = FakeSink()
        result = await self._sender(sink).deliver(
            "a", "p", reply_card([], no_reply_reason="对方只发了表情")
        )
        self.assertFalse(result.delivered)
        self.assertEqual(sink.sent, [])
        self.assertEqual(result.skipped_reason, "对方只发了表情")

    async def test_blacklist_action_applied_even_when_not_replying(self) -> None:
        """AI 端判定拒绝 → 即使这一轮不发消息，拉黑也必须立刻生效。"""
        sink = FakeSink()
        await self._sender(sink).deliver(
            "a", "p",
            reply_card([], no_reply_reason="对方拒绝", actions=[{"type": "blacklist", "reason": "明确拒绝"}]),
        )
        self.assertTrue(self.store.is_blacklisted("a", "p"))

    async def test_guard_blocks_send(self) -> None:
        sink = FakeSink()
        self.store.blacklist("a", "p", "先前已拒绝")
        result = await self._sender(sink).deliver("a", "p", reply_card(["你好"]))
        self.assertFalse(result.delivered)
        self.assertEqual(sink.sent, [])

    async def test_rate_limit_blocks_send(self) -> None:
        sink = FakeSink()
        sender = self._sender(sink, max_sends_per_hour=1)
        self.store.record_send("a", "p", datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc))
        result = await sender.deliver("a", "p", reply_card(["你好"]))
        self.assertFalse(result.delivered)
        self.assertIn("上限", result.skipped_reason)


# ────────────────────────── 入站主循环（整条回路）──────────────────────────


class InboundLoopTests(StoreTestCase):
    def _loop(self, source: FakeSource, handler, *, now: datetime, debounce: float = 3.0) -> InboundLoop:
        self._now = now
        return InboundLoop(
            accounts=["acct-1"],
            source=source,
            store=self.store,
            handler=handler,
            debounce_seconds=debounce,
            clock=lambda: self._now,
        )

    async def test_burst_becomes_one_turn(self) -> None:
        seen: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            seen.append(transcript)

        source = FakeSource([
            [msg("在吗", msg_id="m1"), msg("还招人吗", msg_id="m2")],
            [],
        ])
        loop = self._loop(source, handler, now=T0)
        self.assertEqual(await loop.tick(), 0)      # 还在去抖窗口内
        self._now = T0 + timedelta(seconds=10)
        self.assertEqual(await loop.tick(), 1)      # 静默够久 → 一个批次
        await loop.drain()
        self.assertEqual(len(seen), 1)
        self.assertIn("在吗", seen[0])
        self.assertIn("还招人吗", seen[0])

    async def test_duplicate_msg_id_triggers_only_once(self) -> None:
        """RPA 重推同一条消息不能触发第二轮。"""
        calls: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            calls.append(transcript)

        source = FakeSource([
            [msg("还招人吗", msg_id="m1")],
            [msg("还招人吗", msg_id="m1")],   # 同一条重推
        ])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        self._now = T0 + timedelta(seconds=20)
        await loop.tick()
        await loop.drain()
        self.assertEqual(len(calls), 1)

    async def test_rejection_blacklists_and_dispatches_nothing(self) -> None:
        """确定性拒绝闸：命中即拉黑，连告别语都不发（§7 的字面要求）。"""
        calls: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            calls.append(transcript)

        source = FakeSource([[msg("别再联系我了", msg_id="m1")], []])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        await loop.drain()

        self.assertEqual(calls, [])
        self.assertTrue(self.store.is_blacklisted("acct-1", "peer-1"))

    async def test_blacklisted_peer_is_never_processed(self) -> None:
        calls: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            calls.append(transcript)

        self.store.blacklist("acct-1", "peer-1", "先前已拒绝")
        source = FakeSource([[msg("还招人吗", msg_id="m1")], []])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        await loop.drain()
        self.assertEqual(calls, [])

    async def test_same_peer_is_strictly_serial(self) -> None:
        """一轮没收口，第二批必须等着——AI 端同 session 也只允许一轮。"""
        started = asyncio.Event()
        release = asyncio.Event()
        transcripts: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            transcripts.append(transcript)
            started.set()
            await release.wait()

        # 空批次代表「这一轮轮询没有新消息」——新消息会重置去抖窗口，所以两条要分开到达。
        source = FakeSource([
            [msg("第一条", msg_id="m1")],
            [],
            [msg("第二条", msg_id="m2")],
            [],
            [],
        ])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()                                    # 收到第一条，攒着
        self._now = T0 + timedelta(seconds=10)
        self.assertEqual(await loop.tick(), 1)               # 静默够久 → 派发第一轮
        await started.wait()

        self._now = T0 + timedelta(seconds=20)
        await loop.tick()                                    # 第二条到达，攒着

        # 第二条已经静默够久，但第一轮还没收口 → 不许派发，放回缓冲继续攒
        self._now = T0 + timedelta(seconds=30)
        self.assertEqual(await loop.tick(), 0)
        self.assertEqual(len(transcripts), 1)

        release.set()
        await loop.drain()

        # 第一轮收口后，攒下的那批才派发出去
        self._now = T0 + timedelta(seconds=50)
        self.assertEqual(await loop.tick(), 1)
        await loop.drain()
        self.assertEqual(len(transcripts), 2)
        self.assertIn("第二条", transcripts[1])

    async def test_different_peers_run_concurrently(self) -> None:
        running = 0
        peak = 0
        release = asyncio.Event()

        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await release.wait()
            running -= 1

        source = FakeSource([[
            msg("甲", msg_id="m1", peer="peer-1"),
            msg("乙", msg_id="m2", peer="peer-2"),
        ], []])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        self.assertEqual(await loop.tick(), 2)
        await asyncio.sleep(0)
        release.set()
        await loop.drain()
        self.assertEqual(peak, 2)

    async def test_handler_failure_does_not_break_loop(self) -> None:
        calls = 0

        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("AI 端挂了")

        source = FakeSource([[msg("一", msg_id="m1")], [], [msg("二", msg_id="m2")], []])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()                                    # 攒第一条
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()                                    # 派发第一轮 → handler 抛异常
        await loop.drain()
        self._now = T0 + timedelta(seconds=20)
        await loop.tick()                                    # 攒第二条
        self._now = T0 + timedelta(seconds=30)
        await loop.tick()                                    # 派发第二轮：上一轮炸了不影响
        await loop.drain()
        self.assertEqual(calls, 2)

    async def test_source_failure_is_survived(self) -> None:
        class BoomSource:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch(self, account_id: str):
                self.calls += 1
                raise OSError("微信没开")

        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            raise AssertionError("不该被调用")

        source = BoomSource()
        loop = self._loop(source, handler, now=T0)  # type: ignore[arg-type]
        await loop.tick()
        await loop.tick()
        self.assertEqual(source.calls, 2)

    async def test_operator_message_is_included(self) -> None:
        """专员本人手打的消息必须一起喂给 AI，否则它会重复发问。"""
        transcripts: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            transcripts.append(transcript)

        source = FakeSource([[
            msg("还招人吗", msg_id="m1"),
            msg("稍等我看下", msg_id="m2", from_self=True),
        ], []])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        await loop.drain()
        self.assertIn("[专员本人", transcripts[0])
        self.assertIn("[对方", transcripts[0])


if __name__ == "__main__":
    unittest.main()
