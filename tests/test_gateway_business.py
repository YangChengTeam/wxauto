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
    RateLimit,
    Watchlist,
    WatchScope,
    clamp_interval,
    extract_reply,
    format_transcript,
    jittered,
    looks_like_rejection,
    within_active_hours,
)
from wxclient.gateway import inbound as _inbound_mod
from wxclient.gateway.guard import (
    ACTION_FRIEND,
    ACTION_GROUP,
    ACTION_MESSAGE,
    ACTION_MOMENTS,
)
from wxclient.gateway.inbound import FOLLOW_UP_DEFER, PENDING_MAX_AGE_HOURS, PENDING_MAX_ATTEMPTS, message_to_row
from wxclient.gateway.outbound import AUTO_TAKEOVER_REASON
from wxclient.gateway.sessions import HUMAN_TAKEOVER
from wxclient.gateway.wechat_io import (
    FIRST_SEEN_TAIL,
    MAX_PEERS_PER_POLL,
    MAX_PEERS_PER_QUICK_POLL,
    preview_keys,
    preview_matches,
    row_time,
    QueueMessageSource,
    latest_cursor,
    legacy_message_signature,
    message_signature,
    split_new_messages,
)
from wxclient.queue import TaskType

# 夹具 T0 是 UTC，行首时分的断言按 UTC 写；生产按本机时区打（见 inbound.local_clock_text）。
_inbound_mod.LOCAL_TZ = timezone.utc

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

    def test_all_day_window(self) -> None:
        window = "00:00-24:00"
        self.assertTrue(within_active_hours(datetime(2026, 8, 10, 0, 0), window))
        self.assertTrue(within_active_hours(datetime(2026, 8, 10, 3, 0), window))
        self.assertTrue(within_active_hours(datetime(2026, 8, 10, 23, 59, 59), window))

    def test_end_of_day_only_for_end(self) -> None:
        self.assertTrue(within_active_hours(datetime(2026, 8, 10, 23, 30), "21:00-24:00"))
        self.assertFalse(within_active_hours(datetime(2026, 8, 10, 12, 0), "21:00-24:00"))
        with self.assertRaises(ValueError):
            within_active_hours(datetime(2026, 8, 10, 12, 0), "24:00-09:00")

    def test_bad_format_rejected(self) -> None:
        with self.assertRaises(ValueError):
            within_active_hours(datetime(2026, 8, 10, 12, 0), "早上到晚上")


class GuardTests(StoreTestCase):
    def _guard(self, **kwargs: Any) -> OutboundGuard:
        return OutboundGuard(self.store, **kwargs)

    async def test_allows_normal(self) -> None:
        self.assertTrue(self._guard().check("a", "p", now=datetime(2026, 8, 10, 14, 0)).allowed)

    async def test_blocks_taken_over(self) -> None:
        self.store.set_candidate_mode("a", "p", HUMAN_TAKEOVER, reason="说了别联系")
        decision = self._guard().check("a", "p", now=datetime(2026, 8, 10, 14, 0))
        self.assertFalse(decision.allowed)
        self.assertIn("人工接管", decision.reason)

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


