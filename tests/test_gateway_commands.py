"""AI 端下发微信能力指令（066 Phase 1）——白名单 / 守门 / 幂等 / 回执。

全部在 Linux 跑，不碰真微信：runtime 用假实现，只断言「投了什么任务、拒了什么、拒的理由」。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wxclient.gateway import CommandExecutor, GatewayStore, OutboundGuard
from wxclient.queue import TaskType

BEIJING = timezone(timedelta(hours=8))
WORKDAY = datetime(2026, 8, 13, 14, 0, tzinfo=BEIJING)      # 工作日下午两点
MIDNIGHT = datetime(2026, 8, 13, 3, 0, tzinfo=BEIJING)      # 凌晨三点
ACCOUNT = "zk_weixin"


class FakeRuntime:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[tuple[TaskType, dict[str, Any], str]] = []
        self._boom = boom

    async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
        if self._boom:
            raise RuntimeError("微信没登录")
        self.calls.append((task_type, dict(payload or {}), confirmation))
        return {"result": {"ok": True}, "task_id": f"t{len(self.calls)}"}


class CommandTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.now = WORKDAY

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, runtime: FakeRuntime, **kwargs: Any) -> CommandExecutor:
        kwargs.setdefault("allow_real_actions", True)
        kwargs.setdefault("confirmation", "确认")
        return CommandExecutor(
            runtime=runtime,
            store=self.store,
            guard=OutboundGuard(
                self.store,
                max_sends_per_hour=kwargs.pop("max_sends_per_hour", 20),
                active_hours=kwargs.pop("active_hours", "09:00-21:00"),
            ),
            account_id=ACCOUNT,
            clock=lambda: self.now,
            **kwargs,
        )


class WhitelistTests(CommandTestCase):
    async def test_known_op_is_translated_to_a_queue_task(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message",
            "args": {"to": "张三", "text": "面试改到明天下午三点"},
        })
        self.assertEqual(result.status, "done")
        task_type, payload, confirmation = runtime.calls[-1]
        self.assertIs(task_type, TaskType.SEND_MESSAGE)
        self.assertEqual(payload["friend"], "张三")
        self.assertEqual(payload["text"], "面试改到明天下午三点")
        self.assertFalse(payload["dry_run"])
        self.assertEqual(confirmation, "确认", "真实写操作必须带确认短语")

    async def test_unknown_op_is_rejected_without_touching_wechat(self) -> None:
        """地基不信任上游：AI 端推来没见过的 op，一个微信动作都不许做。"""
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "delete_everything", "args": {},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("不支持的能力", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_watch_chats_is_deliberately_not_exposed(self) -> None:
        """常驻扫描属于入站循环，被一条指令占住会把正常收消息饿死。"""
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "watch_chats", "args": {"friend": "张三"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertEqual(runtime.calls, [])

    async def test_missing_required_arg_is_rejected_not_guessed(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("text", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_dry_run_when_real_actions_are_off(self) -> None:
        runtime = FakeRuntime()
        executor = self._executor(runtime, allow_real_actions=False)
        await executor.execute({"command_id": "c1", "op": "post_moments",
                                "args": {"text": "招 5 个普工，日结 200"}})
        _task_type, payload, confirmation = runtime.calls[-1]
        self.assertTrue(payload["dry_run"])
        self.assertEqual(confirmation, "", "没开真实动作就不该带确认短语")


class CommandGuardTests(CommandTestCase):
    async def test_write_blocked_outside_active_hours(self) -> None:
        runtime = FakeRuntime()
        self.now = MIDNIGHT
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("时间窗", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_read_also_blocked_outside_active_hours(self) -> None:
        """半夜翻联系人一样是「机器在跑」，读操作同样不许在窗外动。"""
        runtime = FakeRuntime()
        self.now = MIDNIGHT
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "search_contacts", "args": {"query": "张"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertEqual(runtime.calls, [])

    async def test_read_ignores_blacklist_and_rate_limit(self) -> None:
        """读不打扰任何人：对方在黑名单里、发送额度用光了，都不该挡住「查一下资料」。"""
        runtime = FakeRuntime()
        self.store.blacklist(ACCOUNT, "张三", "说了别联系")
        for _ in range(30):
            self.store.record_send(ACCOUNT, "张三", self.now - timedelta(minutes=1))
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "get_contact_profile", "args": {"friend": "张三"},
        })
        self.assertEqual(result.status, "done", result.reason)

    async def test_write_to_blacklisted_peer_is_rejected(self) -> None:
        runtime = FakeRuntime()
        self.store.blacklist(ACCOUNT, "张三", "说了别联系")
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("黑名单", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_active_send_counts_toward_the_hourly_limit(self) -> None:
        """主动触达要和被动回复共用同一个额度，否则限频等于没限。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime, max_sends_per_hour=2)
        for i in range(2):
            got = await executor.execute({
                "command_id": f"c{i}", "op": "send_message",
                "args": {"to": f"候选人{i}", "text": "你好"},
            })
            self.assertEqual(got.status, "done")
        blocked = await executor.execute({
            "command_id": "c9", "op": "send_message", "args": {"to": "候选人9", "text": "你好"},
        })
        self.assertEqual(blocked.status, "rejected")
        self.assertIn("上限", blocked.reason)


class IdempotencyTests(CommandTestCase):
    async def test_same_command_id_executes_once(self) -> None:
        """重推（网络重试/网关重连补推）绝不能再发一遍消息。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        frame = {"command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "你好"}}
        first = await executor.execute(frame)
        second = await executor.execute(dict(frame))
        self.assertEqual(first.status, "done")
        self.assertEqual(second.status, "done")
        self.assertEqual(len(runtime.calls), 1, "同一条指令被执行了两次")

    async def test_rejection_is_also_remembered(self) -> None:
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        frame = {"command_id": "c1", "op": "send_message", "args": {"to": "张三"}}
        await executor.execute(frame)
        again = await executor.execute(dict(frame))
        self.assertEqual(again.status, "rejected")
        self.assertEqual(runtime.calls, [])

    async def test_missing_command_id_is_rejected(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({"op": "send_message", "args": {}})
        self.assertEqual(result.status, "rejected")
        self.assertIn("command_id", result.reason)


class FailureTests(CommandTestCase):
    async def test_execution_failure_is_reported_not_swallowed(self) -> None:
        runtime = FakeRuntime(boom=True)
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "你好"},
        })
        self.assertEqual(result.status, "failed")
        self.assertIn("微信没登录", result.reason)
        frame = result.to_frame_data()
        self.assertEqual(frame["command_id"], "c1")
        self.assertEqual(frame["status"], "failed")
        self.assertTrue(frame["reason"])


if __name__ == "__main__":
    unittest.main()
