"""网关业务层测试（Phase B）——去重/去抖/串行/守门/出站，全部在 Linux 跑，不碰真微信。"""

from __future__ import annotations

import asyncio
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from structlog.testing import capture_logs

from wxclient.gateway import (
    Debouncer,
    FriendWatcher,
    GatewayStore,
    InboundLoop,
    InboundMessage,
    OutboundGuard,
    OutboundSender,
    Watchlist,
    clamp_interval,
    extract_reply,
    format_transcript,
    jittered,
    looks_like_rejection,
    within_active_hours,
)
from wxclient.gateway.inbound import PENDING_MAX_AGE_HOURS, PENDING_MAX_ATTEMPTS
from wxclient.gateway.wechat_io import (
    QueueMessageSource,
    legacy_message_signature,
    message_signature,
    split_new_messages,
)
from wxclient.queue import TaskType

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
        due = {key: messages for key, messages, _batch_ids in d.take_due(T0 + timedelta(seconds=5))}
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
        # 时间窗给满 24 小时：这条测的是频次上限，不该因为 14:00 UTC 在本机是几点而飘。
        guard = self._guard(max_sends_per_hour=2, active_hours="00:00-23:59")
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
        # 时间窗默认给满：这些用例测的是出站本身，T0 换算到本机是几点不该左右结论。
        guard_kwargs.setdefault("active_hours", "00:00-23:59")
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

    async def test_reading_takes_time_so_the_same_tick_can_dispatch(self) -> None:
        """★ 读消息本身要花好几秒，读完就该派发，不许白等下一趟轮询。

        真机上 `_collect` 逐个会话开窗读记录，一趟 5~15 秒；去抖窗口只有 3 秒。
        原来 `tick()` 拿**读之前**取的 now 去问「静默够久了吗」，答案恒为 0 秒，
        于是本趟收到的消息必然被推到下一趟——轮询间隔 240s，每条回复白等一个周期
        （2026-08-13 真机：21:25:39 收到，21:26:43 / 21:27:58 两趟都没派发）。
        """
        seen: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            seen.append(transcript)

        class SlowSource:
            """模拟真机：读一趟微信要花 8 秒（逐个会话开窗读记录）。"""

            def __init__(self, advance) -> None:
                self._advance = advance
                self._done = False

            async def fetch(self, account_id: str) -> list[InboundMessage]:
                self._advance(8)
                if self._done:
                    return []
                self._done = True
                return [msg("在吗", msg_id="m1")]

        def advance(seconds: int) -> None:
            self._now = self._now + timedelta(seconds=seconds)

        loop = self._loop(SlowSource(advance), handler, now=T0)
        # 读完已经过去 8 秒 > 去抖窗口 3 秒 → **本趟**就该派发，而不是等下一趟。
        self.assertEqual(await loop.tick(), 1)
        await loop.drain()
        self.assertEqual(len(seen), 1)

    async def test_debounce_still_holds_a_burst_together(self) -> None:
        """上面那条不许把去抖弄失效：读得快的时候，连发的消息仍要合成一轮。"""
        seen: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            seen.append(transcript)

        source = FakeSource([
            [msg("在吗", msg_id="m1")],
            [msg("还招人吗", msg_id="m2")],
            [],
        ])
        loop = self._loop(source, handler, now=T0)
        self.assertEqual(await loop.tick(), 0)        # 读得快（时钟没走）→ 还在窗口内
        self._now = T0 + timedelta(seconds=1)
        self.assertEqual(await loop.tick(), 0)        # 第二条又把窗口顶开
        self._now = T0 + timedelta(seconds=10)
        self.assertEqual(await loop.tick(), 1)        # 静默够久 → 合成一轮
        await loop.drain()
        self.assertEqual(len(seen), 1)
        self.assertIn("在吗", seen[0])
        self.assertIn("还招人吗", seen[0])

    async def test_cancel_inflight_gives_up_the_running_turn(self) -> None:
        """★ 退出时不能被一轮没跑完的对话拖住——那一轮最长能占 180s。"""
        started = asyncio.Event()

        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            started.set()
            await asyncio.sleep(3600)   # 模拟卡在一轮里

        source = FakeSource([[msg("在吗", msg_id="m1")], []])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        self.assertEqual(await loop.tick(), 1)
        await started.wait()

        self.assertEqual(loop.cancel_inflight(), 1)
        await loop.drain()               # 放弃之后 drain 立即返回，不再等

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
        await loop.tick()                                    # 攒第二条（顺带把炸掉那批捞回来）
        self._now = T0 + timedelta(seconds=30)
        await loop.tick()                                    # 派发第二轮：上一轮炸了不影响
        await loop.drain()
        self.assertGreaterEqual(calls, 2)                    # 循环没被一次异常带停
        # 两批都还欠着（handler 一直在炸），一条都不许被悄悄丢掉。
        self.assertEqual(len(self.store.list_pending_inbound()), 2)