class ActionRateLimitTests(StoreTestCase):
    """★ 分动作频次（072，需求文档 §13.3）。

    原来只有一个 `max_sends_per_hour` 总闸管所有写操作，于是「今天一条消息没发」
    就能换来「连加 30 个好友」——而加好友恰恰是风控最敏感的动作（2026-08-12 自动退登）。
    """

    def _guard(self, **kw) -> OutboundGuard:
        limits = {
            ACTION_MESSAGE: RateLimit(3, 3600.0),
            ACTION_GROUP: RateLimit(1, 3600.0),
            ACTION_FRIEND: RateLimit(2, 3600.0),
            ACTION_MOMENTS: RateLimit(1, 86400.0),
        }
        return OutboundGuard(
            self.store, active_hours="00:00-23:59", action_limits=limits, **kw
        )

    async def test_each_action_has_its_own_budget(self) -> None:
        """★ 加好友的额度用完，不该因为「消息还没发几条」就放行。"""
        guard = self._guard(max_sends_per_hour=100)
        for _ in range(2):
            self.store.record_send("a", "p", T0, action=ACTION_FRIEND)

        friend = guard.check("a", "p", now=T0, action=ACTION_FRIEND)
        self.assertFalse(friend.allowed)
        self.assertIn("加好友", friend.reason)
        # 另一档一点都没被消耗。
        self.assertTrue(guard.check("a", "p", now=T0, action=ACTION_MESSAGE).allowed)

    async def test_group_is_stricter_than_one_on_one(self) -> None:
        """群的误伤面比单聊大，配额单独且更紧。"""
        guard = self._guard(max_sends_per_hour=100)
        self.store.record_send("a", "某某招聘群", T0, action=ACTION_GROUP)
        self.assertFalse(guard.check("a", "某某招聘群", now=T0, action=ACTION_GROUP).allowed)
        self.assertTrue(guard.check("a", "王建国", now=T0, action=ACTION_MESSAGE).allowed)

    async def test_moments_budget_is_per_day_not_per_hour(self) -> None:
        """一小时发 3 条和一天发 3 条完全是两回事。"""
        guard = self._guard(max_sends_per_hour=100)
        self.store.record_send("a", "", T0, action=ACTION_MOMENTS)
        self.assertFalse(guard.check("a", "", now=T0 + timedelta(hours=5), action=ACTION_MOMENTS).allowed)
        self.assertTrue(guard.check("a", "", now=T0 + timedelta(hours=25), action=ACTION_MOMENTS).allowed)

    async def test_overall_cap_still_applies(self) -> None:
        """★ 分档各自没超，加起来也不能没边——两道闸都要过。"""
        guard = self._guard(max_sends_per_hour=2)
        self.store.record_send("a", "p", T0, action=ACTION_FRIEND)
        self.store.record_send("a", "p", T0, action=ACTION_MESSAGE)
        decision = guard.check("a", "p", now=T0, action=ACTION_MESSAGE)
        self.assertFalse(decision.allowed)
        self.assertIn("上限", decision.reason)

    async def test_zero_budget_means_unlimited(self) -> None:
        """配额配成 0 = 这一档不限（用户 2026-09-18 裁决；原来 0 表示「禁止」）。

        分档为 0 时随便发；总闸为 0 时也不数总量；两边都 0 就只剩黑名单/时间窗/接管三道纯判断。
        """
        guard = OutboundGuard(
            self.store, active_hours="00:00-23:59", max_sends_per_hour=0,
            action_limits={ACTION_GROUP: RateLimit(0, 3600.0)},
        )
        for _ in range(50):
            self.store.record_send("a", "g", T0, action=ACTION_GROUP)
        self.assertTrue(guard.check("a", "g", now=T0, action=ACTION_GROUP).allowed)
        reservation = guard.reserve("a", "g", now=T0, action=ACTION_GROUP, count=3)
        self.assertEqual(reservation.granted, 3)
        reservation.settle(3)
        # 不限的档照样记账——别的档（比如总闸开着时）还要数它
        self.assertEqual(self.store.sends_since("a", T0 - timedelta(hours=1), action=ACTION_GROUP), 53)

    async def test_group_messages_unlimited_by_default(self) -> None:
        """出厂默认群消息不限，但总闸仍然罩着它。"""
        guard = OutboundGuard(self.store, active_hours="00:00-23:59", max_sends_per_hour=2)
        for _ in range(10):
            self.store.record_send("a", "g", T0, action=ACTION_GROUP)
        decision = guard.check("a", "g", now=T0, action=ACTION_GROUP)
        self.assertFalse(decision.allowed)
        self.assertIn("该账号一小时内", decision.reason)

    async def test_legacy_rows_count_as_private_messages(self) -> None:
        """老库补列后，历史那些没有 action 的行按「私聊」算——不能凭空清零谁的额度。"""
        self.store.record_send("a", "p", T0)          # 不带 action，走默认
        self.assertEqual(
            self.store.sends_since("a", T0 - timedelta(hours=1), action=ACTION_MESSAGE), 1
        )


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
    def _sender(self, sink: FakeSink, *, takeover=None, **guard_kwargs: Any) -> OutboundSender:
        now = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        # 时间窗默认给满：这些用例测的是出站本身，T0 换算到本机是几点不该左右结论。
        guard_kwargs.setdefault("active_hours", "00:00-23:59")
        return OutboundSender(
            sink=sink,
            guard=OutboundGuard(self.store, **guard_kwargs),
            store=self.store,
            clock=lambda: now,
            takeover=takeover,
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

    # ── 不找工作 / 别联系 → 转人工接管（2026-10-09，替代本机拉黑）──

    async def test_takeover_applied_even_when_not_replying(self) -> None:
        """AI 端判定转人工 → 即使这一轮不发消息，接管也必须立刻生效。"""
        sink = FakeSink()
        await self._sender(sink).deliver("a", "p", reply_card([], no_reply_reason="对方拒绝", actions=[{"type": "set_ai_status", "reason": "对方说不想找工作了", "payload": {"status": "human_takeover"}}]))
        row = self.store.candidate_mode("a", "p")
        self.assertEqual(row.mode, HUMAN_TAKEOVER)
        self.assertEqual(row.reason, "对方说不想找工作了")

    async def test_closing_line_goes_out_before_takeover(self) -> None:
        """★ 收尾那句先发出去再接管——反过来它会被守门当「接管中」挡下，攒到交回分身时才冒出去。"""
        sink = FakeSink()
        result = await self._sender(sink).deliver("a", "p", reply_card(["好的，不打扰了"], actions=[{"type": "set_ai_status", "reason": "对方说不想找工作了", "payload": {"status": "human_takeover"}}]))
        self.assertTrue(result.delivered)
        self.assertEqual([text for _, _, text in sink.sent], ["好的，不打扰了"])
        self.assertEqual(self.store.candidate_mode("a", "p").mode, HUMAN_TAKEOVER)

    async def test_denied_turn_with_takeover_is_settled_not_deferred(self) -> None:
        sink = FakeSink()
        self.store.record_send("a", "p", datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc))
        result = await self._sender(sink, max_sends_per_hour=1).deliver(
            "a", "p", reply_card(["好的，不打扰了"], actions=[{"type": "set_ai_status", "reason": "对方说不想找工作了", "payload": {"status": "human_takeover"}}]),
        )
        self.assertFalse(result.delivered)
        self.assertEqual(result.follow_up, "", "留着的收尾话会在交回分身时重投出去")
        self.assertEqual(self.store.candidate_mode("a", "p").mode, HUMAN_TAKEOVER)
        # 对照：没有接管提案的同一种拦截照旧留着重投。
        self.store.record_send("a", "q", datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc))
        plain = await self._sender(sink, max_sends_per_hour=1).deliver("a", "q", reply_card(["你好"]))
        self.assertEqual(plain.follow_up, FOLLOW_UP_DEFER)

    async def test_existing_takeover_reason_is_kept(self) -> None:
        self.store.set_candidate_mode("a", "p", HUMAN_TAKEOVER, reason="招聘老师自己在聊")
        await self._sender(FakeSink()).deliver("a", "p", reply_card([], actions=[{"type": "set_ai_status", "reason": "对方说不想找工作了", "payload": {"status": "human_takeover"}}]))
        self.assertEqual(self.store.candidate_mode("a", "p").reason, "招聘老师自己在聊")

    async def test_legacy_blacklist_action_becomes_takeover(self) -> None:
        """老 AI 端还会发 `blacklist`：按接管处置，绝不再本机拉黑。"""
        await self._sender(FakeSink()).deliver(
            "a", "p", reply_card([], actions=[{"type": "blacklist"}]),
        )
        row = self.store.candidate_mode("a", "p")
        self.assertEqual((row.mode, row.reason), (HUMAN_TAKEOVER, AUTO_TAKEOVER_REASON))
        self.assertEqual(self.store.legacy_blacklist("a"), [])

    async def test_takeover_goes_through_the_injected_path(self) -> None:
        calls: list[tuple[str, str]] = []

        async def takeover(peer: str, reason: str) -> None:
            calls.append((peer, reason))

        await self._sender(FakeSink(), takeover=takeover).deliver("a", "p", reply_card(["好"], actions=[{"type": "set_ai_status", "reason": "对方说不想找工作了", "payload": {"status": "human_takeover"}}]))
        self.assertEqual(calls, [("p", "对方说不想找工作了")])

    async def test_takeover_failure_does_not_break_delivery(self) -> None:
        async def takeover(_peer: str, _reason: str) -> None:
            raise RuntimeError("业务端 500")

        result = await self._sender(FakeSink(), takeover=takeover).deliver(
            "a", "p", reply_card(["好的，不打扰了"], actions=[{"type": "set_ai_status", "reason": "对方说不想找工作了", "payload": {"status": "human_takeover"}}]),
        )
        self.assertTrue(result.delivered)

    async def test_non_takeover_status_does_not_take_over(self) -> None:
        await self._sender(FakeSink()).deliver(
            "a", "p", reply_card(["在的"], actions=[{"type": "set_ai_status", "payload": {"status": "ai_managed"}}]),
        )
        self.assertFalse(self.store.candidate_mode("a", "p").known)

    async def test_guard_blocks_send(self) -> None:
        sink = FakeSink()
        self.store.set_candidate_mode("a", "p", HUMAN_TAKEOVER, reason="先前已拒绝")
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

    async def test_rejection_wording_is_dispatched_not_blacklisted_locally(self) -> None:
        """★ 网关不再凭关键词自己处置（用户 2026-09-19 裁决：候选人每条新消息都要回）。

        「别再联系我了」照样派给 AI 端一轮；是不是拒绝由它看过原话再判，
        它提案接管时网关才转人工（`outbound._hand_over`，2026-10-09 起不再拉黑）。
        """
        calls: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            calls.append(transcript)

        source = FakeSource([[msg("别再联系我了", msg_id="m1")], []])
        loop = self._loop(source, handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        await loop.drain()

        self.assertEqual(len(calls), 1)
        self.assertIn("别再联系我了", calls[0])
        self.assertEqual(self.store.candidate_modes("acct-1"), {})

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

    async def test_self_only_batch_is_not_booked_as_a_debt(self) -> None:
        """★ 全是专员本人说的话 → 不触发一轮，也**不记进待处理表**。

        待处理表记的是「欠对方一条回复」。自己说的那几条谁也没在等回复，记进去就是一笔
        永远销不掉的账：实时日志里那句「有 1 批已经读进来、但还没回出去」一直挂着不降，看的人以为消息卡住了
        （2026-08-14 真机，专员自己在群里说了句话就挂了一下午）。
        """
        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            raise AssertionError("不该被触发")

        loop = self._loop(
            FakeSource([[msg("我先问问他", msg_id="m1", from_self=True)], []]), handler, now=T0
        )
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        self.assertEqual(await loop.tick(), 0)
        await loop.drain()
        self.assertEqual(self.store.list_pending_inbound(), [], "没欠账就不该有账")

    async def test_not_dispatching_is_explained_once(self) -> None:
        """★ 「读到了却没回」必须有一行说明，否则现场只能猜（用户 2026-08-14）。

        又不能每趟都喊：缓冲里的消息每 tick 都重新过一遍判定，逐趟一行会把实时日志刷满。
        """
        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            raise AssertionError("不该被触发")

        loop = self._loop(
            FakeSource([[msg("我先问问他", msg_id="m1", from_self=True)], [], []]), handler, now=T0
        )
        with capture_logs() as logs:
            await loop.tick()
            self._now = T0 + timedelta(seconds=10)
            await loop.tick()
            self._now = T0 + timedelta(seconds=20)
            await loop.tick()
        said = [e for e in logs if e["event"].startswith("这几条全是自己说的")]
        self.assertEqual(len(said), 1, f"同一批只该说一次：{said}")
        self.assertEqual(said[0]["条数"], 1)

    async def test_self_message_still_joins_the_next_real_turn(self) -> None:
        """不记账不等于丢上下文：对方回话时，自己刚说的那句要一起送上去。"""
        seen: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            seen.append(transcript)

        loop = self._loop(
            FakeSource([
                [msg("我先问问他", msg_id="m1", from_self=True)],
                [msg("好的我在", msg_id="m2")],
                [],
            ]),
            handler,
            now=T0,
        )
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()
        self._now = T0 + timedelta(seconds=20)
        await loop.tick()
        await loop.drain()
        self.assertEqual(len(seen), 1)
        self.assertIn("我先问问他", seen[0])
        self.assertIn("好的我在", seen[0])
        self.assertEqual(self.store.list_pending_inbound(), [], "真回复跑完，账要销干净")

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

    def __init__(
        self, unread: list[dict], history: dict[str, list[dict]], sessions: list[dict] | None = None
    ) -> None:
        self.unread = unread
        self.history = history
        #: 左侧会话列表（`read_chat_sessions`）。None = 假装读不到（老测试不关心它，一律开窗）。
        self.sessions = sessions
        self.calls: list[tuple[Any, Any]] = []
        self.confirmations: list[str] = []

    async def submit(self, task_type, payload=None, *, confirmation: str = "", **_kw):
        self.calls.append((task_type, payload))
        self.confirmations.append(confirmation)
        if task_type is TaskType.GET_UNREAD_MESSAGES:
            return {"result": {"unread_sessions": self.unread}}
        if task_type is TaskType.READ_CHAT_SESSIONS:
            if self.sessions is None:
                raise RuntimeError("读不到会话列表")
            return {"status": "succeeded", "result": {"sessions": list(self.sessions)}}
        friend = (payload or {}).get("friend")
        return {"result": {"messages": self.history.get(friend, [])}}

    def listed(self) -> int:
        """读了几次会话列表。"""
        return sum(1 for task_type, _ in self.calls if task_type is TaskType.READ_CHAT_SESSIONS)

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

    async def test_nothing_is_touched_before_the_list_arrives(self) -> None:
        """★ 名单没到之前一个会话都不碰（fail-closed，069）。

        原来的兜底是「所有单聊都管」，意味着装上就会自动回复任何人——包括专员的
        家人朋友，违反需求文档 §13.1「对象必须在 HR 数据权限范围内」。
        """
        runtime = self._runtime()
        source = QueueMessageSource(runtime=runtime, store=self.store)
        with capture_logs() as logs:
            await source.fetch("acct-1")
        self.assertEqual(runtime.opened(), [], "名单没到就不该打开任何会话")
        # 且必须喊出来，否则现场表现是「有新消息、网关一声不吭」，看的人以为坏了。
        self.assertTrue(
            any(entry["log_level"] == "warning" and "监听名单" in entry["event"] for entry in logs),
            logs,
        )

    async def test_group_is_handled_when_it_is_on_the_list(self) -> None:
        """★ 群和单聊一视同仁，只看名单（用户裁决 2026-08-14）。

        原来写死「群聊一概不管」，于是「把某个招聘群纳入自动化」在网关侧根本做不到。
        """
        runtime = self._runtime()
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.GROUP])
        )
        await source.fetch("acct-1")          # 先读一轮，把游标建起来
        self.assertIn(self.GROUP, runtime.opened())
        self.assertTrue(self.store.is_known_group("acct-1", self.GROUP), "是不是群仍要记下来备查")
        self.assertNotIn(self.PERSON, runtime.opened(), "不在名单上的人照样不碰")

    async def test_group_off_the_list_is_never_opened(self) -> None:
        """不在名单上的群，连开都不开——开窗口本身就是打扰。"""
        runtime = self._runtime()
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.PERSON])
        )
        await source.fetch("acct-1")
        self.assertNotIn(self.GROUP, runtime.opened())

    async def test_reads_watched_chats_even_when_nothing_is_unread(self) -> None:
        """★ 不再依赖微信的「未读」（用户裁决 2026-08-14）。

        未读这个信号根本不可靠：群多半设了消息免打扰不进未读，专员在手机上瞄过一眼的
        也不再是未读——于是该回的消息网关压根看不见。名单里的会话就是要读的会话。
        """
        runtime = FakeRuntime(
            unread=[],   # 微信这边一条未读都没有
            history={self.PERSON: [
                {"text": "还招人吗", "is_self": False},
                {"text": "在的", "is_self": True},
                {"text": "我明天能去看看吗", "is_self": False},
            ]},
        )
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.PERSON])
        )
        await source.fetch("acct-1")                      # 先读一轮，把游标建起来
        runtime.history[self.PERSON].append({"text": "在吗", "is_self": False})
        messages = await source.fetch("acct-1")
        self.assertEqual([m.text for m in messages], ["在吗"])
        self.assertEqual(runtime.opened(), [self.PERSON, self.PERSON])

    async def test_long_list_is_read_in_rotation(self) -> None:
        """名单长了轮着读：每轮把几十个聊天窗口挨个点一遍正是风控最不喜欢的形状。"""
        names = [f"候选人-{i:02d}" for i in range(20)]
        runtime = FakeRuntime(unread=[], history={n: [{"text": "你好", "is_self": False}] for n in names})
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names)
        )
        seen: set[str] = set()
        for _ in range(3):
            runtime.calls.clear()
            await source.fetch("acct-1")
            opened = runtime.opened()
            self.assertLessEqual(len(opened), MAX_PEERS_PER_POLL, "一轮开的窗口数超了上限")
            seen.update(opened)
        # 20 个名字、一轮最多 8 个 → 三轮之内每个都该轮到（轮转不往前走的话会卡在头 8 个）。
        self.assertEqual(seen, set(names), "轮转没走完一圈，有人永远读不到")

    async def test_hot_peer_is_read_every_round_while_cold_peers_still_rotate(self) -> None:
        """名单超长时**正聊着的人每轮都读**，其余的照旧轮着来、谁都不会永远读不到。

        原来是机械轮转：对方刚回了一句，网关却先去翻另外十几个没人说话的窗口，
        正在聊的人也得等两三轮。有限的窗口先给有来往的会话。
        """
        names = [f"候选人-{i:02d}" for i in range(20)]
        hot = "候选人-13"
        now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        runtime = FakeRuntime(unread=[], history={n: [{"text": "你好", "is_self": False}] for n in names})
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names),
            clock=lambda: now, hot_window_seconds=900,
        )
        # 对方 3 分钟前说过话（记在聊天记录里）→ 正热着
        self.store.record_messages("acct-1", hot, [{
            "msg_id": f"{hot}#1", "text": "还招人吗", "from_self": False,
            "at": (now - timedelta(minutes=3)).isoformat(),
        }])
        seen: set[str] = set()
        for _ in range(3):
            runtime.calls.clear()
            await source.fetch("acct-1")
            opened = runtime.opened()
            self.assertLessEqual(len(opened), MAX_PEERS_PER_POLL, "一轮开的窗口数超了上限")
            self.assertIn(hot, opened, "正聊着的人这一轮没读到")
            self.assertEqual(opened[0], hot, "正聊着的人该排在最前面")
            seen.update(opened)
        self.assertEqual(seen, set(names), "轮转没走完一圈，有人永远读不到")

    async def test_our_own_unread_back_reply_keeps_the_peer_hot(self) -> None:
        """我们刚发出去、还没从微信读回来的那句也算来往——正等对方回，这会儿最该盯着。"""
        names = [f"候选人-{i:02d}" for i in range(20)]
        hot = "候选人-17"
        now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        runtime = FakeRuntime(unread=[], history={n: [] for n in names})
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names),
            clock=lambda: now, hot_window_seconds=900,
        )
        self.store.remember_sent_text("acct-1", hot, "明天上午方便来看看吗", at=now - timedelta(minutes=1))
        await source.fetch("acct-1")
        self.assertEqual(runtime.opened()[0], hot)

    async def test_stale_activity_falls_back_to_rotation(self) -> None:
        """热窗之外的来往不算热：昨天聊过的人跟没聊过的一样排队。"""
        names = [f"候选人-{i:02d}" for i in range(20)]
        stale = "候选人-13"
        now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        runtime = FakeRuntime(unread=[], history={n: [] for n in names})
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names),
            clock=lambda: now, hot_window_seconds=900,
        )
        self.store.record_messages("acct-1", stale, [{
            "msg_id": f"{stale}#1", "text": "还招人吗", "from_self": False,
            "at": (now - timedelta(hours=20)).isoformat(),
        }])
        await source.fetch("acct-1")
        self.assertEqual(runtime.opened(), names[:MAX_PEERS_PER_POLL], "没人热着时该按名单顺序从头读")

    async def test_hot_peers_leave_one_slot_for_cold_rotation(self) -> None:
        """热的人把槛位占满时也要留一个给冷档往前走，否则第九个人的消息永远看不见。"""
        names = [f"候选人-{i:02d}" for i in range(20)]
        hots = names[:10]
        now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        runtime = FakeRuntime(unread=[], history={n: [] for n in names})
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names),
            clock=lambda: now, hot_window_seconds=900,
        )
        for name in hots:
            self.store.record_messages("acct-1", name, [{
                "msg_id": f"{name}#1", "text": "在吗", "from_self": False,
                "at": (now - timedelta(minutes=2)).isoformat(),
            }])
        cold_seen: list[str] = []
        hot_seen: set[str] = set()
        for _ in range(12):
            runtime.calls.clear()
            await source.fetch("acct-1")
            opened = runtime.opened()
            self.assertEqual(len(opened), MAX_PEERS_PER_POLL)
            cold_in_round = [p for p in opened if p not in hots]
            self.assertEqual(len(cold_in_round), 1, "每轮该正好留一个位子给冷档")
            cold_seen.extend(cold_in_round)
            hot_seen.update(p for p in opened if p in hots)
        self.assertEqual(set(cold_seen), set(names[10:]), "冷档没轮完一圈")
        self.assertEqual(hot_seen, set(hots), "热档里有人一直排在门外")

    def test_recent_peer_activity_takes_the_latest_of_both_ledgers(self) -> None:
        """来往时刻取聊天记录和出站台账里最近的那一笔，太早的一律不出现。"""
        now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        self.store.record_messages("acct-1", "甲", [{
            "msg_id": "甲#1", "text": "在吗", "from_self": False, "at": (now - timedelta(minutes=8)).isoformat(),
        }])
        self.store.remember_sent_text("acct-1", "甲", "在的", at=now - timedelta(minutes=2))
        self.store.remember_sent_text("acct-1", "乙", "您好", at=now - timedelta(hours=3))
        self.store.record_messages("acct-2", "丙", [{
            "msg_id": "丙#1", "text": "在吗", "from_self": False, "at": now.isoformat(),
        }])
        activity = self.store.recent_peer_activity("acct-1", since=now - timedelta(minutes=15))
        self.assertEqual(set(activity), {"甲"}, "别的账号、太早的来往都不该出现")
        self.assertEqual(activity["甲"], now - timedelta(minutes=2))

    async def test_unchanged_session_row_skips_the_window(self) -> None:
        """会话列表里预览和时间都跟上次读到的一样 → 这一轮不开它的窗口；变了的、不在列表里的照旧开。"""
        a, b, c = "候选人-甲", "候选人-乙", "候选人-丙"
        now = datetime(2026, 9, 23, 2, 5, tzinfo=timezone.utc)  # 本机时区下的某个上午
        stamp = now.astimezone().strftime("%H:%M")
        runtime = FakeRuntime(
            unread=[],
            history={a: [{"text": "ok", "is_self": False}], b: [{"text": "在吗", "is_self": False}], c: []},
            sessions=[
                {"name": a, "preview": "ok", "timestamp_text": stamp, "timestamp_is_today": True},
                {"name": b, "preview": "在吗", "timestamp_text": stamp, "timestamp_is_today": True},
            ],
        )
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[a, b, c]),
            clock=lambda: now, allow_real_actions=True, confirmation="口令",
        )
        # 第一轮：上次读的时刻都还没有 → 全开一遍
        messages = await source.fetch("acct-1")
        self.assertEqual(set(runtime.opened()), {a, b, c})
        self.assertEqual(runtime.confirmations[0], "口令", "真机读会话列表要带确认短语，否则被授权闸拒掉")
        self.store.record_messages("acct-1", a, [message_to_row(m) for m in messages if m.peer_id == a])
        self.store.record_messages("acct-1", b, [message_to_row(m) for m in messages if m.peer_id == b])

        # 第二轮：甲没变；乙预览变了（对方又说了一句）；丙不在列表里
        runtime.sessions[1] = dict(runtime.sessions[1], preview="还招人吗")
        runtime.calls.clear()
        now = now + timedelta(minutes=3)
        await source.fetch("acct-1")
        self.assertEqual(set(runtime.opened()), {b, c}, "甲没变不该开窗；乙变了、丙不在列表里都得开")
        self.assertEqual(runtime.listed(), 1)

    async def test_same_preview_but_newer_time_still_opens(self) -> None:
        """对方连发两句一样的话：预览对得上、但时间比上次读晚 → 还是要开（单看预览会看漏）。"""
        a = "候选人-甲"
        now = datetime(2026, 9, 23, 2, 5, tzinfo=timezone.utc)
        runtime = FakeRuntime(
            unread=[], history={a: [{"text": "在吗", "is_self": False}]},
            sessions=[{"name": a, "preview": "在吗", "timestamp_text": now.astimezone().strftime("%H:%M")}],
        )
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[a, "候选人-乙"]),
            clock=lambda: now,
        )
        messages = await source.fetch("acct-1")
        self.store.record_messages("acct-1", a, [message_to_row(m) for m in messages])
        runtime.calls.clear()
        later = (now + timedelta(minutes=2)).astimezone().strftime("%H:%M")
        runtime.sessions[0] = dict(runtime.sessions[0], timestamp_text=later)
        now = now + timedelta(minutes=4)
        await source.fetch("acct-1")
        self.assertIn(a, runtime.opened())

    async def test_list_failure_or_draft_falls_back_to_opening(self) -> None:
        """列表读不到、或那一行带「[草稿]」→ 照旧开窗（预筛只是省事，宁开勿漏）。"""
        a = "候选人-甲"
        now = datetime(2026, 9, 23, 2, 5, tzinfo=timezone.utc)
        runtime = FakeRuntime(unread=[], history={a: [{"text": "ok", "is_self": False}]}, sessions=None)
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[a, "候选人-乙"]),
            clock=lambda: now,
        )
        messages = await source.fetch("acct-1")
        self.store.record_messages("acct-1", a, [message_to_row(m) for m in messages])
        runtime.calls.clear()
        with capture_logs() as logs:
            await source.fetch("acct-1")
        self.assertIn(a, runtime.opened())
        self.assertTrue(any(log["event"] == "读会话列表失败，这一轮不预筛、照旧开窗" for log in logs))

        stamp = now.astimezone().strftime("%H:%M")
        runtime.sessions = [{"name": a, "preview": "[草稿] ok", "timestamp_text": stamp}]
        runtime.calls.clear()
        await source.fetch("acct-1")
        self.assertIn(a, runtime.opened(), "专员正在打字（草稿）时不确定有没有新消息，得开")

    async def test_between_full_rounds_only_the_peers_in_conversation_are_read(self) -> None:
        """在聊快读（用户 2026-09-24「合理化一下」）：两次全量巡检之间只读热窗里有来往的人。

        原来正聊着的时候每 30~60 秒就把整份名单过一遍（读列表 18 秒 + 最多 8 个窗口），
        哪怕真在聊的只有一个。
        """
        names = [f"候选人-{i:02d}" for i in range(20)]
        hot = "候选人-13"
        clock = {"now": datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)}
        runtime = FakeRuntime(unread=[], history={n: [] for n in names}, sessions=[])
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names),
            clock=lambda: clock["now"], hot_window_seconds=600,
        )
        source.set_full_interval(180)
        self.store.record_messages("acct-1", hot, [{
            "msg_id": f"{hot}#1", "text": "还招人吗", "from_self": False,
            "at": (clock["now"] - timedelta(minutes=1)).isoformat(),
        }])

        await source.fetch("acct-1")  # 第一轮：全量巡检
        self.assertEqual(len(runtime.opened()), MAX_PEERS_PER_POLL)

        for seconds in (30, 60, 90):  # 巡检间隔内：只读在聊的那一个，也不读会话列表
            runtime.calls.clear()
            clock["now"] += timedelta(seconds=30)
            await source.fetch("acct-1")
            self.assertEqual(runtime.opened(), [hot], f"第 {seconds} 秒那轮该只读正在聊的人")
            self.assertEqual(runtime.listed(), 0, "只有一两个人在聊时直接开窗，不先读列表")

        runtime.calls.clear()
        clock["now"] += timedelta(seconds=100)  # 距上次巡检满 180 秒 → 又是全量
        await source.fetch("acct-1")
        self.assertEqual(len(runtime.opened()), MAX_PEERS_PER_POLL, "巡检不能被快读一直顶掉")

    async def test_quick_round_with_nobody_in_conversation_touches_nothing(self) -> None:
        names = [f"候选人-{i:02d}" for i in range(20)]
        clock = {"now": datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)}
        runtime = FakeRuntime(unread=[], history={n: [] for n in names}, sessions=[])
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names),
            clock=lambda: clock["now"], hot_window_seconds=600,
        )
        source.set_full_interval(180)
        await source.fetch("acct-1")
        runtime.calls.clear()
        clock["now"] += timedelta(seconds=30)
        await source.fetch("acct-1")
        self.assertEqual(runtime.calls, [], "没人在聊时快读一个微信动作都不该有")

    async def test_quick_round_caps_windows_and_prefilters_when_many_are_talking(self) -> None:
        """在聊的人多（>2）：先读列表剔掉没变的，一轮最多开 3 个窗口。"""
        names = [f"候选人-{i:02d}" for i in range(20)]
        hots = names[:6]
        clock = {"now": datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)}
        runtime = FakeRuntime(unread=[], history={n: [] for n in names}, sessions=[])
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=names),
            clock=lambda: clock["now"], hot_window_seconds=600,
        )
        source.set_full_interval(180)
        for name in hots:
            self.store.record_messages("acct-1", name, [{
                "msg_id": f"{name}#1", "text": "在吗", "from_self": False,
                "at": (clock["now"] - timedelta(minutes=1)).isoformat(),
            }])
        await source.fetch("acct-1")
        runtime.calls.clear()
        clock["now"] += timedelta(seconds=30)
        await source.fetch("acct-1")
        opened = runtime.opened()
        self.assertLessEqual(len(opened), MAX_PEERS_PER_QUICK_POLL)
        self.assertTrue(set(opened) <= set(hots))
        self.assertEqual(runtime.listed(), 1)

    def test_prefilter_can_be_switched_off(self) -> None:
        runtime = FakeRuntime(unread=[], history={}, sessions=[])
        source = QueueMessageSource(runtime=runtime, store=self.store, prefilter_by_session_list=False)
        self.assertFalse(source._prefilter)

    def test_preview_keys_strip_wechat_decorations(self) -> None:
        """预览剥法：免打扰群「[7条] 」、群聊「某人: 」、「[图片]」对应聊天记录里的「图片」。"""
        self.assertEqual(preview_keys("ok"), ["ok"])
        self.assertIn("图片", preview_keys("[图片] "))
        self.assertIn("收到", preview_keys("[7条] 依凡: 收到"))
        self.assertIn("依凡: 收到", preview_keys("[7条] 依凡: 收到"))
        self.assertEqual(preview_keys("[草稿] 明天"), [], "草稿不算任何原文")
        self.assertEqual(preview_keys(""), [])

    def test_preview_matches_allows_truncated_preview(self) -> None:
        self.assertTrue(preview_matches("ok", "ok"))
        self.assertTrue(preview_matches("[图片]", "图片"))
        self.assertTrue(preview_matches("需要带什么我这边还没确认到，我让招聘老师…", "需要带什么我这边还没确认到，我让招聘老师跟厂里问一下"))
        self.assertFalse(preview_matches("在吗", "在吗？"))
        self.assertFalse(preview_matches("在吗", ""))
        self.assertFalse(preview_matches("在", "在吗在吗在吗"), "太短的前缀不算对上")

    def test_row_time_reads_wechat_labels_in_local_time(self) -> None:
        now = datetime(2026, 9, 23, 9, 30, tzinfo=timezone.utc)
        local = now.astimezone()
        today = local.date()
        tz = local.tzinfo
        self.assertEqual(row_time("17:01", now), datetime(today.year, today.month, today.day, 17, 1, tzinfo=tz))
        yesterday = today - timedelta(days=1)
        self.assertEqual(
            row_time("昨天 17:26", now), datetime(yesterday.year, yesterday.month, yesterday.day, 17, 26, tzinfo=tz)
        )
        sunday = row_time("星期日", now)
        self.assertIsNotNone(sunday)
        self.assertEqual(sunday.astimezone(tz).weekday(), 6)
        self.assertLess(sunday, now)
        self.assertEqual(row_time("9月20日", now).astimezone(tz).day, 20)
        self.assertIsNone(row_time("", now))
        self.assertIsNone(row_time("消息免打扰", now))

    async def test_transcript_uses_wechat_own_timestamp(self) -> None:
        """时间点用微信自己标的那个。

        ``at`` 是**网关读到这条的时刻**；直接读会话之后，读到的很可能是几分钟前甚至
        昨天的消息——拿读取时刻当发送时刻喂给模型，它会对着昨晚的消息说「你刚才说」。
        """
        runtime = FakeRuntime(
            unread=[],
            history={self.PERSON: [
                {"text": "在吗", "is_self": False,
                 "timestamp_text": "昨天 20:11", "timestamp_is_today": False},
            ]},
        )
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.PERSON])
        )
        await source.fetch("acct-1")
        runtime.history[self.PERSON].append(
            {"text": "还招人吗", "is_self": False, "timestamp_text": "今天 09:05"}
        )
        messages = await source.fetch("acct-1")
        self.assertEqual(format_transcript(messages), "[对方 · 今天 09:05] 还招人吗")

    async def test_every_read_says_what_it_saw(self) -> None:
        """★ 读完必须说清「读到几条、几条算新的、新的是谁说的什么」（用户 2026-08-14）。

        原来读完只有一句「读取完成 N 条」，之后要么回复、要么无声无息——现场分不清是
        「没有新消息」「新消息是自己发的」还是「哪一环坏了」。
        """
        runtime = FakeRuntime(
            unread=[],
            history={self.PERSON: [{"text": "还招人吗", "is_self": False, "timestamp_is_today": True}]},
        )
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.PERSON])
        )
        with capture_logs() as logs:
            await source.fetch("acct-1")
        done = [e for e in logs if e["event"] == "读完这个会话"]
        self.assertEqual(len(done), 1, logs)
        self.assertEqual(done[0]["读到"], 1)
        self.assertEqual(done[0]["新消息"], 1)
        fresh_logs = [e for e in logs if e["event"] == "新消息"]
        self.assertEqual([e["内容"] for e in fresh_logs], ["还招人吗"])
        self.assertEqual(fresh_logs[0]["谁"], "对方")

    async def test_unreadable_peer_is_reported_once_not_every_poll(self) -> None:
        """名字和微信里对不上 → 说清楚是哪个名字，且只说一次。

        名单按会话名逐字寻址，差一个空格就是「查无此人」。这条错误只落在队列日志里的话，
        现场表现就成了「名单加了、可他就是不回」（2026-08-14 真机实测，群名对不上）。
        """
        class FailingRuntime(FakeRuntime):
            async def submit(self, task_type, payload=None, **kw):
                await super().submit(task_type, payload, **kw)
                return {"status": "failed", "last_error": "读取聊天记录失败：好友或群聊备注有误！查无此人！"}

        runtime = FailingRuntime(unread=[], history={})
        source = QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=["AI Agent"])
        )
        with capture_logs() as logs:
            await source.fetch("acct-1")
            await source.fetch("acct-1")
        warnings = [e for e in logs if e["log_level"] == "warning" and "读不到" in e["event"]]
        self.assertEqual(len(warnings), 1, "同一个读不到的会话每轮喊一次会把日志刷满")
        self.assertEqual(warnings[0]["会话"], "AI Agent")

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
        self.assertIsNone(watchlist.reason(self.PERSON))
        self.assertIsNotNone(watchlist.reason("路人甲"))

    async def test_pushed_empty_list_means_watch_nothing(self) -> None:
        """★ AI 端推来一份空名单 = 一个都不管，**不是**「不限制」。

        「空 = 不限制」这个默认值正是「装上就自动回复所有人」的来源。
        """
        watchlist = Watchlist.from_local(only=[self.PERSON])
        self.assertTrue(watchlist.apply_push({"only": []}))
        self.assertIsNotNone(watchlist.reason(self.PERSON))

    async def test_pushed_list_survives_a_restart(self) -> None:
        """名单要落盘：重启后如果得等 AI 端重推才恢复，中间那段网关是全哑的。"""
        saved: list[dict] = []
        watchlist = Watchlist(persist=lambda scope: saved.append({"only": sorted(scope.only)}))
        self.assertTrue(watchlist.apply_push({"only": [self.PERSON, self.GROUP]}))
        self.assertEqual(saved[-1]["only"], sorted([self.GROUP, self.PERSON]))

        # ——— 重启：从库里读回来那份 ———
        restored = Watchlist()
        restored.apply_scope(
            WatchScope(only=frozenset(saved[-1]["only"]), received=True, origin="本地留存")
        )
        self.assertTrue(restored.ready)
        self.assertIsNone(restored.reason(self.PERSON))
        self.assertIsNone(restored.reason(self.GROUP))
        self.assertIsNotNone(restored.reason("路人甲"))


