"""频次守门：**先预留后执行**（2026-08-18 审计 P0-2/P0-3 + 读类不限频）。

老写法是「判一次 → 动微信 → 记一次」，两次数之间隔着一整个微信动作，于是：

- **并发下发**时一批指令全读到同一个旧计数后集体放行（实测并发 20 条加好友
  done=20/rejected=0，串行才是 5/15）——加好友红线在真实形态下等于不存在；
- 一条 `accept_new_friends` 通过 5 个人**只记 1 次**，那档配额被放宽 5 倍；
- 一张回复卡拆成 N 段，判一次发 N 条，按 1 记账、按 N 打扰；
- 读类只要拒绝理由里没有「时间窗」就一律放行，`check_new_friends` 想查多勤查多勤。

这四条是同一个病，所以修法也只有一个：**额度在动微信之前占住，做完按真实用量结算。**
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wxclient.gateway import CommandExecutor, GatewayStore, OutboundGuard
from wxclient.gateway.guard import ACTION_FRIEND, ACTION_MESSAGE, RateLimit
from wxclient.gateway.outbound import OutboundSender

T0 = datetime(2026, 8, 13, 14, 0, tzinfo=timezone(timedelta(hours=8)))
ACCOUNT = "zk_weixin"


class SlowRuntime:
    """假底座。`delay` 用来把「判」和「记」之间那段真实窗口撑开。"""

    def __init__(self, *, delay: float = 0.0, result: dict[str, Any] | None = None,
                 boom: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._delay, self._result, self._boom = delay, result or {"ok": True}, boom

    async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._boom:
            raise RuntimeError("微信没登录")
        self.calls.append(dict(payload or {}))
        return {"result": dict(self._result), "task_id": f"t{len(self.calls)}"}


class ReservationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.now = T0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _guard(self, **kw: Any) -> OutboundGuard:
        kw.setdefault("active_hours", "00:00-23:59")
        kw.setdefault("max_sends_per_hour", 100)
        return OutboundGuard(self.store, **kw)

    def _executor(self, runtime: SlowRuntime, **kw: Any) -> CommandExecutor:
        return CommandExecutor(
            runtime=runtime,
            store=self.store,
            guard=self._guard(**{k: kw.pop(k) for k in list(kw) if k in
                                {"active_hours", "max_sends_per_hour", "action_limits"}}),
            account_id=ACCOUNT,
            clock=lambda: self.now,
            allow_real_actions=True,
            confirmation="确认",
            **kw,
        )

    def _friend_sends(self) -> int:
        return self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action=ACTION_FRIEND)


class ConcurrencyTests(ReservationTestCase):
    async def test_twenty_at_once_still_only_five_get_through(self) -> None:
        """★ 这条是整个改动的理由：并发不再能把配额冲开。

        `delay` 撑开的正是老实现里「已经放行、还没记账」那段窗口。

        用发消息来量**配额**这一道：加好友后来又多了一道 300 秒的节拍闸
        （`guard.WRITE_PACING`），并发下它先拦到只剩 1 条，配额那道就量不出来了。
        两道闸各测各的，见下面一条。
        """
        runtime = SlowRuntime(delay=0.02)
        executor = self._executor(runtime, max_sends_per_hour=5)
        results = await asyncio.gather(*(
            executor.execute({"command_id": f"c{i}", "op": "send_message",
                              "args": {"to": f"人{i}", "text": "在吗"}})
            for i in range(20)
        ))
        done = [r for r in results if r.status == "done"]
        self.assertEqual(len(done), 5, "一小时 5 条，并发也必须是 5 条")
        self.assertEqual(len(runtime.calls), 5, "被拒的那 15 条一次微信都不许动")
        self.assertEqual(
            self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action=ACTION_MESSAGE), 5
        )

    async def test_twenty_add_friends_at_once_get_exactly_one(self) -> None:
        """★ 加好友并发下只过 1 个：配额说「一小时 5 个」，节拍说「300 秒 1 个」。

        两道闸取更严的那一道。2026-08-27 真机上只有配额，于是 81 秒里加了两次、
        两次都「没搜到这个号」，微信随即弹出安全验证。
        """
        runtime = SlowRuntime(delay=0.02)
        executor = self._executor(runtime)
        results = await asyncio.gather(*(
            executor.execute({"command_id": f"c{i}", "op": "add_friend",
                              "args": {"number": f"wxid_{i}"}})
            for i in range(20)
        ))
        self.assertEqual(len([r for r in results if r.status == "done"]), 1)
        self.assertEqual(len(runtime.calls), 1, "被拒的那 19 条一次微信都不许动")
        self.assertEqual(self._friend_sends(), 1, "被节拍挡下的不许白占配额")


class BatchAccountingTests(ReservationTestCase):
    async def test_accepting_five_people_costs_five_slots(self) -> None:
        """一条指令通过 5 个人 = 5 次加好友，不是 1 次。"""
        runtime = SlowRuntime(result={"items": ["甲", "乙", "丙", "丁", "戊"]})
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "accept_new_friends", "args": {"limit": 5},
        })
        self.assertEqual(result.status, "done", result.reason)
        self.assertEqual(self._friend_sends(), 5)

    async def test_only_what_really_happened_is_charged(self) -> None:
        """预留 5、实际只通过 2 —— 剩下 3 个额度要还回去。"""
        runtime = SlowRuntime(result={"items": ["甲", "乙"]})
        await self._executor(runtime).execute({
            "command_id": "c1", "op": "accept_new_friends", "args": {"limit": 5},
        })
        self.assertEqual(self._friend_sends(), 2)

    async def test_a_failed_action_costs_nothing(self) -> None:
        """动作抛错 = 一件没做成，额度原样还回去，不能白扣。"""
        result = await self._executor(SlowRuntime(boom=True)).execute({
            "command_id": "c1", "op": "add_friend", "args": {"number": "wxid_1"},
        })
        self.assertEqual(result.status, "failed")
        self.assertEqual(self._friend_sends(), 0)

    async def test_it_does_fewer_rather_than_none_when_room_is_tight(self) -> None:
        """「最多通过 5 个」而只剩 2 个额度 —— 减量做 2 个，别整条拒掉。"""
        for i in range(3):
            self.store.record_send(ACCOUNT, f"p{i}", self.now, action=ACTION_FRIEND)
        runtime = SlowRuntime(result={"items": ["甲", "乙"]})
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "accept_new_friends", "args": {"limit": 5},
        })
        self.assertEqual(result.status, "done", result.reason)
        self.assertEqual(runtime.calls[-1]["limit"], 2, "减量了就要把上限跟着调下来")
        self.assertEqual(self._friend_sends(), 5)


class ReadPacingTests(ReservationTestCase):
    async def test_checking_new_friends_has_a_five_minute_floor(self) -> None:
        """★ 查好友申请 ≥300s。原来读类一律放行，串行 50 条全部通过。"""
        executor = self._executor(SlowRuntime())
        first = await executor.execute({"command_id": "c1", "op": "check_new_friends", "args": {}})
        second = await executor.execute({"command_id": "c2", "op": "check_new_friends", "args": {}})
        self.assertEqual(first.status, "done", first.reason)
        self.assertEqual(second.status, "rejected")
        self.assertIn("300", second.reason)

        self.now = T0 + timedelta(seconds=301)
        third = await executor.execute({"command_id": "c3", "op": "check_new_friends", "args": {}})
        self.assertEqual(third.status, "done", third.reason)

    async def test_reading_messages_has_a_thirty_second_floor(self) -> None:
        executor = self._executor(SlowRuntime())
        await executor.execute({"command_id": "c1", "op": "get_unread_messages", "args": {}})
        blocked = await executor.execute({"command_id": "c2", "op": "get_unread_messages", "args": {}})
        self.assertEqual(blocked.status, "rejected")

        self.now = T0 + timedelta(seconds=31)
        ok = await executor.execute({"command_id": "c3", "op": "get_unread_messages", "args": {}})
        self.assertEqual(ok.status, "done", ok.reason)

    async def test_reads_do_not_eat_the_write_quota(self) -> None:
        """读归读：它不占加好友/私聊的额度，只受间隔约束。"""
        await self._executor(SlowRuntime()).execute(
            {"command_id": "c1", "op": "get_unread_messages", "args": {}})
        self.assertEqual(self.store.sends_in_last_hour(ACCOUNT, self.now), 0)


class OutboundSegmentTests(ReservationTestCase):
    def _sender(self, sink, **kw: Any) -> OutboundSender:
        return OutboundSender(sink=sink, guard=self._guard(**kw),
                              store=self.store, clock=lambda: self.now)

    async def test_every_segment_costs_one_slot(self) -> None:
        sent: list[str] = []

        class Sink:
            async def send(self, _a, _p, text):
                sent.append(text)
                return f"t{len(sent)}"

        card = {"type": "info", "body": {"kind": "wechat_reply",
                                         "reply_segments": ["一", "二", "三"]}}
        result = await self._sender(Sink()).deliver(ACCOUNT, "王建国", card)
        self.assertTrue(result.delivered)
        self.assertEqual(len(sent), 3)
        self.assertEqual(
            self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action=ACTION_MESSAGE), 3,
            "三段就是三次打扰，不能按一次记",
        )

    async def test_a_reply_is_never_sent_half(self) -> None:
        """只剩 2 个额度而要发 3 段 —— 整条不发。发一半比不发更糟。"""
        sent: list[str] = []

        class Sink:
            async def send(self, _a, _p, text):  # pragma: no cover - 不该被调到
                sent.append(text)
                return "t"

        sender = self._sender(Sink(), action_limits={ACTION_MESSAGE: RateLimit(2, 3600.0)})
        card = {"type": "info", "body": {"kind": "wechat_reply",
                                         "reply_segments": ["一", "二", "三"]}}
        result = await sender.deliver(ACCOUNT, "王建国", card)
        self.assertFalse(result.delivered)
        self.assertEqual(sent, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
