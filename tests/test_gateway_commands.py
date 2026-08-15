"""AI 端下发微信能力指令（066 Phase 1）——白名单 / 守门 / 幂等 / 回执。

全部在 Linux 跑，不碰真微信：runtime 用假实现，只断言「投了什么任务、拒了什么、拒的理由」。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from structlog.testing import capture_logs

from wxclient.gateway import CommandExecutor, GatewayStore, OutboundGuard
from wxclient.gateway.capabilities import (
    CAP_GROUP_MESSAGE,
    CAP_MOMENTS,
    CAP_PRIVATE_MESSAGE,
    CAP_READ,
    CAP_SEND_FILES,
    capability_of,
    capability_status,
    probe_capabilities,
)
from wxclient.gateway.download import DownloadError, FileFetcher, FileRef
from wxclient.gateway.schedule import MAX_LATE_HOURS, ScheduleLoop, parse_schedule_at
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
            "args": {"to": "张三", "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
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
            "args": {"to": "张三", "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
        })
        self.assertEqual(result.status, "failed")
        self.assertIn("找不到", result.reason)
        self.assertEqual(runtime.calls, [], "没取到文件就绝不能去发")

    async def test_没有下载器时明确拒绝而不是把描述符当路径发(self) -> None:
        runtime = FakeRuntime()
        ex = self._executor(runtime)   # 不带 fetcher
        result = await ex.execute({
            "command_id": "c3", "op": "send_files",
            "args": {"to": "张三", "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
        })
        self.assertEqual(result.status, "rejected")
        self.assertEqual(runtime.calls, [])


class InlineFileTestCase(unittest.IsolatedAsyncioTestCase):
    """字节随指令下发：落盘 → 交本机路径 → 用完删。"""

    def _ref(self, name: str, data: bytes) -> FileRef:
        return FileRef.parse({"file_id": "f1", "file_name": name, "content_b64": base64.b64encode(data).decode()})

    async def test_落盘后交出本机路径且内容一致(self) -> None:
        ref = self._ref("招聘海报-1.jpg", b"\xff\xd8\xff\xe0")
        async with FileFetcher().materialize("acc", [ref]) as paths:
            self.assertEqual(paths[0].name, "招聘海报-1.jpg", "原名要带到微信上，不能退化成 .bin")
            self.assertEqual(paths[0].read_bytes(), b"\xff\xd8\xff\xe0")
            tmp_root = paths[0].parent.parent
        self.assertFalse(tmp_root.exists(), "发完必须连临时目录一起删")

    async def test_同名文件不互相覆盖(self) -> None:
        refs = [self._ref("海报.jpg", b"a"), self._ref("海报.jpg", b"bb")]
        async with FileFetcher().materialize("acc", refs) as paths:
            self.assertEqual([p.read_bytes() for p in paths], [b"a", b"bb"])

    async def test_超总量上限就拒绝而不是硬写盘(self) -> None:
        refs = [self._ref("大.bin", b"x" * 1024)]
        with self.assertRaises(DownloadError):
            async with FileFetcher(max_total_bytes=512).materialize("acc", refs):
                pass

    async def test_空文件与坏_base64_明确报错(self) -> None:
        # 空内容在 parse 阶段就不算文件；坏 base64 要在落盘前拦下。
        self.assertIsNone(FileRef.parse({"file_id": "f1", "file_name": "空.jpg", "content_b64": ""}))
        for ref in (FileRef("f1", "空.jpg", "  "), FileRef("f1", "坏.jpg", "!!!not-base64!!!")):
            with self.assertRaises(DownloadError):
                async with FileFetcher().materialize("acc", [ref]):
                    pass

    def test_文件名不许带路径(self) -> None:
        ref = FileRef.parse({"file_id": "f1", "file_name": "../../evil.png", "content_b64": "YQ=="})
        self.assertNotIn("/", ref.file_name)
        self.assertNotIn("..", ref.file_name.strip("._"))

    def test_没有字节的描述符不算文件(self) -> None:
        self.assertIsNone(FileRef.parse({"file_id": "f1", "url": "/v1/sessions/s/files/f1"}))


# ────────────────────────── 定时指令（077）──────────────────────────


class ScheduleParsingTests(unittest.TestCase):
    def test_parses_iso_with_zone(self) -> None:
        due = parse_schedule_at("2026-08-15T09:00:00+08:00")
        self.assertIsNotNone(due)
        self.assertEqual(due.utcoffset(), timedelta(hours=8))

    def test_naive_time_is_read_as_local_not_utc(self) -> None:
        """★ 用户说「明天上午九点」指的是他那台电脑上的九点。

        当成 UTC 的话在东八区差八小时——`within_active_hours` 踩过一模一样的坑。
        """
        due = parse_schedule_at("2026-08-15T09:00:00")
        self.assertIsNotNone(due)
        self.assertIsNotNone(due.tzinfo)

    def test_garbage_is_not_a_schedule(self) -> None:
        for raw in ("", "   ", "明天九点", None, 123, "2026-13-45"):
            self.assertIsNone(parse_schedule_at(raw))


class ScheduleLoopTests(unittest.IsolatedAsyncioTestCase):
    """★ 077：`schedule_at` 原来只是透传到网关就没人管了，「明天上午九点提醒他」
    会被**当场**发出去——比不做还糟。需求文档 §6.19 的定时触发就卡在这里。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.now = WORKDAY
        self.fired: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _loop(self, *, active: bool = True) -> ScheduleLoop:
        async def run_command(_account: str, payload: dict) -> None:
            self.fired.append(payload)

        return ScheduleLoop(
            accounts=[ACCOUNT],
            store=self.store,
            run_command=run_command,
            is_active=lambda _now: active,
            clock=lambda: self.now,
        )

    def _schedule(self, due: datetime, command_id: str = "cmd-1") -> None:
        self.store.put_scheduled_command(
            ACCOUNT, command_id,
            {"command_id": command_id, "op": "send_message",
             "args": {"to": "王建国", "text": "提醒您明天面试"}},
            due, at=self.now,
        )

    async def test_not_due_yet_stays_put(self) -> None:
        self._schedule(WORKDAY + timedelta(hours=2))
        self.assertEqual(await self._loop().tick(), 0)
        self.assertEqual(self.fired, [])
        self.assertEqual(self.store.count_scheduled_commands(), 1)

    async def test_due_fires_once_and_is_cleared(self) -> None:
        self._schedule(WORKDAY - timedelta(minutes=5))
        self.assertEqual(await self._loop().tick(), 1)
        self.assertEqual(len(self.fired), 1)
        self.assertEqual(self.fired[0]["args"]["to"], "王建国")
        # 销账了：下一趟不会再放一遍。
        self.assertEqual(self.store.count_scheduled_commands(), 0)
        self.assertEqual(await self._loop().tick(), 0)

    async def test_nothing_fires_outside_working_hours(self) -> None:
        """★ 半夜三点准时发一条「提醒您明天面试」正是风控要找的那种账号。

        不是丢掉——在表里等着，第二天进时间窗第一趟放出去。
        """
        self._schedule(WORKDAY - timedelta(minutes=5))
        self.assertEqual(await self._loop(active=False).tick(), 0)
        self.assertEqual(self.fired, [])
        self.assertEqual(self.store.count_scheduled_commands(), 1)

        self.assertEqual(await self._loop(active=True).tick(), 1)
        self.assertEqual(len(self.fired), 1)

    async def test_way_too_late_is_dropped_with_a_trace(self) -> None:
        """机器关了一夜再开机，昨天的排期不该突然全放出来——面试都结束了。"""
        self._schedule(WORKDAY - timedelta(hours=MAX_LATE_HOURS + 1))
        with capture_logs() as logs:
            self.assertEqual(await self._loop().tick(), 0)
        self.assertEqual(self.fired, [])
        self.assertEqual(self.store.count_scheduled_commands(), 0)
        self.assertTrue(
            any(e["log_level"] == "warning" and "迟到" in e["event"] for e in logs), logs
        )