# ────────────────────────── 节拍（防风控）──────────────────────────


class PacingTests(unittest.TestCase):
    """2026-08-12 真机事故：跑了一天后微信自己退了登录，判定触发风控。"""

    def test_interval_clamped_to_floor(self) -> None:
        self.assertEqual(clamp_interval(5.0, 30.0, what="读消息间隔"), 30.0)
        self.assertEqual(clamp_interval(60.0, 30.0, what="读消息间隔"), 60.0)

    def test_jitter_never_goes_below_the_floor(self) -> None:
        """★ 抖动只往慢里抖。

        双向抖动会把实际间隔抖到红线以下（审计实测：读消息 52% 的轮次低于 30s、
        查好友 50% 低于 300s）——而那两个数正是这一层声称把住的下限。
        """
        rng = random.Random(7)
        values = [jittered(60.0, rng=rng) for _ in range(200)]
        self.assertTrue(all(60.0 <= v <= 78.1 for v in values), min(values))
        self.assertGreater(len(set(values)), 150, "间隔还是整齐的，等于没抖")


class LostCursorAnchorTests(StoreTestCase):
    """★ 游标丢了不能把处理过的旧消息再跑一遍（2026-09-23 线上事故）。

    聊天窗口被翻上去（专员在看历史 / 读完那下 {END} 没生效），网关读到的是一窗**旧消息**：
    游标在里面找不到，老逻辑「只对齐游标」把游标拽回到旧消息上；下一趟窗口回到底部，
    从旧游标往后的整段（含早已回过的问题、以及分身自己的回复）全被当成新消息派了一轮——
    候选人同一个问题被回了两遍。修法：这台机器处理过哪几条**我们是知道的**（`gateway_messages`），
    拿最近处理过的那条在窗口里的位置当第二道锚；整窗都是处理过的就一条不接、游标不动。
    """

    PEER = "候选人-李四"

    def _source(self, runtime: FakeRuntime) -> QueueMessageSource:
        return QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.PEER]),
            history_limit=4,
        )

    def _record(self, texts: list[str]) -> None:
        rows = [{"msg_id": f"k{i}", "text": t, "raw_text": t, "at": T0.isoformat()} for i, t in enumerate(texts)]
        self.store.record_messages("acct-1", self.PEER, rows)

    def test_anchor_on_last_processed_message(self) -> None:
        window = [{"text": "老1"}, {"text": "老2"}, {"text": "新1"}, {"text": "新2"}]
        fresh, cursor = split_new_messages(window, "游标不在窗口里", known=["更老", "老1", "老2"])
        self.assertEqual([m["text"] for m in fresh], ["新1", "新2"])
        self.assertEqual(cursor, latest_cursor(window))

    def test_whole_window_already_processed_keeps_cursor(self) -> None:
        window = [{"text": "老1"}, {"text": "老2"}, {"text": "老3"}]
        with capture_logs() as logs:
            fresh, cursor = split_new_messages(window, "游标不在窗口里", known=["老1", "老2", "老3", "老4", "老5"])
        self.assertEqual(fresh, [])
        self.assertEqual(cursor, "游标不在窗口里", "游标绝不能被拽回旧消息上")
        self.assertTrue(any("翻上去" in e["event"] for e in logs), logs)

    def test_nothing_known_falls_back_to_realign(self) -> None:
        window = [{"text": "a"}, {"text": "b"}]
        fresh, cursor = split_new_messages(window, "游标不在窗口里", known=["x", "y"])
        self.assertEqual(fresh, [])
        self.assertEqual(cursor, latest_cursor(window))

    def test_repeated_phrase_anchors_on_the_right_occurrence(self) -> None:
        """对方后来又说了一句一模一样的「好的」：单看一行会对到新的那条上，把中间几条跳过。"""
        window = [{"text": "在吗"}, {"text": "好的"}, {"text": "工种是什么"}, {"text": "好的"}]
        fresh, _ = split_new_messages(window, "游标不在窗口里", known=["在吗", "好的"])
        self.assertEqual([m["text"] for m in fresh], ["工种是什么", "好的"])

    def test_emoji_code_is_part_of_the_anchor(self) -> None:
        """UIA 把表情按 `[微笑]` 原文读回来，锚也按原文比。"""
        window = [{"text": "早上好[微笑]"}, {"text": "在的"}]
        fresh, _ = split_new_messages(window, "游标不在窗口里", known=["在吗", "早上好[微笑]"])
        self.assertEqual([m["text"] for m in fresh], ["在的"])

    async def test_scrolled_up_window_does_not_replay_old_messages(self) -> None:
        """端到端：三趟读取——正常 / 窗口翻上去 / 窗口回底——只有真正的新消息被接进来。"""
        history = [{"text": f"第{i}句", "is_self": False} for i in range(1, 9)]   # 第1句…第8句
        runtime = FakeRuntime(unread=[], history={self.PEER: history[-4:]})       # 窗口=最新 4 条
        source = self._source(runtime)
        first = await source.fetch("acct-1")          # 第一次读：接尾部那几条，建游标
        self.assertTrue(first)
        # 网关那边处理完会把这些记进聊天记录（InboundLoop._ingest 做的事，这里手动落）
        self.store.record_messages("acct-1", self.PEER, [
            {"msg_id": m.msg_id, "text": m.text, "raw_text": m.raw_text, "at": m.at.isoformat()} for m in first
        ])

        # 第二趟：聊天窗口被翻上去了，读到的是最老的 4 条（全是处理前的历史）
        runtime.history[self.PEER] = history[:4]
        with capture_logs() as logs:
            self.assertEqual(await source.fetch("acct-1"), [], "一窗旧消息，一条都不该接")
        self.assertTrue(any("找不到" in e["event"] for e in logs), logs)

        # 第三趟：窗口回到底部，对方又说了一句
        runtime.history[self.PEER] = history[-3:] + [{"text": "第9句", "is_self": False}]
        got = await source.fetch("acct-1")
        self.assertEqual([m.text for m in got], ["第9句"], "第5–8句已处理过，只能接第9句")

    async def test_recent_raw_keys_are_oldest_first_and_use_raw_text(self) -> None:
        self.store.record_messages("acct-1", self.PEER, [
            {"msg_id": "m1", "text": "[图片：a.jpg（已附上）]", "raw_text": "图片", "at": "2026-09-23T01:00:00+00:00"},
            {"msg_id": "m2", "text": "在吗", "at": "2026-09-23T01:01:00+00:00"},
        ])
        self.assertEqual(self.store.recent_raw_keys("acct-1", self.PEER, limit=5), ["图片", "在吗"])


