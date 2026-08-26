"""089 · 分身级人工接管：本机那道总闸。

★ 这道闸是整个 089 里**唯一断网也成立**的一层。业务端接口没通、AI 端连不上，
  招聘老师按下开关之后那台电脑就是不发消息——这一点不依赖任何网络。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from wxclient.gateway.guard import OutboundGuard
from wxclient.gateway.sessions import GatewayStore

ACCOUNT = "acc-1"
PEER = "王建国"
NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)


class ContextModeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.store = GatewayStore(os.path.join(self.dir, "gateway.db"))

    def test_default_is_delegated(self) -> None:
        """★ 从来没设过 = 分身在管。接管是例外状态，装好就接管等于装上就不干活。"""
        row = self.store.context_mode(ACCOUNT)
        self.assertEqual(row.context_mode, "delegated")
        self.assertFalse(row.taken_over)

    def test_it_survives_a_restart(self) -> None:
        """★ 重启读回是这张表存在的**唯一理由**。"""
        self.store.set_context_mode(ACCOUNT, "take_over", reason="我自己来")
        reopened = GatewayStore(os.path.join(self.dir, "gateway.db"))
        row = reopened.context_mode(ACCOUNT)
        self.assertTrue(row.taken_over)
        self.assertEqual(row.reason, "我自己来")

    def test_a_fresh_switch_still_owes_a_report(self) -> None:
        """刚按下的那一下还没报给业务端——重启之后要补报（无心跳契约）。"""
        self.store.set_context_mode(ACCOUNT, "take_over", reason="x")
        self.assertIsNone(self.store.context_mode(ACCOUNT).reported_at)
        self.store.mark_context_mode_reported(ACCOUNT)
        self.assertIsNotNone(self.store.context_mode(ACCOUNT).reported_at)

    def test_marking_reported_never_rewrites_the_state(self) -> None:
        """上报回执只该动 `reported_at`，碰状态就等于让网络决定这台机器发不发消息。"""
        self.store.set_context_mode(ACCOUNT, "take_over", reason="x")
        self.store.mark_context_mode_reported(ACCOUNT)
        self.assertTrue(self.store.context_mode(ACCOUNT).taken_over)

    def test_a_junk_value_reads_back_as_delegated(self) -> None:
        """库里被写脏了也不能变成「永久接管」——那是没人能从界面解开的状态。"""
        self.store.set_context_mode(ACCOUNT, "whatever")
        self.assertEqual(self.store.context_mode(ACCOUNT).context_mode, "delegated")


class TakeoverGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.store = GatewayStore(os.path.join(self.dir, "gateway.db"))
        self.guard = OutboundGuard(self.store, max_sends_per_hour=30, active_hours="00:00-23:59")

    def take_over(self, reason: str = "老师自己在用") -> None:
        self.store.set_context_mode(ACCOUNT, "take_over", reason=reason)

    def test_automatic_replies_are_blocked(self) -> None:
        """① 自动回复被拦。"""
        self.assertTrue(self.guard.check(ACCOUNT, PEER, now=NOW).allowed)
        self.take_over()
        decision = self.guard.check(ACCOUNT, PEER, now=NOW)
        self.assertFalse(decision.allowed)
        self.assertIn("人工接管", decision.reason)

    def test_ai_issued_commands_hit_the_same_gate(self) -> None:
        """② AI 下发的 send_message / add_friend 走的是同一道闸——一处加闸两条路全挡。"""
        self.take_over()
        for action in ("message", "add_friend", "send_files"):
            with self.subTest(action=action):
                self.assertFalse(self.guard.reserve(ACCOUNT, PEER, now=NOW, action=action).allowed)

    def test_reading_messages_keeps_going(self) -> None:
        """③ ★ 读侧零误伤：接管期间人真回了话，那条「未回复跟进」照样要被取消。"""
        self.take_over()
        self.assertTrue(self.guard.allow_read(ACCOUNT, "read_messages", now=NOW).allowed)

    def test_the_refusal_is_temporary_so_nothing_gets_settled(self) -> None:
        """④ ★★ `temporary=True`：接管两小时 ≠ 丢两小时的人。

        上层据此把这批消息 defer 掉重投，而不是当作「已处理」销账。
        这一位设错，恢复之后就是「攒了一天的话一次性喷出去」或者「那些人再也没人联系」。
        """
        self.take_over()
        reservation = self.guard.reserve(ACCOUNT, PEER, now=NOW)
        self.assertFalse(reservation.allowed)
        self.assertTrue(reservation.temporary)

    def test_the_gate_lifts_the_moment_it_is_handed_back(self) -> None:
        self.take_over()
        self.assertFalse(self.guard.check(ACCOUNT, PEER, now=NOW).allowed)
        self.store.set_context_mode(ACCOUNT, "delegated")
        self.assertTrue(self.guard.check(ACCOUNT, PEER, now=NOW).allowed)

    def test_it_takes_no_quota_to_be_blocked(self) -> None:
        """★ 被接管拦下不该消耗额度——否则接管两小时回来发现配额没了。"""
        self.take_over()
        self.guard.reserve(ACCOUNT, PEER, now=NOW)
        self.store.set_context_mode(ACCOUNT, "delegated")
        self.assertEqual(self.store.sends_in_last_hour(ACCOUNT, NOW), 0)

    def test_an_unreadable_store_reads_as_not_taken_over(self) -> None:
        """★ fail 方向：读不到当**没接管**。库抖一下就让整台机器停摆是更糟的失败。"""

        class Broken:
            def __getattr__(self, name):  # noqa: ANN001, ANN204
                raise RuntimeError("db is down")

        guard = OutboundGuard(Broken(), active_hours="00:00-23:59")
        self.assertIsNone(guard._takeover_denial(ACCOUNT))


if __name__ == "__main__":
    unittest.main()