# ────────────────────────── 逐能力探测（074）──────────────────────────


class CapabilityProbeTests(unittest.TestCase):
    """★ 074：原来只有一个总开关「UIA 可用/不可用」，所有「做不了」长一个样，
    而且要**执行到一半**才发现（需求文档 §12.2 要逐能力上报 + 执行前校验）。
    """

    LIVE = {"logged_in": True, "uia_exposed": True}

    def _caps(self, **kw):
        base = {"uia": self.LIVE, "allow_real_actions": True, "has_file_channel": True}
        base.update(kw)
        return probe_capabilities(**base)

    def test_everything_works_when_wechat_is_live(self) -> None:
        caps = self._caps()
        self.assertTrue(all(c.available for c in caps.values()), caps)

    def test_login_screen_blocks_everything_with_a_human_reason(self) -> None:
        caps = self._caps(uia={"logged_in": False, "login_window": True})
        self.assertFalse(caps[CAP_PRIVATE_MESSAGE].available)
        self.assertIn("扫码登录", caps[CAP_PRIVATE_MESSAGE].reason)

    def test_no_probe_result_is_reported_as_unavailable_not_available(self) -> None:
        """★ 探不出来 = 不知道 = 按做不了报。猜出来的能力表比没有更糟——
        它会让 AI 端理直气壮地承诺做不到的事。"""
        caps = self._caps(uia=None)
        self.assertFalse(caps[CAP_PRIVATE_MESSAGE].available)

    def test_dry_run_mode_says_writes_do_not_work(self) -> None:
        """★ 只走流程时每条写指令都回 done，AI 端会照着告诉用户「已经发过去了」。"""
        caps = self._caps(allow_real_actions=False)
        self.assertFalse(caps[CAP_PRIVATE_MESSAGE].available)
        self.assertFalse(caps[CAP_MOMENTS].available)
        self.assertTrue(caps[CAP_READ].available, "读类不受影响")

    def test_missing_file_channel_only_blocks_files(self) -> None:
        caps = self._caps(has_file_channel=False)
        self.assertFalse(caps[CAP_SEND_FILES].available)
        self.assertIn("文件", caps[CAP_SEND_FILES].reason)
        self.assertTrue(caps[CAP_PRIVATE_MESSAGE].available)

    def test_unsupported_op_is_reported_as_such(self) -> None:
        caps = self._caps(supported_ops={"send_message"})
        self.assertTrue(caps[CAP_PRIVATE_MESSAGE].available)
        self.assertFalse(caps[CAP_MOMENTS].available)

    def test_capability_status_never_talks_up_a_dry_run_machine(self) -> None:
        """契约 §8：只有 ``passed`` 会让企业端显示「运行中」。

        ★ 只走流程不真发的机器必须报 ``limited``——报 passed 的话，企业端说运行中，
        而这台电脑一条消息都不会真的发出去。
        """
        self.assertEqual(capability_status(self._caps()), "passed")
        self.assertEqual(capability_status(self._caps(allow_real_actions=False)), "limited")
        self.assertEqual(capability_status(self._caps(has_file_channel=False)), "limited")
        self.assertEqual(capability_status(self._caps(uia=None)), "failed")

    def test_group_message_is_its_own_capability(self) -> None:
        self.assertEqual(capability_of("send_message"), CAP_PRIVATE_MESSAGE)
        self.assertEqual(capability_of("send_message", to_group=True), CAP_GROUP_MESSAGE)


class CapabilityGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, caps, runtime) -> CommandExecutor:
        return CommandExecutor(
            runtime=runtime,
            store=self.store,
            guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
            account_id=ACCOUNT,
            allow_real_actions=True,
            confirmation="确认",
            clock=lambda: WORKDAY,
            capabilities=lambda: caps,
        )

    async def test_unavailable_capability_is_refused_before_touching_wechat(self) -> None:
        """★ 做不了就当场说清楚，别一路走到取字节那步才拒。"""
        runtime = FakeRuntime()
        caps = probe_capabilities(
            uia={"logged_in": True, "uia_exposed": True},
            allow_real_actions=True,
            has_file_channel=False,          # 没配取文件通道
        )
        result = await self._executor(caps, runtime).execute({
            "command_id": "c1", "op": "send_files",
            "args": {"to": "王建国", "files": [{"file_id": "f1", "content_b64": "eA=="}]},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("文件", result.reason)
        self.assertEqual(runtime.calls, [], "一条微信动作都不该发生")

    async def test_available_capability_still_goes_through(self) -> None:
        runtime = FakeRuntime()
        caps = probe_capabilities(
            uia={"logged_in": True, "uia_exposed": True},
            allow_real_actions=True,
            has_file_channel=True,
        )
        result = await self._executor(caps, runtime).execute({
            "command_id": "c2", "op": "send_message",
            "args": {"to": "王建国", "text": "您好"},
        })
        self.assertEqual(result.status, "done")
        self.assertEqual(len(runtime.calls), 1)