class OwnEchoTests(StoreTestCase):
    """★ 自己发出去的回复被读回来时，不能被当成对方的新消息（2026-09-09 真机：自问自答）。

    读回来的每一条「是不是我发的」全靠 `uia.py` 的启发式（气泡左右位置 / 截图 / 右键菜单），
    任何一条判错就是「对方说了句新的」→ 网关对着自己的话再回一轮。而我们自己发了什么是
    确定知道的：出站时记原文，读回来按原文认亲，认一次销一条。
    """

    PEER = "候选人-王建国"

    def _source(self, runtime: FakeRuntime) -> QueueMessageSource:
        return QueueMessageSource(
            runtime=runtime, store=self.store, watchlist=Watchlist.from_local(only=[self.PEER])
        )

    async def test_echo_misjudged_by_ui_is_still_ours(self) -> None:
        runtime = FakeRuntime(unread=[], history={self.PEER: [{"text": "还招人吗", "is_self": False}]})
        source = self._source(runtime)
        await source.fetch("acct-1")                       # 建游标

        # 我们回了一句；下一轮微信把它读回来，UI 却把它判成了对方发的。
        self.store.remember_sent_text("acct-1", self.PEER, "在的，岗位还在招")
        runtime.history[self.PEER].append({"text": "在的，岗位还在招", "is_self": False})
        with capture_logs() as logs:
            messages = await source.fetch("acct-1")
        self.assertEqual([(m.text, m.from_self) for m in messages], [("在的，岗位还在招", True)])
        self.assertTrue(any("自己刚发出去" in entry["event"] for entry in logs), logs)
        # 台账认出来的回声要标成「分身」而不是「专员本人」（2026-09-19：AI 端否则当成老师插话）。
        self.assertEqual([m.from_gateway for m in messages], [True])
        self.assertTrue(format_transcript(messages).startswith("[分身 · "), format_transcript(messages))

    async def test_echo_with_no_is_self_field_at_all(self) -> None:
        """真机上多数行压根没有 is_self / sender 字段——这时更只能靠原文认。"""
        runtime = FakeRuntime(unread=[], history={self.PEER: [{"text": "还招人吗"}]})
        source = self._source(runtime)
        await source.fetch("acct-1")
        self.store.remember_sent_text("acct-1", self.PEER, "  在的，\n岗位还在招  ")
        runtime.history[self.PEER].append({"text": "在的， 岗位还在招"})   # 微信折叠了换行/空白
        messages = await source.fetch("acct-1")
        self.assertEqual([(m.text, m.from_self) for m in messages], [("在的， 岗位还在招", True)])

    async def test_peer_repeating_our_words_is_still_the_peer(self) -> None:
        """认一次销一条：对方原样复读「好的」时，第二次出现的才是对方说的，不能被吞。"""
        runtime = FakeRuntime(unread=[], history={self.PEER: [{"text": "还招人吗", "is_self": False}]})
        source = self._source(runtime)
        await source.fetch("acct-1")
        self.store.remember_sent_text("acct-1", self.PEER, "好的")
        runtime.history[self.PEER].append({"text": "好的", "is_self": False})   # 我们的回声（UI 判错）
        runtime.history[self.PEER].append({"text": "好的", "is_self": False})   # 对方复读
        messages = await source.fetch("acct-1")
        self.assertEqual([m.from_self for m in messages], [True, False])

    async def test_ui_correct_echo_still_consumes_the_record(self) -> None:
        """UI 判对了也要销账，否则记录留到对方哪天说同一句时把真话吞掉。"""
        runtime = FakeRuntime(unread=[], history={self.PEER: [{"text": "还招人吗", "is_self": False}]})
        source = self._source(runtime)
        await source.fetch("acct-1")
        self.store.remember_sent_text("acct-1", self.PEER, "好的")
        runtime.history[self.PEER].append({"text": "好的", "is_self": True})
        await source.fetch("acct-1")
        self.assertFalse(self.store.claim_sent_echo("acct-1", self.PEER, "好的"), "已认领的记录该销掉")

    async def test_first_read_after_plan_outreach(self) -> None:
        """计划先发了第一句、再把人加进名单：第一次读时我们那句得认出来，
        否则 first_seen_fresh 停不住，会把它前面的老历史一并当新消息倒给模型。"""
        self.store.remember_sent_text("acct-1", self.PEER, "您好，看到您在找普工的工作")
        runtime = FakeRuntime(unread=[], history={self.PEER: [
            {"text": "去年聊过的老消息"},
            {"text": "您好，看到您在找普工的工作"},        # 计划发的，UI 没标
        ]})
        messages = await self._source(runtime).fetch("acct-1")
        self.assertEqual(messages, [], "尾部是自己说的 → 一条都不接")

    async def test_records_are_scoped_per_peer(self) -> None:
        runtime = FakeRuntime(unread=[], history={self.PEER: [{"text": "还招人吗", "is_self": False}]})
        source = self._source(runtime)
        await source.fetch("acct-1")
        self.store.remember_sent_text("acct-1", "别的人", "好的")
        runtime.history[self.PEER].append({"text": "好的", "is_self": False})
        messages = await source.fetch("acct-1")
        self.assertEqual([m.from_self for m in messages], [False], "给别人发的话不能拿来认这个人的")

    def test_purge_drops_only_stale_records(self) -> None:
        t0 = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)
        self.store.remember_sent_text("acct-1", self.PEER, "旧", at=t0 - timedelta(days=3))
        self.store.remember_sent_text("acct-1", self.PEER, "新", at=t0)
        self.assertEqual(self.store.purge_sent_echo_before(t0 - timedelta(hours=48)), 1)
        self.assertFalse(self.store.claim_sent_echo("acct-1", self.PEER, "旧"))
        self.assertTrue(self.store.claim_sent_echo("acct-1", self.PEER, "新"))

    async def test_outbound_sender_records_each_sent_segment(self) -> None:
        now = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
        sender = OutboundSender(
            sink=FakeSink(),
            guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
            store=self.store,
            clock=lambda: now,
        )
        await sender.deliver("a", "p", reply_card(["第一句", "第二句"]))
        self.assertTrue(self.store.claim_sent_echo("a", "p", "第一句"))
        self.assertTrue(self.store.claim_sent_echo("a", "p", "第二句"))
        self.assertFalse(self.store.claim_sent_echo("a", "p", "第一句"), "认一次销一条")

    def test_emoji_codes_must_match_literally(self) -> None:
        """线上实测 UIA 把黄脸表情按 `[微笑]` 原文读回来，所以原文带表情就按带表情比；
        对方恰好说了一句没表情的同一句话，不是我们的回声。"""
        self.store.remember_sent_text("a", "p", "早上好[微笑]，看到您在找普工")
        self.assertFalse(self.store.claim_sent_echo("a", "p", "早上好，看到您在找普工"))
        self.assertTrue(self.store.claim_sent_echo("a", "p", "早上好[微笑]，看到您在找普工"))


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

    async def test_outside_the_window_each_round_still_says_so_at_most_once_a_minute(self) -> None:
        """用户 2026-09-30：「为什么轮询的日志没有」——窗外也要报「开始新一轮轮询」，一分钟最多一条。"""
        now = [T0]
        loop = InboundLoop(
            accounts=["acct-1"],
            source=FakeSource([[]]),
            store=self.store,
            handler=lambda *_a: None,
            is_active=lambda _now: False,
            clock=lambda: now[0],
        )
        with capture_logs() as logs:
            await loop.tick()
            now[0] = T0 + timedelta(seconds=30)
            await loop.tick()
            now[0] = T0 + timedelta(seconds=61)
            await loop.tick()
        rounds = [e for e in logs if str(e["event"]).startswith("开始新一轮轮询")]
        self.assertEqual(len(rounds), 2)
        self.assertIn("不在工作时间窗", rounds[0]["event"])

    async def test_the_countdown_says_nothing_when_nothing_is_scheduled(self) -> None:
        """★ 界面上那个「下次读消息」的倒计时，源头在这儿（用户 2026-08-15）。

        窗外一趟都不会读，就别给个会走到 00:00 却什么都不发生的数——宁可留白。
        """
        loop = InboundLoop(
            accounts=["acct-1"],
            source=FakeSource([[]]),
            store=self.store,
            handler=self._nothing,
            is_active=lambda _now: False,
            clock=lambda: T0,
        )
        self.assertIsNone(loop.seconds_to_next_poll(), "还没跑起来就没有「下一趟」")
        self.assertTrue(loop.in_quiet_window())

        loop._next_poll_at = T0 + timedelta(seconds=30)   # 假装排过一趟
        self.assertIsNone(loop.seconds_to_next_poll(), "窗外排了也不会读，倒计时不许照报")

    async def test_the_countdown_follows_the_nap_the_loop_really_took(self) -> None:
        """★ 报的是**真实那一觉**（含抖动、含快慢档），不是照配置算的。

        照配置算的话，热的时候快到 30 秒、抖动又 ±30%，界面会一直和现实差几十秒，
        看着就像坏了。
        """
        loop = InboundLoop(
            accounts=["acct-1"],
            source=FakeSource([[]]),
            store=self.store,
            handler=self._nothing,
            poll_interval_seconds=60.0,
            clock=lambda: datetime.now(timezone.utc),
        )
        task = asyncio.ensure_future(loop.run())
        for _ in range(20):                      # 让它跑完第一趟、排上下一觉
            await asyncio.sleep(0)
            if loop.seconds_to_next_poll() is not None:
                break
        left = loop.seconds_to_next_poll()
        self.assertIsNotNone(left, "跑起来之后就该有排期了")
        self.assertTrue(41.9 <= left <= 78.1, f"{left} 不在 60 秒那一档的抖动范围里")

        loop.stop()
        await task
        self.assertIsNone(loop.seconds_to_next_poll(), "停了就没有「下一趟」")

    async def _nothing(self, *_args: Any) -> None:
        return None

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