# ────────────────────────── 入站不丢消息（070）──────────────────────────


class PendingInboundTests(StoreTestCase):
    """★ 游标在**读到消息那一刻**就推进了，所以「读到」和「处理完」之间断掉任何一环，
    这批消息就同时满足「游标翻过去了」和「谁也没处理」——候选人永远等不到回复，
    日志上只有一行 warning（2026-08-13 真机，票据过期那段窗口就是这么静悄悄丢的）。
    """

    def _loop(self, source, handler, *, now: datetime, debounce: float = 3.0) -> InboundLoop:
        self._now = now
        return InboundLoop(
            accounts=["acct-1"],
            source=source,
            store=self.store,
            handler=handler,
            debounce_seconds=debounce,
            clock=lambda: self._now,
        )

    async def test_success_leaves_no_residue(self) -> None:
        """跑通的批次必须销账，否则队列会越积越多、每趟都重投一遍已经回过的消息。"""
        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            return None

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")], []]), handler, now=T0)
        await loop.tick()
        self.assertEqual(len(self.store.list_pending_inbound()), 1)   # 入缓冲即落盘
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        await loop.drain()
        self.assertEqual(self.store.list_pending_inbound(), [])

    async def test_ai_outage_is_retried_until_it_lands(self) -> None:
        """★ AI 端断线期间的消息不许丢：恢复之后要自己补上。"""
        delivered: list[str] = []
        online = False

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            if not online:
                raise RuntimeError("AI 端连不上")
            delivered.append(transcript)

        loop = self._loop(FakeSource([[msg("还招人吗", msg_id="m1")], [], [], []]), handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()                        # 派发 → 断线 → 失败
        await loop.drain()
        self.assertEqual(delivered, [])
        self.assertEqual(len(self.store.list_pending_inbound()), 1)   # 还欠着，没丢

        online = True                            # AI 端恢复
        self._now = T0 + timedelta(seconds=20)
        await loop.tick()                        # 重投
        await loop.drain()
        self.assertEqual(len(delivered), 1)
        self.assertIn("还招人吗", delivered[0])
        self.assertEqual(self.store.list_pending_inbound(), [])

    async def test_restart_replays_what_was_never_delivered(self) -> None:
        """★ 进程被关掉/崩掉之后重新起来，欠的账要自己补上——微信那边已经是已读了。"""
        async def boom(_a: str, _p: str, _t: str, _m) -> None:
            raise RuntimeError("还没连上")

        first = self._loop(FakeSource([[msg("我想应聘", msg_id="m1")], []]), boom, now=T0)
        await first.tick()
        self._now = T0 + timedelta(seconds=10)
        await first.tick()
        await first.drain()
        self.assertEqual(len(self.store.list_pending_inbound()), 1)

        # ——— 进程重启：全新的 loop，全新的内存缓冲，来源里一条新消息都没有 ———
        delivered: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            delivered.append(transcript)

        second = self._loop(FakeSource([[], []]), handler, now=T0 + timedelta(seconds=30))
        await second.tick()
        await second.drain()
        self.assertEqual(len(delivered), 1)
        self.assertIn("我想应聘", delivered[0])

    async def test_stale_batch_is_dropped_with_a_trace(self) -> None:
        """队列是短期的：隔了一天再回过去更奇怪。但丢弃必须留痕，不能又变成静悄悄丢消息。"""
        async def boom(_a: str, _p: str, _t: str, _m) -> None:
            raise RuntimeError("AI 端连不上")

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")], [], []]), boom, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        await loop.drain()
        self.assertEqual(len(self.store.list_pending_inbound()), 1)

        self._now = T0 + timedelta(hours=PENDING_MAX_AGE_HOURS + 1)
        with capture_logs() as logs:
            await loop.tick()
        self.assertEqual(self.store.list_pending_inbound(), [])
        # 丢弃必须喊出来——静悄悄丢消息正是这一整块要根治的毛病。
        self.assertTrue(
            any(entry["log_level"] == "warning" and "保鲜期" in entry["event"] for entry in logs),
            logs,
        )

    async def test_gives_up_after_max_attempts(self) -> None:
        """一直失败的那一批不能无限重投，否则每趟都在原地打转。"""
        self.store.put_pending_inbound(
            "acct-1", "peer-1", "pi_dead",
            [{"msg_id": "m1", "text": "在吗", "at": T0.isoformat(), "from_self": False}],
        )
        for _ in range(PENDING_MAX_ATTEMPTS):
            self.store.bump_pending_attempts("pi_dead")

        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            raise AssertionError("不该再被投出去")

        loop = self._loop(FakeSource([[], []]), handler, now=T0 + timedelta(seconds=30))
        with capture_logs() as logs:
            await loop.tick()
        await loop.drain()
        self.assertEqual(self.store.list_pending_inbound(), [])
        self.assertTrue(
            any(entry["log_level"] == "warning" and "已放弃" in entry["event"] for entry in logs),
            logs,
        )

    async def test_delivery_failure_does_not_retry(self) -> None:
        """★ 已经开始往微信发了才失败 → **不许重投**。

        重投的代价是「已经发出去的那几段再发一遍」，候选人收到重复消息且撤不回来
        （需求文档 §12.4：结果未知先查询或转人工，不能盲目重发）。
        与之相对，对话轮失败时一条微信都还没发，那才是该重投的场景。
        """
        from wxclient.gateway.guard import OutboundGuard
        from wxclient.gateway.outbound import OutboundSender

        class BoomSink:
            async def send(self, account_id: str, peer_id: str, text: str) -> str:
                raise OSError("微信窗口没了")

        sender = OutboundSender(
            sink=BoomSink(),
            guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
            store=self.store,
            clock=lambda: self._now,
        )

        async def handler(account_id: str, peer_id: str, _t: str, _m) -> None:
            # 对话轮当作已经跑通（拿到卡），失败发生在投递阶段——照搬 runner.handle_turn 的边界。
            try:
                await sender.deliver(account_id, peer_id, reply_card(["您好"]))
            except Exception:  # noqa: BLE001
                return  # runner 在这里只记账、不上抛

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")], []]), handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        await loop.drain()
        # 销账了：不会在下一趟把同一批再送一遍。
        self.assertEqual(self.store.list_pending_inbound(), [])

    async def test_self_only_batch_keeps_its_receipt(self) -> None:
        """全是专员本人说的话不触发一轮，但那一批仍欠在账上——对方回话时要一起送上去。"""
        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            raise AssertionError("不该被触发")

        loop = self._loop(
            FakeSource([[msg("我先问问他", msg_id="m1", from_self=True)], []]), handler, now=T0
        )
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        self.assertEqual(await loop.tick(), 0)
        await loop.drain()
        self.assertEqual(len(self.store.list_pending_inbound()), 1)

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


class FakeRuntime:
    """只回放事先摆好的未读列表与聊天记录，并记下都调了什么。"""

    def __init__(self, unread: list[dict], history: dict[str, list[dict]]) -> None:
        self.unread = unread
        self.history = history
        self.calls: list[tuple[Any, Any]] = []

    async def submit(self, task_type, payload=None, **_kw):
        self.calls.append((task_type, payload))
        if task_type is TaskType.GET_UNREAD_MESSAGES:
            return {"result": {"unread_sessions": self.unread}}
        friend = (payload or {}).get("friend")
        return {"result": {"messages": self.history.get(friend, [])}}

    def opened(self) -> list[str]:
        """被真正打开读过历史的会话名。打开窗口本身就是打扰，所以要能断言。"""
        return [
            (payload or {}).get("friend")
            for task_type, payload in self.calls
            if task_type is TaskType.READ_CHAT_HISTORY
        ]


class InboundScopeTests(StoreTestCase):
    """哪些会话轮得到网关管（2026-08-12：真机上有个群在闪未读，差点被自动回）。"""

    GROUP = "某某招聘交流群"
    PERSON = "候选人-王建国"

    def _runtime(self) -> FakeRuntime:
        return FakeRuntime(
            unread=[{"name": self.GROUP, "unread_count": 3}, {"name": self.PERSON, "unread_count": 1}],
            history={
                self.GROUP: [{"text": "群里在聊天", "is_group": True, "is_self": False}],
                self.PERSON: [{"text": "还招人吗", "is_group": False, "is_self": False}],
            },
        )

    async def test_group_is_skipped_and_remembered(self) -> None:
        runtime = self._runtime()
        source = QueueMessageSource(runtime=runtime, store=self.store)

        await source.fetch("acct-1")
        self.assertTrue(self.store.is_known_group("acct-1", self.GROUP))
        self.assertIn(self.GROUP, runtime.opened(), "第一次判群必须打开一次才知道")

        runtime.calls.clear()
        await source.fetch("acct-1")
        self.assertNotIn(self.GROUP, runtime.opened(), "判过一次之后连开都不该再开")

    async def test_group_allowed_when_explicitly_turned_on(self) -> None:
        runtime = self._runtime()
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(allow_groups=True)
        )
        await source.fetch("acct-1")          # 首次只对齐游标
        messages = await source.fetch("acct-1")
        self.assertEqual(messages, [], "首次对齐游标后没有新消息，但群没被拦下")
        self.assertIn(self.GROUP, runtime.opened())

    async def test_denylist_wins_and_never_opens_the_chat(self) -> None:
        runtime = self._runtime()
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(never=[self.PERSON])
        )
        await source.fetch("acct-1")
        self.assertNotIn(self.PERSON, runtime.opened())

    async def test_watchlist_limits_to_named_peers(self) -> None:
        runtime = self._runtime()
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.PERSON])
        )
        await source.fetch("acct-1")
        self.assertEqual(runtime.opened(), [self.PERSON])

    async def test_push_replaces_watchlist_live(self) -> None:
        """AI 端推一份名单下来，下一趟就该照新名单走——不用重启网关。"""
        runtime = self._runtime()
        watchlist = Watchlist.from_local(only=[self.PERSON])
        source = QueueMessageSource(runtime=runtime, store=self.store, watchlist=watchlist)
        await source.fetch("acct-1")
        self.assertEqual(runtime.opened(), [self.PERSON])

        self.assertTrue(watchlist.apply_push({"only": [], "never": [self.PERSON]}))
        runtime.calls.clear()
        await source.fetch("acct-1")
        self.assertNotIn(self.PERSON, runtime.opened())

    async def test_push_ignores_garbage_and_keeps_current_scope(self) -> None:
        """坏帧不能把名单清空——清空 = 网关开始回它本不该回的人。"""
        watchlist = Watchlist.from_local(only=[self.PERSON])
        self.assertFalse(watchlist.apply_push({"only": "王建国"}))
        self.assertFalse(watchlist.apply_push({}))
        self.assertEqual(watchlist.scope.only, frozenset({self.PERSON}))
        self.assertIsNone(watchlist.reason(self.PERSON, is_group=False))
        self.assertIsNotNone(watchlist.reason("路人甲", is_group=False))


