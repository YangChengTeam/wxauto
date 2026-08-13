"""AI 端下发微信能力指令（066 Phase 1）——白名单 / 守门 / 幂等 / 回执。

全部在 Linux 跑，不碰真微信：runtime 用假实现，只断言「投了什么任务、拒了什么、拒的理由」。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wxclient.gateway import CommandExecutor, GatewayStore, OutboundGuard
from wxclient.gateway.download import (
    DownloadError,
    FileFetcher,
    _name_from_headers,
    _reject_html,
    http_base_from_ws,
)
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

    async def test_read_op_runs_for_real_not_as_a_stub(self) -> None:
        """读类也要显式关 dry_run。

        底座的 dry_run 默认是 True，`check_new_friends` / `read_recent_moments` 在
        dry-run 下压根不碰微信、直接回一个空壳（`wechat_api/uia.py`）——AI 端拿到的
        会是「已完成、什么都没有」的假回执，比报错还难查。
        """
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "check_new_friends", "args": {},
        })
        self.assertEqual(result.status, "done")
        task_type, payload, _confirmation = runtime.calls[-1]
        self.assertIs(task_type, TaskType.CHECK_NEW_FRIENDS)
        self.assertFalse(payload["dry_run"])

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


class SendFilesDownloadTestCase(CommandTestCase):
    """067：send_files 收的是**文件描述符**，不是路径。

    路径由网关自己下载后产生——AI 端给不了、也不许给（那是服务端派生的东西）。
    """

    def _fetcher(self, *, boom: str = "") -> Any:
        outer = self

        class FakeFetcher:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []
                self.cleaned = False

            @asynccontextmanager
            async def materialize(self, account_id, refs):
                self.calls.append([r.file_id for r in refs])
                if boom:
                    raise DownloadError(boom)
                paths = []
                for ref in refs:
                    p = Path(outer._tmp.name) / f"{ref.file_id}.png"
                    p.write_bytes(b"png")
                    paths.append(p)
                try:
                    yield paths
                finally:
                    self.cleaned = True

        return FakeFetcher()

    async def test_file_ids_下载后交本机路径给底座(self) -> None:
        runtime, fetcher = FakeRuntime(), self._fetcher()
        ex = self._executor(runtime, fetcher=fetcher)
        result = await ex.execute({
            "command_id": "c1", "op": "send_files",
            "args": {"to": "张三", "files": [{"file_id": "f1", "url": "/v1/sessions/s1/files/f1"}]},
        })
        self.assertEqual(result.status, "done", result.reason)
        task_type, payload, _conf = runtime.calls[0]
        self.assertEqual(task_type, TaskType.SEND_FILES)
        self.assertEqual(fetcher.calls, [["f1"]])
        self.assertTrue(payload["files"][0].endswith("f1.png"), payload["files"])
        self.assertTrue(fetcher.cleaned, "发完必须清掉临时文件")

    async def test_下载失败回执说人话且不投任务(self) -> None:
        runtime = FakeRuntime()
        ex = self._executor(runtime, fetcher=self._fetcher(boom="这个文件在 AI 服务上找不到了"))
        result = await ex.execute({
            "command_id": "c2", "op": "send_files",
            "args": {"to": "张三", "files": [{"file_id": "f1", "url": "/v1/sessions/s1/files/f1"}]},
        })
        self.assertEqual(result.status, "failed")
        self.assertIn("找不到", result.reason)
        self.assertEqual(runtime.calls, [], "没取到文件就绝不能去发")

    async def test_没有下载器时明确拒绝而不是把描述符当路径发(self) -> None:
        runtime = FakeRuntime()
        ex = self._executor(runtime)   # 不带 fetcher
        result = await ex.execute({
            "command_id": "c3", "op": "send_files",
            "args": {"to": "张三", "files": [{"file_id": "f1", "url": "/v1/sessions/s1/files/f1"}]},
        })
        self.assertEqual(result.status, "rejected")
        self.assertEqual(runtime.calls, [])


class DownloadUrlTestCase(unittest.TestCase):
    """下载器的地址判定——这条线是防 SSRF 的，必须有回归护栏。"""

    def _fetcher(self) -> Any:
        return FileFetcher(ws_url="wss://ai.example.com/v1/ws", ticket_provider=None)

    def test_ws_地址推导出_http_base(self) -> None:
        self.assertEqual(http_base_from_ws("wss://ai.example.com/v1/ws"), "https://ai.example.com")
        self.assertEqual(http_base_from_ws("ws://127.0.0.1:8137/v1/ws"), "http://127.0.0.1:8137")

    def test_反代前缀必须留着(self) -> None:
        """线上就是这个形状；丢了前缀会打到前端 SPA 的兜底 location，拿回一张 index.html。"""
        self.assertEqual(
            http_base_from_ws("wss://worker.laoxiaozhi.cn/agent-ws/v1/ws"),
            "https://worker.laoxiaozhi.cn/agent-ws",
        )
        fetcher = FileFetcher(ws_url="wss://worker.laoxiaozhi.cn/agent-ws/v1/ws", ticket_provider=None)
        self.assertEqual(
            fetcher._absolute("/v1/sessions/s1/files/f1"),
            "https://worker.laoxiaozhi.cn/agent-ws/v1/sessions/s1/files/f1",
        )

    def test_取回网页当场停不当成文件(self) -> None:
        """200 + text/html = 地址打偏了。这一条比 404 更危险：它会被当成下载成功。"""
        with self.assertRaises(DownloadError):
            _reject_html({"content-type": "text/html; charset=utf-8"})
        _reject_html({"content-type": "image/jpeg"})   # 正常图片不该被拦

    def test_带前缀时外部地址仍然拒(self) -> None:
        fetcher = FileFetcher(ws_url="wss://worker.laoxiaozhi.cn/agent-ws/v1/ws", ticket_provider=None)
        for url in (
            "https://evil.example.com/agent-ws/v1/sessions/s1/files/f1",
            "https://worker.laoxiaozhi.cn/v1/sessions/s1/files/f1",   # 少了前缀 = 打到 SPA
        ):
            with self.assertRaises(DownloadError):
                fetcher._absolute(url)

    def test_相对路径按自家_base_拼(self) -> None:
        got = self._fetcher()._absolute("/v1/sessions/s1/files/f1")
        self.assertEqual(got, "https://ai.example.com/v1/sessions/s1/files/f1")

    def test_外部_host_一律拒(self) -> None:
        for url in ("https://evil.example.com/v1/sessions/s1/files/f1", "http://169.254.169.254/latest/meta-data"):
            with self.assertRaises(DownloadError):
                self._fetcher()._absolute(url)

    def test_同_host_但不是取会话文件的路径也拒(self) -> None:
        for url in ("/metrics", "/v1/sessions/s1/files/f1/../../../etc/passwd", "https://ai.example.com/dev/raw"):
            with self.assertRaises(DownloadError):
                self._fetcher()._absolute(url)

    def test_响应里的文件名不许带路径(self) -> None:
        name = _name_from_headers({"content-disposition": 'attachment; filename="../../evil.png"'}, "x.bin")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name.strip("._"))
