"""`wxclient.human_activity`：有人用鼠标键盘时微信 UI 操作停手，停手够久自动恢复。

钩子本身（Windows 低级钩子、注入输入不算人）走真机验收；这里验判定、队列闸和等待时限。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wxclient import human_activity
from wxclient.human_activity import HumanActivity
from wxclient.queue import OperationWorker, TaskStatus, TaskStore, TaskType


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class HumanActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.activity = HumanActivity(idle_seconds=5, clock=self.clock)

    def test_还没见过真人输入不暂停(self) -> None:
        self.assertFalse(self.activity.busy())

    def test_有动静就暂停_停够秒数自动恢复(self) -> None:
        self.activity.note(0)
        self.assertTrue(self.activity.busy())
        self.clock.now += 4.9
        self.assertTrue(self.activity.busy())
        self.clock.now += 0.1
        self.assertFalse(self.activity.busy())

    def test_新动静重新计时(self) -> None:
        self.activity.note(0)
        self.clock.now += 4
        self.activity.note(0)  # 滚了一下滚轮
        self.clock.now += 4
        self.assertTrue(self.activity.busy())
        self.clock.now += 1
        self.assertFalse(self.activity.busy())

    def test_按住不放一直暂停(self) -> None:
        self.activity.note(1)
        self.clock.now += 60
        self.assertTrue(self.activity.busy())
        self.activity.note(0)  # 松开
        self.clock.now += 5
        self.assertFalse(self.activity.busy())

    def test_核实松开不算新动静(self) -> None:
        self.activity.note(1)
        self.clock.now += 60
        self.activity.released(0)  # 锁屏时松的手，钩子没看到，子进程核实后补报
        self.assertFalse(self.activity.busy())

    def test_暂停与恢复各打一条日志(self) -> None:
        with mock.patch.object(human_activity, "_log") as log:
            self.activity.note(0)
            self.activity.busy()
            self.activity.busy()
            self.clock.now += 5
            self.activity.busy()
            self.activity.busy()
        events = [call.args[0] for call in log.info.call_args_list]
        self.assertEqual(events, ["检测到有人在用鼠标键盘，微信自动操作暂停", "鼠标键盘已停下，微信自动操作恢复"])

    def test_wait_idle_等到停手才返回(self) -> None:
        self.activity.note(0)
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            self.clock.now += seconds

        self.activity.wait_idle(sleep=sleep)
        self.assertAlmostEqual(sum(slept), 5.0, delta=0.21)
        self.assertFalse(self.activity.busy())


class _Api:
    def __init__(self) -> None:
        self.calls = 0

    def search_contacts(self, query: str, limit: int) -> dict:
        self.calls += 1
        return {"action": "search_contacts", "query": query}


class QueueGateTests(unittest.TestCase):
    """有人在用电脑：任务原样放回、不算一次尝试；停手后照常执行。"""

    def test_有人在用时任务放回队列_停手后执行(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "tasks.db")
            api = _Api()
            worker = OperationWorker(store, api)  # type: ignore[arg-type]
            task = store.create_task(TaskType.SEARCH_CONTACTS, {"query": "张三", "limit": 5}, "default")

            # 放回后隔多久再取：测试里拨成 0，免得等
            with mock.patch("wxclient.queue.worker.HUMAN_BUSY_RETRY_SECONDS", 0):
                with mock.patch.object(human_activity, "busy", return_value=True):
                    self.assertFalse(worker.run_once())
                held = store.get(task.id)
                self.assertEqual(held.status, TaskStatus.PENDING)  # type: ignore[union-attr]
                self.assertEqual(held.attempts, 0)  # type: ignore[union-attr]
                self.assertEqual(api.calls, 0)

                with mock.patch.object(human_activity, "busy", return_value=False):
                    self.assertTrue(worker.run_once())
            self.assertEqual(store.get(task.id).status, TaskStatus.SUCCEEDED)  # type: ignore[union-attr]
            self.assertEqual(api.calls, 1)


class WaitDeadlineTests(unittest.TestCase):
    """任务被压着的那段时间不算进网关的等待时限，否则人一用久，回复就被记成「没确认发出」。"""

    def test_暂停期间不计入等待时限(self) -> None:
        from wxclient.runtime import WeChatRuntime

        runtime = WeChatRuntime.__new__(WeChatRuntime)
        pending = mock.Mock(status=TaskStatus.PENDING)
        done = mock.Mock(status=TaskStatus.SUCCEEDED)
        polls = {"n": 0}

        def get(_task_id: str):
            polls["n"] += 1
            return done if polls["n"] > 8 else pending

        runtime.store = mock.Mock(get=get)
        runtime.task_to_dict = lambda task: {"status": task.status.value}  # type: ignore[method-assign]
        with mock.patch.object(human_activity, "busy", return_value=True), \
                mock.patch("wxclient.runtime.time.sleep", lambda _s: None), \
                mock.patch("wxclient.runtime.time.monotonic", side_effect=[0.0] + [0.15 * i for i in range(1, 50)]):
            result = runtime.wait_for_task("t1", timeout_seconds=0.1)
        self.assertEqual(result, {"status": "succeeded"})


class DisabledGuardTests(unittest.TestCase):
    def test_没开监听时从不暂停(self) -> None:
        self.assertFalse(human_activity.busy())
        human_activity.wait_idle()  # 立刻返回

    def test_0秒表示关闭(self) -> None:
        with mock.patch.object(HumanActivity, "start") as start:
            human_activity.start(0)
        start.assert_not_called()
        self.assertFalse(human_activity.busy())


if __name__ == "__main__":
    unittest.main()