# ────────────────────────── 节拍（防风控）──────────────────────────


class PacingTests(unittest.TestCase):
    """2026-08-12 真机事故：跑了一天后微信自己退了登录，判定触发风控。"""

    def test_interval_clamped_to_floor(self) -> None:
        self.assertEqual(clamp_interval(5.0, 30.0, what="读消息间隔"), 30.0)
        self.assertEqual(clamp_interval(60.0, 30.0, what="读消息间隔"), 60.0)

    def test_jitter_is_bounded_and_not_a_metronome(self) -> None:
        rng = random.Random(7)
        values = [jittered(60.0, rng=rng) for _ in range(50)]
        self.assertTrue(all(41.9 <= v <= 78.1 for v in values), values[:3])
        self.assertGreater(len(set(values)), 40, "间隔还是整齐的，等于没抖")


class QuietHoursTests(StoreTestCase):
    """非工作时段**一个微信动作都不做**——连读都不读，不只是不发。"""

    async def test_loop_does_not_touch_wechat_when_inactive(self) -> None:
        source = FakeSource([[msg("在吗", msg_id="m1", at=T0)]])

        async def handler(*_args: Any) -> None:
            raise AssertionError("非工作时段不该派发任何一轮")

        loop = InboundLoop(
            accounts=["acct-1"],
            source=source,
            store=self.store,
            handler=handler,
            is_active=lambda _now: False,
            clock=lambda: T0,
        )
        self.assertEqual(await loop.tick(), 0)
        self.assertEqual(source.calls, 0, "非工作时段还去读了微信")

    async def test_uses_local_wall_clock_not_utc(self) -> None:
        """时间窗说的是专员那台机器上的九点到九点。

        原来直接拿 UTC 的 .time() 比，东八区就成了 17:00–次日 05:00：白天不发、半夜猛发。
        """
        beijing = timezone(timedelta(hours=8))
        noon = datetime(2026, 8, 10, 12, 0, tzinfo=beijing)      # 北京中午 = 04:00 UTC
        midnight = datetime(2026, 8, 10, 3, 0, tzinfo=beijing)   # 北京凌晨三点 = 19:00 UTC
        self.assertTrue(within_active_hours(noon, "09:00-21:00"))
        self.assertFalse(within_active_hours(midnight, "09:00-21:00"))