class AdaptivePaceTests(StoreTestCase):
    """★ 聊着的时候读得勤一点，聊完自动慢下来（用户 2026-08-14）。

    固定 300 秒两头不讨好：对方问一句最多干等 5 分钟，回得像台机器；没人说话时又照样
    每 5 分钟翻一次微信，白动。**但快也只快到下限为止** —— 30 秒的地板、抖动、工作时间窗
    一个都没动，热的时候只是借用已经允许的那一档。
    """

    def setUp(self) -> None:
        super().setUp()
        self._now = T0

    def _loop(self, **kw) -> InboundLoop:
        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            return None

        return InboundLoop(
            accounts=["acct-1"],
            source=FakeSource([[], [], []]),
            store=self.store,
            handler=handler,
            clock=lambda: self._now,
            poll_interval_seconds=300.0,
            active_poll_interval_seconds=60.0,
            hot_window_seconds=600.0,
            **kw,
        )

    async def test_idle_by_default(self) -> None:
        loop = self._loop()
        self.assertEqual(loop._interval_for(T0), 300.0)

    async def test_peer_talking_switches_to_the_fast_lane(self) -> None:
        loop = self._loop()
        loop._source = FakeSource([[msg("在吗", msg_id="m1")], []])
        await loop.tick()
        self.assertEqual(loop._interval_for(self._now), 60.0)

    async def test_my_own_message_does_not_heat_it_up(self) -> None:
        """自己发的不算「聊着」——否则网关自己回一句就把自己吵醒一轮。"""
        loop = self._loop()
        loop._source = FakeSource([[msg("我先问问他", msg_id="m1", from_self=True)], []])
        await loop.tick()
        self.assertEqual(loop._interval_for(self._now), 300.0)

    async def test_cools_down_after_the_hot_window(self) -> None:
        loop = self._loop()
        loop._source = FakeSource([[msg("在吗", msg_id="m1")], []])
        await loop.tick()
        self.assertEqual(loop._interval_for(self._now + timedelta(seconds=599)), 60.0)
        self.assertEqual(loop._interval_for(self._now + timedelta(seconds=601)), 300.0)

    async def test_ai_can_change_the_pace_but_not_below_the_floor(self) -> None:
        """★ 节拍是防风控红线：AI 端一句「盯紧点」不能把它压到 5 秒。"""
        loop = self._loop()
        pace = loop.set_pace(idle_seconds=120, active_seconds=5, hot_window_seconds=99999, floor=30.0)
        self.assertEqual(pace["idle_seconds"], 120.0)
        self.assertEqual(pace["active_seconds"], 30.0, "低于下限必须被抬上来")
        self.assertEqual(pace["hot_window_seconds"], 3600.0, "热窗不许无限长")

    async def test_the_idle_interval_is_the_full_sweep_interval_on_the_source(self) -> None:
        """慢档间隔同步给读消息那一侧当「全量巡检间隔」——没接上的话快读永远不会发生。"""
        full: list[float] = []

        class _Source(FakeSource):
            def set_full_interval(self, seconds: float) -> None:
                full.append(seconds)

        async def handler(_a: str, _p: str, _t: str, _m) -> None:
            return None

        source = _Source([[]])
        loop = InboundLoop(accounts=["acct-1"], source=source, store=self.store, handler=handler,
                           clock=lambda: self._now, poll_interval_seconds=180.0,
                           active_poll_interval_seconds=30.0)
        self.assertEqual(full[-1], 180.0)
        loop.set_pace(idle_seconds=240, floor=30.0)
        self.assertEqual(full[-1], 240.0, "AI 端改了慢档，巡检间隔要跟着改")

    async def test_fast_lane_never_slower_than_the_idle_one(self) -> None:
        loop = self._loop()
        pace = loop.set_pace(idle_seconds=45, floor=30.0)
        self.assertEqual(pace["active_seconds"], 45.0)


