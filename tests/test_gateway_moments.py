"""本机朋友圈巡看循环（`MomentsWatcher`）：只碰新动态、点赞走共享的那本账、评论交给 AI 端。

重点是几条**不该发生**的路：节拍没到不点但照看、失败不前移游标、没素材不交 AI 端、
dry-run 空壳不动游标。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import beijing_time  # noqa: F401 —— 时间窗按本机时间比，用例按北京时间写

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway import GatewayStore
from wxclient.gateway.guard import ACTION_MOMENTS_LIKE, OutboundGuard, RateLimit
from wxclient.gateway.moments import MIN_POLL_INTERVAL_SECONDS, MomentsWatcher
from wxclient.queue import TaskType

ACCOUNT = "wxid_test"
T0 = datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc)  # 北京 10:00
POSTS = (
    {"content": "张三 今天招工", "posted_at": "5分钟前", "photos": 3, "videos": 0, "key": "张三今天招工"},
    {"content": "", "posted_at": "6分钟前", "photos": 9, "videos": 0, "key": "包含9张图片"},
    {"content": "李四 新到一批岗位", "posted_at": "2小时前", "photos": 0, "videos": 0, "key": "李四新到一批岗位"},
)


class _Runtime:
    def __init__(self, result=None, status="succeeded", boom=False, timed_out=False):
        self._result = result
        self._status = status
        self._boom = boom
        self._timed_out = timed_out
        self.calls: list[tuple] = []

    async def submit(self, task_type, payload, *, confirmation=""):
        self.calls.append((task_type, dict(payload), confirmation))
        if self._boom:
            raise RuntimeError("微信没起来")
        result = self._result
        if result is None:
            result = {
                "dry_run": False, "posts": list(POSTS), "liked": min(2, int(payload.get("like_limit", 0))),
                "already_liked": 0, "scanned": 3, "first_key": "张三今天招工", "stopped": "older",
            }
        if self._timed_out:  # `WeChatRuntime.wait_for_task` 超时的形状：状态还没终结 + timed_out
            return {"status": "running", "result": None, "timed_out": True}
        return {"status": self._status, "result": result}


class MomentsWatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.now = T0
        self.guard = OutboundGuard(
            self.store, max_sends_per_hour=100, active_hours="00:00-23:59",
            action_limits={ACTION_MOMENTS_LIKE: RateLimit(20, 86400)},
            write_intervals={"moments_like_write": 600.0},
        )
        self.handed: list[tuple[str, list]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def watcher(self, runtime=None, **kwargs) -> MomentsWatcher:
        async def on_new_posts(account_id, posts):
            self.handed.append((account_id, posts))

        params = {
            "accounts": [ACCOUNT], "runtime": runtime or _Runtime(), "store": self.store,
            "guard": self.guard, "interval_seconds": 900.0, "auto_like": True,
            "auto_comment": True, "comment_material": "本周新增夜班岗，欢迎推荐",
            "allow_real_actions": True, "confirmation": "我确认", "on_new_posts": on_new_posts,
            "clock": lambda: self.now,
        }
        params.update(kwargs)
        return MomentsWatcher(**params)

    def test_默认关_间隔0或两样都没开都不起(self) -> None:
        self.assertFalse(self.watcher(interval_seconds=0).enabled)
        self.assertFalse(self.watcher(auto_like=False, auto_comment=False).enabled)
        self.assertFalse(self.watcher(auto_like=False, comment_material="  ").enabled, "没素材=评论没开")
        self.assertTrue(self.watcher(auto_like=False).enabled)
        self.assertTrue(self.watcher(auto_comment=False).enabled)

    def test_间隔有地板(self) -> None:
        self.assertEqual(self.watcher(interval_seconds=30).interval_seconds, MIN_POLL_INTERVAL_SECONDS)

    async def test_一趟_点赞额度先占后结算_游标前移_有字的动态交给AI端(self) -> None:
        runtime = _Runtime()
        posts = await self.watcher(runtime).tick()
        self.assertEqual(len(posts), 3)
        task_type, payload, confirmation = runtime.calls[0]
        self.assertEqual(task_type, TaskType.SCAN_MOMENTS)
        self.assertEqual(payload, {"limit": 10, "like_limit": 20, "until_key": "", "dry_run": False})
        self.assertEqual(confirmation, "我确认")
        self.assertEqual(self.store.moment_cursor(ACCOUNT), "张三今天招工")
        # 占了 20、实际点了 2 → 账上只记 2
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(days=1), action=ACTION_MOMENTS_LIKE)
        self.assertEqual(used, 2)
        self.assertEqual(len(self.handed), 1)
        account, handed = self.handed[0]
        self.assertEqual(account, ACCOUNT)
        self.assertEqual([p["content"] for p in handed], ["张三 今天招工", "李四 新到一批岗位"], "纯图的不交")
        self.assertNotIn("key", handed[0], "键是本机的事，不给 AI 端")

    async def test_第二趟把游标带上(self) -> None:
        runtime = _Runtime()
        watcher = self.watcher(runtime)
        await watcher.tick()
        self.now += timedelta(minutes=20)
        await watcher.tick()
        self.assertEqual(runtime.calls[1][1]["until_key"], "张三今天招工")

    async def test_节拍没到_只看不点_额度还回去(self) -> None:
        runtime = _Runtime()
        watcher = self.watcher(runtime)
        await watcher.tick()
        self.now += timedelta(minutes=2)  # < 600s
        await watcher.tick()
        self.assertEqual(runtime.calls[1][1]["like_limit"], 0)
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(days=1), action=ACTION_MOMENTS_LIKE)
        self.assertEqual(used, 2, "第二趟一个都没占住")

    async def test_日配额用完_只看不点(self) -> None:
        for _ in range(20):
            self.store.record_send(ACCOUNT, "", self.now, action=ACTION_MOMENTS_LIKE)
        runtime = _Runtime()
        await self.watcher(runtime).tick()
        self.assertEqual(runtime.calls[0][1]["like_limit"], 0)

    async def test_失败不前移游标_额度全还(self) -> None:
        runtime = _Runtime(boom=True)
        posts = await self.watcher(runtime).tick()
        self.assertEqual(posts, [])
        self.assertEqual(self.store.moment_cursor(ACCOUNT), "")
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(days=1), action=ACTION_MOMENTS_LIKE)
        self.assertEqual(used, 0)
        self.assertEqual(self.handed, [])

    async def test_队列报失败也不前移游标(self) -> None:
        runtime = _Runtime(status="failed", result={})
        await self.watcher(runtime).tick()
        self.assertEqual(self.store.moment_cursor(ACCOUNT), "")

    async def test_dry_run空壳不动游标不交AI端(self) -> None:
        runtime = _Runtime(result={"dry_run": True, "posts": [], "liked": 0, "first_key": ""})
        await self.watcher(runtime, allow_real_actions=False).tick()
        self.assertTrue(runtime.calls[0][1]["dry_run"])
        self.assertEqual(self.store.moment_cursor(ACCOUNT), "")
        self.assertEqual(self.handed, [])

    async def test_没开自动评论_不交AI端_但照样点赞(self) -> None:
        runtime = _Runtime()
        await self.watcher(runtime, auto_comment=False).tick()
        self.assertEqual(runtime.calls[0][1]["like_limit"], 20)
        self.assertEqual(self.handed, [])

    async def test_只开评论_不点赞不占额度(self) -> None:
        runtime = _Runtime()
        await self.watcher(runtime, auto_like=False).tick()
        self.assertEqual(runtime.calls[0][1]["like_limit"], 0)
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(days=1), action=ACTION_MOMENTS_LIKE)
        self.assertEqual(used, 0)
        self.assertEqual(len(self.handed), 1)

    async def test_非工作时段一趟都不开(self) -> None:
        runtime = _Runtime()
        self.now = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)  # 北京 02:00
        await self.watcher(runtime, active_hours="09:00-21:00").tick()
        self.assertEqual(runtime.calls, [])

    async def test_AI端没接住_不影响这趟结果(self) -> None:
        async def boom(_account, _posts):
            raise RuntimeError("AI 端连不上")

        runtime = _Runtime()
        posts = await self.watcher(runtime, on_new_posts=boom).tick()
        self.assertEqual(len(posts), 3)
        self.assertEqual(self.store.moment_cursor(ACCOUNT), "张三今天招工")

    async def test_等超时_可能已经点了几下_占住的额度不还(self) -> None:
        posts = await self.watcher(_Runtime(timed_out=True)).tick()
        self.assertEqual(posts, [])
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(days=1), action=ACTION_MOMENTS_LIKE)
        self.assertGreater(used, 0, "超时不等于没点：宁多记不少记")
        self.assertEqual(self.store.moment_cursor(ACCOUNT), "", "没结论不前移游标")


if __name__ == "__main__":
    unittest.main()