class FriendWatcherTests(unittest.IsolatedAsyncioTestCase):
    """加好友走自己的节拍，且只看不通过（用户 2026-08-13 要求）。"""

    class Runtime:
        def __init__(self, items: list[str] | None = None) -> None:
            self.calls: list[tuple[Any, dict]] = []
            self._items = items or []

        async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
            self.calls.append((task_type, payload or {}, confirmation))
            return {"result": {"items": list(self._items)}}

    def _watcher(self, runtime, **kwargs: Any) -> FriendWatcher:
        kwargs.setdefault("interval_seconds", 900.0)
        kwargs.setdefault("allow_real_actions", True)
        kwargs.setdefault("confirmation", "确认")
        return FriendWatcher(accounts=["acct-1"], runtime=runtime, **kwargs)

    async def test_checks_but_never_auto_accepts(self) -> None:
        runtime = self.Runtime(["王建国 申请加你为好友"])
        self.assertEqual(await self._watcher(runtime).tick(), 1)
        task_type, payload, confirmation = runtime.calls[-1]
        self.assertIs(task_type, TaskType.CHECK_NEW_FRIENDS)
        self.assertNotIn(
            TaskType.ACCEPT_NEW_FRIENDS,
            [t for t, _, _ in runtime.calls],
            "自动通过好友申请是最容易招风控的动作，不该在这里发生",
        )
        # dry-run 下底座直接回空壳，查了等于没查——真开了才谈得上「查到几个」。
        self.assertFalse(payload["dry_run"])
        self.assertEqual(confirmation, "确认")

    async def test_stays_dry_when_real_actions_are_off(self) -> None:
        runtime = self.Runtime(["有人加你"])
        await self._watcher(runtime, allow_real_actions=False).tick()
        _task_type, payload, confirmation = runtime.calls[-1]
        self.assertTrue(payload["dry_run"])
        self.assertEqual(confirmation, "", "没开真实动作就不该带确认短语")

    async def test_zero_interval_disables_it_entirely(self) -> None:
        runtime = self.Runtime(["有人加你"])
        watcher = self._watcher(runtime, interval_seconds=0)
        self.assertFalse(watcher.enabled)
        self.assertEqual(await watcher.tick(), 0)
        self.assertEqual(runtime.calls, [])

    async def test_quiet_hours_skip_the_check(self) -> None:
        runtime = self.Runtime(["有人加你"])
        watcher = self._watcher(
            runtime,
            active_hours="09:00-21:00",
            clock=lambda: datetime(2026, 8, 10, 3, 0, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual(await watcher.tick(), 0)
        self.assertEqual(runtime.calls, [], "凌晨三点自动查好友申请，正是风控在找的样子")


if __name__ == "__main__":
    unittest.main()

class CursorSignatureStabilityTests(unittest.TestCase):
    """★ 2026-08-13 真机：自动回复**一次都没触发过**，日志反复报「游标已滚出读取窗口」。

    病根：`uia.py::read_current_chat` 只给**最后一条**消息补 `is_self`/`sender`（预览文案 +
    右键菜单探测），其余行没有。游标存的正是最后一条的签名——下一轮它不再是最后一条、
    不再被补字段，同一条消息就算出另一个哈希，游标永远对不回来，`fresh` 永远是空。
    实证：`gateway_cursors.seq` 一直 0、`gateway_sessions` 一条没有。
    """

    def test_same_message_hashes_the_same_with_or_without_the_latest_row_enrichment(self) -> None:
        bare = {"text": "在吗"}
        enriched = {"text": "在吗", "is_self": True, "sender": "我"}  # 它当过"最后一条"时的样子
        self.assertEqual(
            message_signature(bare),
            message_signature(enriched),
            "★ 签名不许依赖只给最后一行补的字段，否则游标永远找不回来",
        )

    def test_cursor_is_found_on_the_next_read_after_it_stops_being_latest(self) -> None:
        # 第一轮：末条被补了字段，游标按它存下
        first = [{"text": "你好"}, {"text": "在吗", "is_self": True, "sender": "我"}]
        _, cursor = split_new_messages(first, None)
        # 第二轮：同一条不再是末条 → 没人补字段；后面来了一条新的
        second = [{"text": "你好"}, {"text": "在吗"}, {"text": "有个岗位想问下"}]
        fresh, _ = split_new_messages(second, cursor)
        self.assertEqual([m["text"] for m in fresh], ["有个岗位想问下"])

    def test_old_style_cursor_still_matches_once(self) -> None:
        """换算法不该让当天每个会话白丢一条消息。"""
        row = {"text": "在吗", "is_self": True, "sender": "我"}
        old_cursor = legacy_message_signature(row)
        fresh, _ = split_new_messages([row, {"text": "新消息"}], old_cursor)
        self.assertEqual([m["text"] for m in fresh], ["新消息"])

    def test_old_cursor_written_while_the_row_was_enriched_still_matches(self) -> None:
        """★ 真实的老游标就是这么来的：存的时候它被补过字段，再读到时字段没了。"""
        old_cursor = legacy_message_signature({"text": "在吗", "is_self": True, "sender": "我"})
        bare = {"text": "在吗"}  # 下一轮读到的样子
        fresh, _ = split_new_messages([bare, {"text": "新消息"}], old_cursor)
        self.assertEqual([m["text"] for m in fresh], ["新消息"], "老游标必须还认得出来")