class FirstSightTests(unittest.TestCase):
    """★ 第一次读一个会话时，该处理哪些消息（用户裁决 2026-08-14：「没有首次见到这一说」）。

    老规则是「首次一条都不回，只对齐游标」——那是「先扫未读、被动等消息」时代的设定。
    现在是名单驱动、直接读会话：把一个人加进名单的那一刻，他很可能刚问完话正等着；
    首次不回 = 他永远等不到答复，除非自己再发一条。
    新判据只看**时间 + 谁说的**：尾部连续的、今天的、对方发的那几条。
    """

    def test_the_question_he_just_asked_gets_answered_on_the_very_first_read(self) -> None:
        history = [
            {"text": "在吗", "is_self": False, "timestamp_is_today": True},
            {"text": "还招人吗", "is_self": False, "timestamp_is_today": True},
        ]
        fresh, cursor = split_new_messages(history, None)
        self.assertEqual([m["text"] for m in fresh], ["在吗", "还招人吗"])
        self.assertEqual(cursor, latest_cursor(history))

    def test_history_before_today_is_not_replayed(self) -> None:
        history = [
            {"text": "三个月前问过", "is_self": False, "timestamp_is_today": False},
            {"text": "昨天又问了一遍", "is_self": False, "timestamp_is_today": False},
            {"text": "今天再问一次", "is_self": False, "timestamp_is_today": True},
        ]
        fresh, _ = split_new_messages(history, None)
        self.assertEqual([m["text"] for m in fresh], ["今天再问一次"], "只接今天的，更早的算历史")

    def test_stops_at_the_operator_s_own_message(self) -> None:
        """尾巴是专员自己发的 → 他已经在人工接着了，分身不插话。"""
        history = [
            {"text": "还招人吗", "is_self": False, "timestamp_is_today": True},
            {"text": "招的，明天来看看", "is_self": True, "timestamp_is_today": True},
        ]
        fresh, cursor = split_new_messages(history, None)
        self.assertEqual(fresh, [])
        self.assertEqual(cursor, latest_cursor(history), "不回也要对齐游标")

    def test_tail_is_capped(self) -> None:
        """一屏都是今天的也不至于整屏倒给模型。"""
        history = [
            {"text": f"第{i}条", "is_self": False, "timestamp_is_today": True} for i in range(20)
        ]
        fresh, _ = split_new_messages(history, None)
        self.assertEqual(len(fresh), FIRST_SEEN_TAIL)
        self.assertEqual(fresh[-1]["text"], "第19条", "留的是最新的几条，不是最老的")

    def test_rows_without_a_timestamp_count_as_today(self) -> None:
        """没有时间分隔行时 uia 标不出今天与否 —— 宁可回一条稍旧的，也不能把刚到的咽掉。"""
        fresh, _ = split_new_messages([{"text": "在吗", "is_self": False}], None)
        self.assertEqual([m["text"] for m in fresh], ["在吗"])


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

    def test_same_text_again_after_an_older_copy_scrolls_out_is_new(self) -> None:
        """★ 2026-09-29 线上：候选人又说一声「你好」，读完报「没有新消息」、分身不回。

        窗口是尾部固定条数，新消息一来最前面就滑出一条。滑出去的正好是一句更早的「你好」时，
        按「第几个同文」记的老游标会认到新来的这句身上。
        """
        first = [{"text": "你好"}, {"text": "在的", "is_self": True}, {"text": "x"},
                 {"text": "y", "is_self": True}, {"text": "你好"}]
        _, cursor = split_new_messages(first, None)
        second = first[1:] + [{"text": "你好"}]
        fresh, _ = split_new_messages(second, cursor)
        self.assertEqual([m["text"] for m in fresh], ["你好"])
        # 同一窗再读一遍：什么都不是新的
        _, cursor2 = split_new_messages(second, cursor)
        self.assertEqual(split_new_messages(second, cursor2)[0], [])

    def test_same_text_twice_in_a_row_each_counts_once(self) -> None:
        # 限度：连着三句以上一模一样、中间没有别人插话，前文也全一样，就分不开了（真实对话里中间有分身的回复）。
        rows = [{"text": "a"}, {"text": "好的"}]
        _, cursor = split_new_messages(rows, None)
        for _ in range(2):
            rows = rows + [{"text": "好的"}]
            fresh, cursor = split_new_messages(rows, cursor)
            self.assertEqual([m["text"] for m in fresh], ["好的"])

    def test_dup_index_cursor_from_before_the_upgrade_still_matches(self) -> None:
        """老格式（带同文序号、没有前文）的游标认一次，之后写成新格式。"""
        rows = [{"text": "图片"}, {"text": "图片"}]
        old_cursor = message_signature(rows[0])
        fresh, cursor = split_new_messages(rows + [{"text": "新消息"}], old_cursor)
        self.assertEqual([m["text"] for m in fresh], ["图片", "新消息"])
        self.assertIn("~", cursor)

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
