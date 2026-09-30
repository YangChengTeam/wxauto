"""桌面端里不含 GUI 的那部分：日志缓冲、服务状态机、参数读写、装配。

窗口和托盘本身只能在 Windows 真机看，所以刻意把逻辑都挤在这些能测的模块里。
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wxclient.gateway.factory import normalize_accounts
from wxclient.ui import logline, settings_store
from wxclient.ui.logbuffer import LogBuffer, LogPipeline, attach_to_root
from wxclient.ui.logfeed import LogFeed, is_log_on_screen
from wxclient.ui.service import GatewayService, ServiceState
from wxclient.ui.single_instance import SingleInstance


class LogBufferTests(unittest.TestCase):
    def test_keeps_only_the_last_lines(self) -> None:
        buffer = LogBuffer(capacity=3)

        buffer.extend(["a", "b", "c", "d"])

        self.assertEqual(buffer.snapshot(), ["b", "c", "d"])

    def test_subscriber_sees_new_lines_and_can_unsubscribe(self) -> None:
        buffer = LogBuffer()
        seen: list[str] = []
        unsubscribe = buffer.subscribe(seen.append)

        buffer.append("first")
        unsubscribe()
        buffer.append("second")

        self.assertEqual(seen, ["first"])

    def test_broken_subscriber_does_not_break_logging(self) -> None:
        buffer = LogBuffer()
        buffer.subscribe(lambda _line: (_ for _ in ()).throw(RuntimeError("窗口没了")))
        seen: list[str] = []
        buffer.subscribe(seen.append)

        buffer.append("still here")  # 不抛

        self.assertEqual(seen, ["still here"])
        self.assertEqual(buffer.snapshot(), ["still here"])

    def test_handler_feeds_the_buffer(self) -> None:
        buffer = LogBuffer()
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.INFO)  # 生产里由 configure_logging 设好
        handler = attach_to_root(buffer)
        try:
            logging.getLogger("test.ui").info("网关启动")
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        self.assertTrue(any("网关启动" in line for line in buffer.snapshot()))


class DataDirTests(unittest.TestCase):
    """设置/日志/状态库放哪。打包成 exe 之后 cwd 不可信，见 _default_data_dir 的注释。"""

    def test_source_run_keeps_the_relative_dot_wxclient(self) -> None:
        from wxclient.config.settings import _default_data_dir

        self.assertEqual(_default_data_dir(), Path(".wxclient"))

    def test_frozen_build_anchors_to_a_fixed_per_user_dir(self) -> None:
        import sys as _sys

        from wxclient.config.settings import _default_data_dir

        with mock.patch.object(_sys, "frozen", True, create=True), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": os.path.join("X:", "AppData")}):
            resolved = _default_data_dir()
        self.assertTrue(resolved.is_absolute() or str(resolved).startswith("X:"))
        self.assertNotEqual(resolved, Path(".wxclient"), "冻结态不能再用相对路径")


class LogVisibilityTests(unittest.TestCase):
    """判断「日志在不在人眼前」。2026-08-13 的「实时日志没有了」就死在这个判据上。"""

    def test_maximized_window_counts_as_on_screen(self) -> None:
        # 回归护栏：判据曾写成 state == "normal"，用户一最大化就被判成看不见
        self.assertTrue(is_log_on_screen("log", "zoomed"))

    def test_normal_window_on_log_page_is_on_screen(self) -> None:
        self.assertTrue(is_log_on_screen("log", "normal"))

    def test_tray_and_minimized_are_off_screen(self) -> None:
        self.assertFalse(is_log_on_screen("log", "withdrawn"))
        self.assertFalse(is_log_on_screen("log", "iconic"))

    def test_settings_page_is_off_screen_even_when_window_shows(self) -> None:
        self.assertFalse(is_log_on_screen("settings", "normal"))
        self.assertFalse(is_log_on_screen("settings", "zoomed"))

    def test_unknown_states_default_to_on_screen(self) -> None:
        # 宁可多画也别再漏画：认不出的状态一律当「露着」
        self.assertTrue(is_log_on_screen("log", "somethingnew"))


class LogFilterTests(unittest.TestCase):
    """日志级别过滤档位。不碰 GUI，所以能在 Linux 上跑。"""

    def test_filters_are_nested_from_loose_to_strict(self) -> None:
        from wxclient.ui.logline import LEVEL_FILTERS

        names = [name for name, _ in LEVEL_FILTERS]
        self.assertEqual(names[0], "全部")
        sets = [allowed for _, allowed in LEVEL_FILTERS]
        for looser, stricter in itertools.pairwise(sets):
            self.assertTrue(stricter < looser, "越靠后的档位必须更严格")
        self.assertIn("error", sets[-1], "最严格的档位也不能把错误滤掉")

    def test_every_severity_is_covered_by_the_loosest_filter(self) -> None:
        from wxclient.ui.logline import LEVEL_FILTERS, LogLine

        everything = LEVEL_FILTERS[0][1]
        for level in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL", ""):
            severity = LogLine(time="", level=level, message="x").severity
            self.assertIn(severity, everything, f"{level} 会被「全部」挡掉")


class LogFeedTests(unittest.TestCase):
    """日志节拍器：攒一批画一批 + 没人看时不画。GUI 无关，所以能在 Linux 上跑。"""

    def test_batches_instead_of_one_call_per_line(self) -> None:
        feed = LogFeed()
        feed.resume()
        for i in range(200):
            feed.push(f"line {i}")
        # 200 行只换来一次 drain：界面就只重排一次，而不是 200 次
        drained = feed.drain()
        self.assertEqual(len(drained), 200)
        self.assertEqual(drained[0], "line 0")
        self.assertEqual(feed.drain(), [], "取过就该空了，别把同一批画两遍")

    def test_paused_by_default_so_tray_start_draws_nothing(self) -> None:
        # 常态是开机缩在托盘里（start_hidden），这时一行都不该画
        feed = LogFeed()
        self.assertTrue(feed.paused)
        feed.push("在托盘里来的日志")
        self.assertEqual(feed.drain(), [])

    def test_resume_reports_missed_lines_so_window_replays(self) -> None:
        feed = LogFeed()
        feed.push("暂停期间来的")
        self.assertTrue(feed.resume(), "暂停期间有日志 → 打开窗口时要整屏回放")
        feed = LogFeed()
        self.assertFalse(feed.resume(), "什么都没来就别白重画一屏")

    def test_pause_drops_pending_because_buffer_still_has_them(self) -> None:
        feed = LogFeed()
        feed.resume()
        feed.push("一行")
        feed.pause()
        self.assertEqual(feed.drain(), [])

    def test_pausing_with_pending_lines_still_asks_for_a_replay(self) -> None:
        # 窗口切走的那一瞬间队列里正好有日志：丢可以，但不能忘了记「漏画过」，
        # 否则下次打开窗口不整屏回放，这几行就永远上不了屏
        feed = LogFeed()
        feed.resume()
        feed.push("切走前一刹那来的")
        feed.pause()
        self.assertTrue(feed.resume(), "丢掉待画的行之后必须要求整屏回放")

    def test_pending_is_capped_so_a_stuck_ui_cannot_grow_without_bound(self) -> None:
        feed = LogFeed(pending_cap=10)
        feed.resume()
        for i in range(25):
            feed.push(f"line {i}")
        drained = feed.drain()
        self.assertEqual(len(drained), 10)
        self.assertEqual(drained[-1], "line 24", "丢的是最旧的，最新的必须留着")
        self.assertEqual(feed.dropped, 15)

    def test_push_is_safe_from_many_threads(self) -> None:
        feed = LogFeed(pending_cap=10_000)
        feed.resume()

        def spam() -> None:
            for i in range(500):
                feed.push(str(i))

        threads = [threading.Thread(target=spam) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(feed.drain()), 2000)


class LogPipelineTests(unittest.TestCase):
    """pyweixin import 时会 fileConfig 掉整套 root handler，界面得能自己接回来。"""

    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.previous = list(self.root.handlers), self.root.level
        self.root.setLevel(logging.INFO)

    def tearDown(self) -> None:
        handlers, level = self.previous
        self.root.handlers = handlers
        self.root.setLevel(level)

    def test_reattaches_after_a_third_party_resets_logging(self) -> None:
        buffer = LogBuffer()
        pipeline = LogPipeline(buffer, lambda: self.root.setLevel(logging.INFO))
        pipeline.install()

        self.assertFalse(pipeline.ensure())  # 没被动过就不重装

        self.root.handlers = []  # 模拟 pyweixin 的 logging.config.fileConfig
        self.assertTrue(pipeline.ensure())

        logging.getLogger("test.ui").info("重新接回后还能进缓冲")
        self.assertTrue(any("还能进缓冲" in line for line in buffer.snapshot()))


class _FakeRunner:
    def __init__(self, started: threading.Event) -> None:
        self._started = started
        self.stopped = False

    async def run(self) -> None:
        import asyncio

        self._started.set()
        await asyncio.Event().wait()  # 一直跑，直到事件循环被停掉

    async def stop(self) -> None:
        self.stopped = True


class _FakeRuntime:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeGateway:
    def __init__(self, started: threading.Event) -> None:
        self.runtime = _FakeRuntime()
        self.runner = _FakeRunner(started)
        self.settings = type("S", (), {"ai_ws_url": "wss://example/v1/ws"})()
        self.accounts = ("wxid_x",)
        self.allow_real_actions = False


class GatewayServiceTests(unittest.TestCase):
    def test_start_runs_gateway_and_stop_cleans_up(self) -> None:
        started = threading.Event()
        gateway = _FakeGateway(started)
        service = GatewayService(lambda: gateway)

        service.start()
        self.assertTrue(service.wait_ready(timeout=5))
        self.assertTrue(started.wait(timeout=5))
        self.assertIs(service.state, ServiceState.RUNNING)
        self.assertTrue(gateway.runtime.started)

        service.stop(timeout=5)

        self.assertIs(service.state, ServiceState.STOPPED)
        self.assertIsNone(service.error)  # 主动停服务不算故障
        self.assertTrue(gateway.runtime.stopped)
        self.assertTrue(gateway.runner.stopped)

    def test_build_failure_surfaces_as_failed_state_not_a_crash(self) -> None:
        service = GatewayService(lambda: (_ for _ in ()).throw(ValueError("AI_WS_URL 必填")))

        service.start()
        self.assertTrue(service.wait_ready(timeout=5))

        self.assertIs(service.state, ServiceState.FAILED)
        self.assertIn("AI_WS_URL", service.error or "")
        self.assertIn("AI_WS_URL", service.label)

    def test_second_start_is_ignored_while_running(self) -> None:
        started = threading.Event()
        builds = []

        def builder() -> _FakeGateway:
            builds.append(1)
            return _FakeGateway(started)

        service = GatewayService(builder)
        service.start()
        self.assertTrue(service.wait_ready(timeout=5))
        service.start()  # 重复点「启动服务」不该起第二条网关
        time.sleep(0.1)
        service.stop(timeout=5)

        self.assertEqual(len(builds), 1)

    def test_stop_async_returns_immediately(self) -> None:
        """界面上点「停止服务」不能被 join 钉住——那 15 秒窗口一次都不重绘。"""
        started = threading.Event()
        gateway = _FakeGateway(started)
        service = GatewayService(lambda: gateway)
        service.start()
        self.assertTrue(service.wait_ready(timeout=5))

        done = threading.Event()
        began = time.monotonic()
        thread = service.stop_async(on_done=done.set, timeout=5)
        self.assertLess(time.monotonic() - began, 0.5, "stop_async 不该在调用方线程上等")

        self.assertTrue(done.wait(timeout=10))
        thread.join(timeout=5)
        self.assertIs(service.state, ServiceState.STOPPED)

    def test_restart_async_stops_then_applies_params_then_starts(self) -> None:
        """参数必须在「停干净之后、起来之前」灌进去，否则新参数会被旧实例读走。"""
        started = threading.Event()
        order: list[str] = []

        def builder() -> _FakeGateway:
            order.append("build")
            started.clear()
            return _FakeGateway(started)

        service = GatewayService(builder)
        service.start()
        self.assertTrue(service.wait_ready(timeout=5))

        done = threading.Event()
        thread = service.restart_async(
            before_start=lambda: order.append("apply-params"),
            on_done=done.set,
            timeout=5,
        )
        self.assertIsNotNone(thread)
        self.assertTrue(done.wait(timeout=15))
        # on_done 只保证「start() 已调用」——起来是另一条线程的事，等 ready 才算数
        self.assertTrue(service.wait_ready(timeout=5))
        self.assertIs(service.state, ServiceState.RUNNING)
        self.assertEqual(order, ["build", "apply-params", "build"])

        service.stop(timeout=5)

    def test_second_restart_while_restarting_is_ignored(self) -> None:
        """连点两下「保存并重启」不该起两条网关。"""
        started = threading.Event()
        service = GatewayService(lambda: _FakeGateway(started))
        service.start()
        self.assertTrue(service.wait_ready(timeout=5))

        release = threading.Event()
        done = threading.Event()
        first = service.restart_async(
            before_start=lambda: release.wait(timeout=5), on_done=done.set, timeout=5
        )
        self.assertIsNotNone(first)
        self.assertIsNone(service.restart_async(timeout=5), "重启期间再点一次应被忽略")
        release.set()
        self.assertTrue(done.wait(timeout=15))
        self.assertFalse(service.restarting)

        service.stop(timeout=5)

    def test_stop_before_start_is_harmless(self) -> None:
        service = GatewayService(lambda: _FakeGateway(threading.Event()))

        service.stop(timeout=1)

        self.assertIs(service.state, ServiceState.STOPPED)


class StateCallbackIsolationTests(unittest.TestCase):
    """★ 2026-08-13 真机：「保存并重启」→ 停机成功、再也没启动、界面全程卡死。

    病根：状态回调原来是**同步**跑的，而 ``start()``/``stop()`` 是在 ``with self._lock:``
    里调 ``_set_state`` 的（RLock 外层还持着）→ 回调跑在持锁状态下。回调里 ``tray.refresh()``
    要从非托盘线程打 Win32 托盘接口，对面没就绪就干等，于是：
      · 重启线程卡在回调里，永远走不到 ``start()``（日志停在「网关已停止」）；
      · 主线程每秒读一次状态，被这把锁挡住 → 窗口不重绘 = 卡死。
    这里钉死：**回调再怎么卡，都不许拖住谁。**
    """

    def test_a_hung_callback_does_not_block_start_stop_or_state_reads(self) -> None:
        started = threading.Event()
        gateway = _FakeGateway(started)
        service = GatewayService(lambda: gateway)

        wedged = threading.Event()  # 永不 set：模拟卡死在托盘更新里的回调
        entered = threading.Event()

        def hung_listener(_state: object, _error: object) -> None:
            entered.set()
            wedged.wait()

        service.set_state_listener(hung_listener)

        service.start()
        self.assertTrue(entered.wait(timeout=5), "回调应当被调用（只是它卡住了）")
        # 回调卡着，但服务照常起来、状态照常读得到（读状态是主线程每秒都要干的事）
        self.assertTrue(service.wait_ready(timeout=5))
        self.assertTrue(started.wait(timeout=5))
        self.assertIs(service.state, ServiceState.RUNNING)
        self.assertIsNone(service.error)
        self.assertIsNotNone(service.uptime_seconds())

        service.stop(timeout=5)
        self.assertIs(service.state, ServiceState.STOPPED)
        wedged.set()

    def test_status_reads_never_block_the_caller_even_if_the_store_is_wedged(self) -> None:
        """★ 界面每秒读一次状态，这几个读法必须**秒回**，真去问网关的活在后台线程上。

        红线的理由不是某一次实测有多慢，而是「主线程的耗时上界由网关说了算」这件事
        本身不成立：`pending_backlog()` 一路走到 SQLite（实测写库时中位 15ms / 最慢
        46ms），而只要网关那头有任何一处会等（等锁、等 I/O、等微信），主线程就跟着停。
        这里用一个「卡住不返回」的读法把边界钉死。
        """
        started = threading.Event()
        gateway = _FakeGateway(started)
        entered = threading.Event()
        release = threading.Event()

        def wedged_backlog() -> int:
            entered.set()
            release.wait(timeout=10)  # 模拟「等 SQLite 写锁」
            return 7

        gateway.runner.pending_backlog = wedged_backlog
        service = GatewayService(lambda: gateway)
        try:
            service.start()
            self.assertTrue(service.wait_ready(timeout=5))
            self.assertTrue(entered.wait(timeout=5), "后台轮询应当已经卡在查库那句里")

            begin = time.monotonic()
            for _ in range(200):
                service.status()
                service.pending_backlog()
                service.connection_summary()
                service.watched_peers()
                service.connect_error()
                service.uptime_seconds()
            elapsed = time.monotonic() - begin
            self.assertLess(elapsed, 0.5, f"读状态被拖住了 {elapsed:.2f}s——主线程会跟着卡")
        finally:
            release.set()
            service.stop(timeout=5)

    def test_status_snapshot_carries_what_the_sidebar_shows(self) -> None:
        """快照要真的把网关那几个值带回来，不能只是「不卡但永远是 0」。"""
        started = threading.Event()
        gateway = _FakeGateway(started)
        gateway.runner.connected_count = lambda: 1
        gateway.runner.pending_backlog = lambda: 3
        gateway.runner.watched_peers = lambda: ["张三", "李四"]
        service = GatewayService(lambda: gateway)
        try:
            service.start()
            self.assertTrue(service.wait_ready(timeout=5))

            status = service.refresh_status_now()

            self.assertEqual((status.connected, status.accounts), (1, 1))
            self.assertEqual(status.backlog, 3)
            self.assertEqual(service.watched_peers(), ["张三", "李四"])
            self.assertEqual(service.connection_summary(), (1, 1))
        finally:
            service.stop(timeout=5)

    def test_one_broken_status_reader_does_not_blank_the_whole_row(self) -> None:
        """某一项读崩了只该它显示成缺省值，别把整排状态清空。"""
        started = threading.Event()
        gateway = _FakeGateway(started)
        gateway.runner.connected_count = lambda: 1
        gateway.runner.watched_peers = lambda: (_ for _ in ()).throw(RuntimeError("名单没了"))
        service = GatewayService(lambda: gateway)
        try:
            service.start()
            self.assertTrue(service.wait_ready(timeout=5))

            status = service.refresh_status_now()

            self.assertEqual(status.connected, 1)   # 好的那项照常
            self.assertEqual(status.watched, ())    # 坏的那项退回缺省
        finally:
            service.stop(timeout=5)

    def test_restart_reaches_start_even_if_the_callback_is_wedged(self) -> None:
        """真机现象的正面复现：停了之后**必须**还能起来。"""
        started = threading.Event()
        gateway = _FakeGateway(started)
        builds: list[int] = []

        def build() -> _FakeGateway:
            builds.append(1)
            started.clear()
            return gateway

        service = GatewayService(build)
        service.set_state_listener(lambda *_: threading.Event().wait())  # 每次通知都卡死

        service.start()
        self.assertTrue(started.wait(timeout=5))

        started.clear()  # on_done 只保证 start() 调过了，装配跑在网关线程上——盯 started 才作数
        done = threading.Event()
        thread = service.restart_async(on_done=done.set, timeout=5)
        self.assertIsNotNone(thread)
        self.assertTrue(done.wait(timeout=20), "重启必须跑完——卡住的回调不该把它钉在半路")
        self.assertTrue(started.wait(timeout=10), "★ 停完之后必须真的又起来了")
        self.assertEqual(len(builds), 2)
        self.assertIs(service.state, ServiceState.RUNNING)

        service.stop(timeout=5)

    def test_a_throwing_callback_does_not_kill_later_notifications(self) -> None:
        started = threading.Event()
        gateway = _FakeGateway(started)
        service = GatewayService(lambda: gateway)

        seen: list[ServiceState] = []
        boom = [True]

        def listener(state: ServiceState, _error: object) -> None:
            if boom[0]:
                boom[0] = False
                raise RuntimeError("界面炸了")
            seen.append(state)

        service.set_state_listener(listener)
        service.start()
        self.assertTrue(started.wait(timeout=5))
        service.stop(timeout=5)

        deadline = time.monotonic() + 5
        while ServiceState.STOPPED not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn(ServiceState.STOPPED, seen, "第一次抛异常之后，后面的通知还得照发")


class HeavyImportSerializationTests(unittest.TestCase):
    """★ 2026-08-14 真机死锁：界面起来了、网关**永远起不来**（没有「网关启动」那行）。

    py-spy 连抓两次、相隔两分钟栈一模一样：
        wx-gateway  → create_module ← 正在 LoadLibrary SQLAlchemy 的 Cython 扩展
        wx-ui-probe → create_module ← 正在 LoadLibrary pywinauto 的 C 扩展
    两个后台线程同时首次 import 重型 C 扩展 = 同时 LoadLibrary，撞上 Windows
    进程级加载器锁。这里只钉「两处惰性 import 都排在同一把闸后面」——
    真正的死锁在 Linux 上复现不出来（没有那把加载器锁）。
    """

    def test_both_lazy_import_sites_take_the_same_lock(self) -> None:
        # 读源码而不是 import：这台跑测试的 Linux 上没有 tkinter，import 不进来。
        root = Path(__file__).resolve().parent.parent / "wxclient"
        # 每一处「在后台线程上首次 import 重型 C 扩展」的地方都要在这张表里。
        # 074 的能力探测是新增的一处：它在保活线程上 import pywinauto，
        # 跟网关装配线程撞上就是 2026-08-14 那个死锁。
        targets = (
            ("ui/__main__.py", "build_gateway"),
            ("ui/window.py", "probe_wechat_uia"),
            ("gateway/factory.py", "probe_wechat_uia"),
        )
        for relpath, needle in targets:
            source = (root / relpath).read_text(encoding="utf-8")
            head = source[: source.index(f"import {needle}")]
            self.assertIn(
                "with HEAVY_IMPORT_LOCK:",
                head[-400:],
                f"{relpath} 里那处惰性 import 必须先拿重型 import 闸",
            )
            self.assertIn(
                "from wxclient.heavy_import import HEAVY_IMPORT_LOCK",
                source,
                f"{relpath} 必须用同一把闸（heavy_import.HEAVY_IMPORT_LOCK），不许各造一把",
            )


    # ★ 2026-09-17 第二次发作：启动动画停在「正在准备界面…」不动，一定概率。上面那把闸围住的
    # 只是一句纯 Python import，真正 LoadLibrary 的 psutil / win32api / pywinauto 在
    # `probe_wechat_uia()` 里是函数内惰性 import，从来没排过队；这回撞上的是主线程
    # （MainWindow 一建完就起探测线程，主线程还在 import pystray / 登录页）。
    # 下面四条钉的是两条修法：首次加载集中进 `import_wechat_driver()`、探测线程等 mainloop。

    def test_driver_import_happens_inside_the_lock_and_only_once(self) -> None:
        from wxclient import heavy_import

        seen: list[tuple[str, bool]] = []

        def fake_import(name: str) -> None:
            seen.append((name, heavy_import.HEAVY_IMPORT_LOCK.locked()))

        before = heavy_import._driver_imported
        with mock.patch.object(heavy_import.platform, "system", return_value="Windows"), \
                mock.patch.object(heavy_import.importlib, "import_module", side_effect=fake_import):
            heavy_import._driver_imported = False
            try:
                heavy_import.import_wechat_driver()
                heavy_import.import_wechat_driver()  # 第二次只查布尔，不再碰加载器
            finally:
                heavy_import._driver_imported = before

        names = [name for name, _ in seen]
        self.assertEqual(len(names), len(set(names)), f"同一个模块被 import 了两遍：{names}")
        self.assertTrue({"pywinauto", "comtypes", "win32api", "psutil"} <= set(names),
                        f"微信驱动链上会 LoadLibrary 的模块都得在这一批里：{names}")
        self.assertTrue(all(locked for _, locked in seen),
                        "每一次首次 import 都必须发生在 HEAVY_IMPORT_LOCK 里")

    def test_driver_import_is_a_noop_off_windows(self) -> None:
        from wxclient import heavy_import

        before = heavy_import._driver_imported
        with mock.patch.object(heavy_import.platform, "system", return_value="Linux"), \
                mock.patch.object(heavy_import.importlib, "import_module") as import_module:
            heavy_import._driver_imported = False
            try:
                heavy_import.import_wechat_driver()
            finally:
                heavy_import._driver_imported = before
        import_module.assert_not_called()

    def test_every_lazy_pywinauto_user_warms_up_through_the_shared_import(self) -> None:
        """探测入口和驱动构造都要先喊 `import_wechat_driver()`，而且要在惰性 import 之前。"""
        root = Path(__file__).resolve().parent.parent / "wxclient"
        probe = (root / "uia_probe/probe.py").read_text(encoding="utf-8")
        self.assertIn("from wxclient.heavy_import import import_wechat_driver", probe)
        self.assertLess(probe.index("import_wechat_driver()"),
                        probe.index("find_wechat_windows(max_depth"),
                        "probe_wechat_uia 必须先排队加载 C 扩展，再去枚举窗口")
        driver = (root / "wechat_api/uia.py").read_text(encoding="utf-8")
        self.assertIn("from wxclient.heavy_import import import_wechat_driver", driver)
        init = driver[driver.index("class UiaWeChatDriver"):driver.index("    def add_friend(")]
        self.assertIn("import_wechat_driver()", init,
                      "UiaWeChatDriver.__init__ 必须先排队加载 C 扩展，别让第一个动作去撞加载器锁")

    def test_first_wechat_probe_waits_for_the_mainloop(self) -> None:
        """探测线程不能在 MainWindow.__init__ 里直接起：主线程那会儿还在 import pystray / 登录页。"""
        root = Path(__file__).resolve().parent.parent / "wxclient"
        source = (root / "ui/window.py").read_text(encoding="utf-8")
        self.assertIn("self._later(0, self._schedule_wechat_probe)", source,
                      "首次探测必须经 after(0) 排到 mainloop 之后")
        self.assertNotIn("self._schedule_wechat_probe()", source,
                         "不许在 tk 线程上同步起探测线程（那正是「正在准备界面…」卡住的死锁）")


class AppIconTests(unittest.TestCase):
    """exe / 快捷方式 / 任务栏 / 托盘用同一份品牌图标（用户 2026-08-13 要求）。"""

    def test_brand_assets_are_shipped(self) -> None:
        from wxclient.ui import icons

        self.assertTrue(icons.ICO_PATH.exists(), "app.ico 没打进包，exe 和快捷方式就没图标")
        self.assertTrue(icons.PNG_PATH.exists(), "托盘要的是 PIL 图像，PNG 不能少")
        self.assertEqual(icons.ico_path(), str(icons.ICO_PATH))

    def test_ico_carries_the_sizes_windows_asks_for(self) -> None:
        from PIL import Image

        from wxclient.ui import icons

        with Image.open(icons.ICO_PATH) as image:
            sizes = {size[0] for size in image.ico.sizes()}
        # 16 = 托盘/标题栏，32/48 = 桌面快捷方式，256 = 大图标视图
        self.assertTrue({16, 32, 48, 256}.issubset(sizes), f"ico 尺寸不全：{sorted(sizes)}")

    def test_tray_icon_keeps_the_brand_and_shows_state(self) -> None:
        """托盘不能退化成一个纯色圆点：品牌图形要在，状态色也要在。"""
        from wxclient.ui.icons import build_icon_image
        from wxclient.ui.service import ServiceState

        running = build_icon_image(ServiceState.RUNNING, 64)
        stopped = build_icon_image(ServiceState.STOPPED, 64)
        self.assertEqual(running.size, (64, 64))
        # 右下角角标：跑着是绿的，停了是灰的
        self.assertNotEqual(running.getpixel((56, 56)), stopped.getpixel((56, 56)))
        # 左上角是品牌图形本身，不该被角标动到
        self.assertEqual(running.getpixel((8, 8)), stopped.getpixel((8, 8)))


class SplashTests(unittest.TestCase):
    """启动动画。窗口本身只能在真机看，这里钉的是配色/几何和「两段接力」的结构。

    ⚠️ 不能 import `wxclient.ui.splash`——它顶上就 `import tkinter`，而跑测试这台
    Linux 上没有 tkinter。纯逻辑都放在 `splash_style.py`（这也是把它分出来的原因）。
    """

    def test_comet_fades_from_the_background_to_the_brand_colour(self) -> None:
        from wxclient.ui import theme
        from wxclient.ui.splash_style import blend, comet_colors

        colors = comet_colors(12)
        self.assertEqual(len(colors), 12)
        self.assertEqual(colors[-1], theme.BRAND, "彗头必须是品牌色")
        self.assertEqual(colors[0], theme.SIDEBAR_ACTIVE_BG, "彗尾必须化进底色")
        self.assertEqual(blend("#000000", "#ffffff", 0.5), "#808080")
        # 越界的比例不许算出非法颜色（动画里 ratio 是算出来的，不是写死的）
        self.assertEqual(blend("#000000", "#ffffff", 2), "#ffffff")
        self.assertEqual(blend("#000000", "#ffffff", -1), "#000000")

    def test_logo_fits_inside_the_ring(self) -> None:
        """图标比环的内径还大就把环压没了（第一版就是这样，看不见转的那圈）。"""
        from wxclient.ui.splash_style import LOGO_PX, RING_RADIUS, RING_WIDTH

        self.assertLess(LOGO_PX, 2 * (RING_RADIUS - RING_WIDTH))

    def test_both_frames_draw_the_logo_at_the_same_size(self) -> None:
        """★ 静态首屏和动图的图标必须一样大（用户 2026-08-17：交接时图标会变一下大小）。

        真因是两边缩图的能力不一样：PIL 想缩多大缩多大，tk 只能**整数倍**
        （`PhotoImage.subsample`——启动那一刻不能为了一张图去 import PIL）。
        LOGO_PX=56 时 tk 缩出来是 256//56=4 倍 → 64，静态图却是精确的 56。
        所以 LOGO_PX 必须整除源图边长。
        """
        from wxclient.ui.splash_style import LOGO_PX, LOGO_SOURCE_PX

        self.assertEqual(LOGO_SOURCE_PX % LOGO_PX, 0,
                         "★ 除不尽 → tk 缩出来不是 LOGO_PX，交接时图标会变大小")
        # tk 那边实际会缩成多大：和 splash.py `_place_icon` 里同一条算法
        factor = max(1, LOGO_SOURCE_PX // LOGO_PX)
        self.assertEqual(LOGO_SOURCE_PX // factor, LOGO_PX)

    def test_the_source_icon_is_the_size_we_think_it_is(self) -> None:
        """★ 上面那条整除约束是照 LOGO_SOURCE_PX 算的——它得和真实资产对得上，
        否则约束守的是一个想象中的数字。"""
        from PIL import Image

        from wxclient.ui.splash_style import LOGO_SOURCE_PX

        png = Path(__file__).resolve().parent.parent / "wxclient/ui/assets/app-256.png"
        with Image.open(png) as image:
            self.assertEqual(image.size, (LOGO_SOURCE_PX, LOGO_SOURCE_PX))

    def test_frozen_first_frame_is_shipped_and_matches_the_animated_one(self) -> None:
        """冻结包那张静态首屏要在，且和会动的那个同尺寸——否则接力时会「跳一下」。"""
        from PIL import Image

        from wxclient.ui.splash_style import HEIGHT, WIDTH

        png = Path(__file__).resolve().parent.parent / "wxclient/ui/assets/splash.png"
        self.assertTrue(png.exists(), "splash.png 没生成：跑 packaging/make_splash.py")
        with Image.open(png) as image:
            self.assertEqual(image.size, (WIDTH, HEIGHT))

    def test_the_static_frame_is_literally_frame_zero(self) -> None:
        """★ 静态首屏画的必须**就是动画的第一帧**（用户 2026-08-17）。

        原来静态图只画一段 90° 的弧、动图画整圈渐变，交接那一下人眼一定看得出来——
        用户说的「第一帧是个实心的 loading，后面还要动一下才开始转」就是这个。
        这里沿环取样：每一段的中点像素，必须最接近**那一段自己的颜色**。
        """
        import math

        from PIL import Image

        from wxclient.ui.splash_style import (
            RING_RADIUS,
            WIDTH,
            comet_arcs,
            comet_colors,
        )

        def rgb(value: str) -> tuple[int, int, int]:
            return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))

        palette = [rgb(color) for color in comet_colors()]
        png = Path(__file__).resolve().parent.parent / "wxclient/ui/assets/splash.png"
        cx, cy = WIDTH // 2, 84
        with Image.open(png) as image:
            pixels = image.convert("RGB").load()
            for start, extent, color in comet_arcs(0):
                mid = math.radians(start + extent / 2)
                # Tk/PIL 的角度都是 3 点钟起算，y 轴向下，所以取样点是 (cos, -sin)
                x = round(cx + RING_RADIUS * math.cos(mid))
                y = round(cy - RING_RADIUS * math.sin(mid))
                got = pixels[x, y]
                closest = min(palette, key=lambda c: sum((a - b) ** 2
                                                         for a, b in zip(c, got)))
                self.assertEqual(closest, rgb(color),
                                 f"环上 {start}° 那一段的颜色对不上：取到 {got}")

    def test_both_frames_sit_in_the_same_place(self) -> None:
        """★ 两段接力必须落在同一个位置，否则整块会挪一下（用户 2026-08-17）。

        PyInstaller 那张静态首屏的位置写死在它的 Tcl 模板里：
        ``int(0.5*(display - image))``，也就是屏幕正中。会动的这个照抄同一个算法。
        """
        from wxclient.ui.splash_style import HEIGHT, WIDTH, window_position

        for screen in ((1920, 1080), (1366, 768), (2560, 1440)):
            expected = (int(0.5 * (screen[0] - WIDTH)), int(0.5 * (screen[1] - HEIGHT)))
            self.assertEqual(window_position(*screen), expected, f"{screen} 上没对齐")

    def test_the_animated_frame_is_as_opaque_as_the_static_one(self) -> None:
        """★ 静态首屏没有透明度可调。会动的这个只要不是 1.0，接力那一下就会亮一下。"""
        from wxclient.ui.splash_style import OPACITY

        self.assertEqual(OPACITY, 1.0)

    def test_the_stage_line_is_baked_into_the_image(self) -> None:
        """★ 不用 PyInstaller 的文字叠加：它只能左对齐、对不准中线，交接时那行字会横着挪。

        我们从不更新它，画死在图里更准。
        """
        spec = (Path(__file__).resolve().parent.parent
                / "packaging/lxz-wechat-gateway.spec").read_text(encoding="utf-8")
        self.assertNotIn("text_pos", spec)
        self.assertNotIn("text_default", spec)
        maker = (Path(__file__).resolve().parent.parent
                 / "packaging/make_splash.py").read_text(encoding="utf-8")
        self.assertIn("正在启动…", maker, "那行字得由 make_splash 画进图里")

    def test_splash_survives_until_the_gateway_is_assembled(self) -> None:
        """★ 动画得撑到网关装配完——那几秒正是启动里最长的一段。

        第一版是「主窗口一建好就关」，于是动画只闪一下，真正在等的那几秒又回到空白。
        这里读源码钉住：收尾走 `reveal()`，由服务状态（不再是 STARTING）或兜底超时触发。
        """
        source = (
            Path(__file__).resolve().parent.parent / "wxclient/ui/__main__.py"
        ).read_text(encoding="utf-8")
        self.assertIn("SPLASH_MAX_MS", source, "必须有兜底超时，否则装配卡住就一直挡着")
        self.assertIn("service.state is not ServiceState.STARTING", source)
        self.assertIn("root.after(SPLASH_MAX_MS, reveal)", source)

    def test_splash_paints_before_the_mainloop_starts(self) -> None:
        """★ 建完控件必须自己催一次重绘：那会儿 mainloop 还没跑，Tk 一个像素都不画。

        真机实测过：少了这句，屏幕上只有 PyInstaller 那张静态图，会动的那个根本不出现。

        ★ 顺序也在这条里：`_reveal()`（露面 + 催重绘）必须排在关掉静态首屏**之前**，
        否则中间那一瞬间屏幕上一张都没有——那就是用户 2026-08-17 说的「闪一下」。
        真 Tk 下的行为验证在 `test_ui_window.SplashRevealTests`；这一条是给没有 Tk 的
        环境留的守卫（Linux/CI 上整个 splash 模块都 import 不了）。
        """
        source = (
            Path(__file__).resolve().parent.parent / "wxclient/ui/splash.py"
        ).read_text(encoding="utf-8")
        head = source[: source.index("_close_frozen_splash()\n")]
        self.assertIn("self._reveal()", head, "露面要排在关静态首屏之前")
        reveal = source[source.index("def _reveal"):]
        self.assertIn("self._pump()", reveal, "露面之后必须自己催一次重绘")
        self.assertIn("self._win.deiconify()", reveal)

    def test_animated_splash_closes_the_frozen_one(self) -> None:
        """会动的那个一接手就必须关掉静态首屏，否则两张图叠在一起。"""
        source = (
            Path(__file__).resolve().parent.parent / "wxclient/ui/splash.py"
        ).read_text(encoding="utf-8")
        self.assertIn("pyi_splash", source)
        self.assertIn("_close_frozen_splash()", source)

    def test_the_fade_starts_from_whatever_opacity_the_splash_uses(self) -> None:
        """淡出必须从 OPACITY 往下走，不能写死从 1.0 起——半透明时会先变亮再消失。

        ★ 2026-08-17 起 OPACITY 就是 1.0（要和静态首屏对得上，见
        `test_the_animated_frame_is_as_opaque_as_the_static_one`），这条仍然留着：
        哪天再调回半透明，淡出不该跟着坏。
        """
        source = (
            Path(__file__).resolve().parent.parent / "wxclient/ui/splash.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"-alpha", OPACITY * left / FADE_STEPS', source)
        # 不透明时不该在创建时就设 -alpha：设了窗口就变成 layered，首次合成走另一条路。
        # 半透明时又必须设，否则第一帧先实心一下再变透——所以是带条件的那一句。
        self.assertIn("if OPACITY < 1.0:", source)


class ColdStartTests(unittest.TestCase):
    """双击到「看见启动动画」之间不许横着一堵 import 墙。"""

    def test_importing_the_ui_package_does_not_drag_in_the_heavy_stack(self) -> None:
        """★ `python -m wxclient.ui` 先跑包 __init__；它一 import 重家伙，动画就得等。

        真机实测：`import wxclient.ui` 冷启动 4.7 秒（pydantic_settings + structlog
        + rich + asyncio），那几秒屏幕上一片空白 = 用户眼里的「点了没反应」。
        所以包 __init__ 改成 PEP 562 懒加载，这条用例把它钉住。
        """
        probe = (
            "import sys, wxclient.ui;"
            "print(int(any(m.split('.')[0] in {'pydantic', 'pydantic_settings', 'structlog', 'rich'}"
            " for m in sys.modules)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, check=True,
        )

        self.assertEqual(result.stdout.strip(), "0", "包 __init__ 又把重家伙拉进来了")

    def test_the_public_api_still_works_after_going_lazy(self) -> None:
        """懒加载不能把公开 API 弄丢——别人还在 `from wxclient.ui import GatewayService`。"""
        import wxclient.ui as package

        for name in package.__all__:
            self.assertTrue(hasattr(package, name), f"{name} 取不到了")
        with self.assertRaises(AttributeError):
            package.没有这个东西  # noqa: B018


class PackagingTests(unittest.TestCase):
    """打包产物的硬约束。跑不了 pyinstaller 也要能守住的那几条，全在 spec 文本里。"""

    SPEC = Path(__file__).resolve().parent.parent / "packaging/lxz-wechat-gateway.spec"

    def test_no_console_window(self) -> None:
        """★ 桌面程序绝不能弹控制台黑框：EXE 必须 console=False。

        真机上黑框还有第二个来源，spec 管不着——**别把 .bat / python.exe 挂到快捷方式上**
        （.bat 必然拉起 cmd.exe 窗口，python.exe 是控制台版）。要么直接指打包出来的 exe，
        要么用 pythonw.exe 并由一个隐藏窗口的 .vbs 拉起。
        """
        spec = self.SPEC.read_text(encoding="utf-8")
        self.assertIn("console=False", spec)
        self.assertNotIn("console=True", spec)

    def test_every_asset_gets_bundled(self) -> None:
        """★ assets 要整目录打进去，别逐个文件列。

        逐个列的下场：新加一张图忘了登记 → 打出来的包少一张，而代码对缺图全是优雅降级
        （背景变纯色、图标退回圆点），既不报错也不崩，只有人肉眼看出来「怎么和开发机不一样」。
        """
        spec = self.SPEC.read_text(encoding="utf-8")
        assets = Path(__file__).resolve().parent.parent / "wxclient/ui/assets"

        self.assertIn('("../wxclient/ui/assets", "wxclient/ui/assets")', spec,
                      "整个目录一起打")
        for asset in assets.iterdir():
            if asset.is_file():
                # 「单独列一行」长这样：("../wxclient/ui/assets/app.ico", "wxclient/ui/assets")
                self.assertNotIn(f'assets/{asset.name}", "wxclient/ui/assets"', spec,
                                 f"{asset.name} 不该再单独列进 datas，目录已经覆盖了")

    def test_the_logger_config_file_gets_bundled(self) -> None:
        """★ pywinauto 那条链上的 `logger` 包要连它的 logging.conf 一起打。

        2026-08-16 装完安装包实测：微信开着三个进程，界面却一直说找不到微信。真因是
        `logger/__init__.py` 一被 import 就去读同目录的 `logging.conf`，而 PyInstaller
        只收 .py 不收 .conf——冻结包里 `from pywinauto import Desktop` 直接抛
        FileNotFoundError，**整个 UIA 探测在打包版里一次都没成功过**，源码跑却一切正常。

        只有「装完在真机上跑」才暴露得出来，所以把它钉在这儿。
        """
        spec = self.SPEC.read_text(encoding="utf-8")
        self.assertIn("collect_data_files", spec)
        self.assertIn('DATA_ONLY_PACKAGES = ("logger",)', spec)

    def test_win32ui_is_not_excluded(self) -> None:
        """★ 别再把 `win32ui` 砍掉——**pywinauto 自己要用它**。

        2026-08-16 装完真机跑才发现：excludes 里砍它的依据是「本仓 grep 不到有人 import」，
        可那只看得见我们的 import，看不见依赖自己的。冻结包里的表现是
        `from pywinauto import Desktop` → ModuleNotFoundError，UIA 探测一次都成功不了。
        """
        spec = self.SPEC.read_text(encoding="utf-8")
        excludes = spec.split("excludes = [", 1)[1].split("]", 1)[0]
        for name in ('"win32ui"', '"Pythonwin"', '"win32uiole"'):
            self.assertNotIn(name, excludes, f"{name} 砍不得：pywinauto 要用")

    def test_pyweixin_gets_bundled(self) -> None:
        """★ vendored 的 `pyweixin` 必须进依赖图——**它才是真正动微信的那个包**。

        2026-08-17 真机：v0.1.12 装完，一点「发消息」就是
        `pyweixin is unavailable: No module named 'pyweixin'`。它住在
        `third_party/pywechat/`，运行时靠 `sys.path.insert` 现找，而冻结包里没有那个目录。
        源码跑、单测跑、连打包后的 `--self-check` 探测都绿——因为探测只用 pywinauto 看窗口，
        一次都碰不到 pyweixin。**「看得见微信」和「动得了微信」是两件事。**
        """
        spec = self.SPEC.read_text(encoding="utf-8")
        vendor = Path(__file__).resolve().parent.parent / "third_party/pywechat/pyweixin"

        self.assertIn("VENDOR_PYWEIXIN", spec)
        self.assertIn('pathex=[".", VENDOR_PYWEIXIN]', spec, "vendor 目录要进 pathex")
        for module in sorted(vendor.glob("*.py")):
            name = "pyweixin" if module.stem == "__init__" else f"pyweixin.{module.stem}"
            self.assertIn(f'"{name}"', spec, f"{name} 没列进 hiddenimports，打不进包")

    def test_self_check_really_imports_pyweixin(self) -> None:
        """★ 自检要真 import 一次 pyweixin，import 不到就必须判失败。

        缺了这条，上一条（spec 里列全了）只是纸面约束：excludes 误砍、依赖装漏一样会让
        冻结包里 import 不到，而那正是只有装完真机才炸的那类。
        """
        from wxclient.ui.__main__ import _check_wechat_driver

        lines: list[str] = []
        with mock.patch("wxclient.wechat_api.uia.UiaWeChatDriver._pyweixin_class",
                        side_effect=ImportError("No module named 'pyweixin'")):
            self.assertFalse(_check_wechat_driver(lines))
        self.assertTrue(any("pyweixin=FAIL" in line for line in lines), lines)

        lines.clear()
        with mock.patch("wxclient.wechat_api.uia.UiaWeChatDriver._pyweixin_class"), \
             mock.patch("wxclient.wechat_api.uia.UiaWeChatDriver._pyweixin_submodule"):
            self.assertTrue(_check_wechat_driver(lines))
        self.assertIn("pyweixin=ok", lines)

    def test_self_check_separates_no_wechat_from_missing_module(self) -> None:
        """★ 「本机没装微信」不能和「包里缺 pyweixin」判成同一件事。

        `pyweixin/Config.py` 在**模块顶层**读注册表找微信版本，没装就抛 `NotInstalledError`
        ——于是任何没装微信的机器（CI 的 Windows runner 就是）`import pyweixin` 必炸。
        以前两件事都报 FAIL，提示语还写着「多半是 spec 的 excludes 砍掉了依赖」，
        指向一个不存在的问题。

        放行的判据必须是**正向且严格**的：`NotInstalledError` 这个类得真的来自 pyweixin。
        只对名字不对来路的话，随便哪个库抛个同名异常都能把这道闸糊弄过去。
        """
        from wxclient.ui.__main__ import _check_wechat_driver

        class NotInstalledError(Exception):
            pass

        # ① 真的来自 pyweixin：证明模块被找到并执行过了 → 放行
        NotInstalledError.__module__ = "pyweixin.Errors"
        lines: list[str] = []
        with mock.patch("wxclient.wechat_api.uia.UiaWeChatDriver._pyweixin_class",
                        side_effect=NotInstalledError("未找到微信注册表路径")):
            self.assertTrue(_check_wechat_driver(lines))
        self.assertTrue(any(line.startswith("pyweixin=ok") for line in lines), lines)
        self.assertFalse(any("FAIL" in line for line in lines), lines)

        # ② 同名但不是 pyweixin 抛的 → 仍然判失败，别让别的库蒙混过关
        NotInstalledError.__module__ = "some_other_lib"
        lines.clear()
        with mock.patch("wxclient.wechat_api.uia.UiaWeChatDriver._pyweixin_class",
                        side_effect=NotInstalledError("未找到微信注册表路径")):
            self.assertFalse(_check_wechat_driver(lines))
        self.assertTrue(any("pyweixin=FAIL" in line for line in lines), lines)

    def test_upgrading_wipes_the_previous_internal_dir(self) -> None:
        """★ 升级安装必须先删掉上一版的 `_internal`，否则装出来的是**两版的并集**。

        Inno 只做「覆盖 + 新增」，不删新版里已经没有的文件。PyInstaller 的 onedir 每版收集
        到的东西都不一样，于是原地升级会留下一堆上一版的残留：2026-08-19 实测 0.1.20 →
        0.1.21，装完 1746 个文件而包里只有 1067 个，680 个是旧的。

        最难看的一个：pywin32 新版把目录名 `Pythonwin` 改成了 `pythonwin`，而 Windows 文件名不分
        大小写——两版的 `win32ui.pyd` 落进同一个目录。`win32ui` 正是上面那条测试里
        「微信明明开着，界面说找不到微信」的主角，拿版本不对的 .pyd 顶它症状一模一样，
        而且**只在升级过的机器上出现**——打包机和全新安装都是好的，自检也照样绿。
        """
        iss = (Path(__file__).resolve().parent.parent
               / "packaging/installer.iss").read_text(encoding="utf-8")
        # 认**行首**那个小节头，别认正文里提到的同名字样（顶上的说明就提了一次）。
        # assertIn 会把整份 iss 打进失败信息，看着累——这里只要一句话说清缺了什么。
        head = "\n[InstallDelete]\n"
        self.assertTrue(head in iss,
                        "installer.iss 里没有 [InstallDelete] 小节，升级装出来就是两版的并集")
        section = iss.split(head, 1)[1].split("\n[", 1)[0]
        self.assertIn(r'Name: "{app}\_internal"', section)
        self.assertIn("filesandordirs", section)

    def test_the_build_verifies_the_package_it_just_made(self) -> None:
        """★ 打完包必须真跑一次 `--self-check`。

        缺模块、缺数据文件这一类，源码跑和单测都抓不到——只有装到别人机器上真去驱动
        微信那一刻才炸。一天之内撞了两次（logger/logging.conf、win32ui）之后加的这道闸。
        """
        build = (Path(__file__).resolve().parent.parent
                 / "packaging/build.py").read_text(encoding="utf-8")
        self.assertIn("--self-check", build)
        self.assertIn("def self_check(exe: Path)", build)

        from wxclient.ui.__main__ import build_parser

        args = build_parser().parse_args(["--self-check", "report.txt"])
        self.assertEqual(args.self_check, "report.txt")


class SettingsDefaultsTests(unittest.TestCase):
    """★ 配置中心不许出现空格子。

    2026-08-16 装完安装包第一次打开就是这个：干净机器上没有 gateway.env，于是「连接配置 /
    调度配置 / 启动设置」全空着，第一次登录直接「网关启动失败：AI_WS_URL 必填」。而这些
    参数**运行时本来就有默认值**，只是界面不知道——人看到的是「什么都没配」，实际是
    「配好了只是没显示」。
    """

    def test_every_field_has_a_default(self) -> None:
        for field in settings_store.FIELDS:
            self.assertNotEqual(field.default, "", f"{field.key} 没有默认值，界面上会是空的")

    def test_defaults_match_the_settings_that_actually_run(self) -> None:
        """★ 界面上写的默认值必须和真正生效的那份一致。

        两处各写一份，对不上的后果特别阴：界面显示 60 秒，实际按 300 秒跑，
        而两边都「没配置错」。这里去比对 `GatewaySettings` / `UiSettings` 本尊。
        """
        from wxclient.gateway.config import GatewaySettings
        from wxclient.ui.config import UiSettings

        gateway = GatewaySettings()
        ui = UiSettings()
        expected = {
            "WXCLIENT_GATEWAY_BUSINESS_API_URL": gateway.business_api_url,
            "WXCLIENT_GATEWAY_AI_WS_URL": gateway.ai_ws_url,
            "WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS": int(gateway.poll_interval_seconds),
            "WXCLIENT_GATEWAY_DEMO_MODE": gateway.demo_mode,
            "WXCLIENT_GATEWAY_DEMO_POLL_INTERVAL_SECONDS": int(gateway.demo_poll_interval_seconds),
            "WXCLIENT_GATEWAY_FRIEND_CHECK_INTERVAL_SECONDS":
                int(gateway.friend_check_interval_seconds),
            "WXCLIENT_GATEWAY_FRIEND_RECHECK_INTERVAL_SECONDS":
                int(gateway.friend_recheck_interval_seconds),
            "WXCLIENT_GATEWAY_FRIEND_RECHECK_DEADLINE_HOURS":
                int(gateway.friend_recheck_deadline_hours),
            "WXCLIENT_GATEWAY_ACTIVE_HOURS": gateway.active_hours,
            "WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR": gateway.max_sends_per_hour,
            "WXCLIENT_GATEWAY_MAX_MESSAGES_PER_HOUR": gateway.max_messages_per_hour,
            "WXCLIENT_GATEWAY_MAX_FRIEND_ACTIONS_PER_HOUR": gateway.max_friend_actions_per_hour,
            "WXCLIENT_GATEWAY_FRIEND_INTERVAL_SECONDS": int(gateway.friend_interval_seconds),
            "WXCLIENT_GATEWAY_MAX_MOMENTS_PER_DAY": gateway.max_moments_per_day,
            "WXCLIENT_GATEWAY_MAX_MOMENT_LIKES_PER_DAY": gateway.max_moment_likes_per_day,
            "WXCLIENT_GATEWAY_MOMENT_LIKE_INTERVAL_SECONDS":
                int(gateway.moment_like_interval_seconds),
            "WXCLIENT_GATEWAY_MAX_MOMENT_COMMENTS_PER_DAY": gateway.max_moment_comments_per_day,
            "WXCLIENT_GATEWAY_MOMENT_COMMENT_INTERVAL_SECONDS":
                int(gateway.moment_comment_interval_seconds),
            "WXCLIENT_GATEWAY_MOMENTS_POLL_INTERVAL_SECONDS":
                int(gateway.moments_poll_interval_seconds),
            "WXCLIENT_GATEWAY_MOMENTS_AUTO_LIKE": gateway.moments_auto_like,
            "WXCLIENT_GATEWAY_MOMENTS_AUTO_COMMENT": gateway.moments_auto_comment,
            "WXCLIENT_GATEWAY_MOMENTS_COMMENT_MATERIAL": gateway.moments_comment_material,
            "WXCLIENT_UI_AUTOSTART": ui.autostart,
            "WXCLIENT_UI_START_HIDDEN": ui.start_hidden,
            "WXCLIENT_UI_AUTO_UPDATE": ui.auto_update,
        }
        for field in settings_store.FIELDS:
            want = expected[field.key]
            if field.kind == "bool":
                self.assertEqual(settings_store.as_bool(field.default), want, field.key)
            else:
                self.assertEqual(field.default, str(want), field.key)

    def test_a_clean_machine_shows_every_value(self) -> None:
        """全新装的机器（没有 gateway.env、没有环境变量）也要每一项都有值。"""
        with tempfile.TemporaryDirectory() as tmp:
            values = settings_store.effective(Path(tmp), {})

        for field in settings_store.FIELDS:
            self.assertTrue(values.get(field.key), f"{field.key} 在干净机器上是空的")

    def test_the_gateway_starts_on_a_clean_machine(self) -> None:
        """★ 装完就能用：干净机器上 `validate_for_start` 不该拦下来。"""
        from wxclient.gateway.config import GatewaySettings

        GatewaySettings().validate_for_start()


class SettingsReachTheGuardTests(unittest.TestCase):
    """★ 配置中心里改的数，必须真的落到限流闸上。

    2026-08-30 用户：「朋友圈在过去 24 小时只能发 3 个呀，这个配置也要能在配置中心配」。
    在界面上加一格很容易，**但加完不接线就是个装饰**——人以为改了，实际还按 3 条跑，
    而两边都不会报错。这里从环境变量一路验到配额表，把这条线钉住。

    朋友圈这条的周期是**天**不是小时（一小时 3 条和一天 3 条完全是两回事），
    顺带一起验，免得哪天被顺手改成 HOUR。
    """

    def test_界面上改的朋友圈上限_真的会变成配额(self) -> None:
        from wxclient.gateway.config import GatewaySettings
        from wxclient.gateway.guard import ACTION_MOMENTS, DAY, action_limits_from

        with mock.patch.dict(os.environ, {"WXCLIENT_GATEWAY_MAX_MOMENTS_PER_DAY": "9"}):
            limits = action_limits_from(GatewaySettings())

        self.assertEqual(limits[ACTION_MOMENTS].max_count, 9, "配置中心改了却没生效＝白改")
        self.assertEqual(limits[ACTION_MOMENTS].window_seconds, DAY, "★ 朋友圈按天算，不是按小时")

    def test_不配的时候还是默认那三条(self) -> None:
        from wxclient.gateway.config import GatewaySettings
        from wxclient.gateway.guard import ACTION_MOMENTS, action_limits_from

        limits = action_limits_from(GatewaySettings())

        self.assertEqual(limits[ACTION_MOMENTS].max_count, 3)

    def test_界面上改的点赞上限和间隔_真的会落到闸上(self) -> None:
        """朋友圈点赞（用户 2026-09-07，一天 20）：上限进配额表、间隔进节拍表，两边都按天/秒对上。"""
        from wxclient.gateway.config import GatewaySettings
        from wxclient.gateway.guard import (
            ACTION_MOMENTS_LIKE,
            DAY,
            MOMENTS_LIKE_WRITE_ACTION,
            action_limits_from,
            write_intervals_from,
        )

        with mock.patch.dict(os.environ, {
            "WXCLIENT_GATEWAY_MAX_MOMENT_LIKES_PER_DAY": "7",
            "WXCLIENT_GATEWAY_MOMENT_LIKE_INTERVAL_SECONDS": "1200",
        }):
            settings = GatewaySettings()
            limits = action_limits_from(settings)
            intervals = write_intervals_from(settings)

        self.assertEqual(limits[ACTION_MOMENTS_LIKE].max_count, 7)
        self.assertEqual(limits[ACTION_MOMENTS_LIKE].window_seconds, DAY, "★ 点赞按天算")
        self.assertEqual(intervals[MOMENTS_LIKE_WRITE_ACTION], 1200.0)

        defaults = action_limits_from(GatewaySettings())
        self.assertEqual(defaults[ACTION_MOMENTS_LIKE].max_count, 20, "用户定的一天 20")

    def test_界面上的朋友圈巡看四格_真的会落到配置上(self) -> None:
        """本机朋友圈巡看（用户 2026-09-08）：间隔、点赞、评论、素材四格都要能从配置中心进 GatewaySettings。"""
        from wxclient.gateway.config import GatewaySettings

        with mock.patch.dict(os.environ, {
            "WXCLIENT_GATEWAY_MOMENTS_POLL_INTERVAL_SECONDS": "900",
            "WXCLIENT_GATEWAY_MOMENTS_AUTO_LIKE": "1",
            "WXCLIENT_GATEWAY_MOMENTS_AUTO_COMMENT": "1",
            "WXCLIENT_GATEWAY_MOMENTS_COMMENT_MATERIAL": "本周新增夜班岗",
        }):
            settings = GatewaySettings()
        self.assertEqual(settings.moments_poll_interval_seconds, 900.0)
        self.assertTrue(settings.moments_auto_like)
        self.assertTrue(settings.moments_auto_comment)
        self.assertEqual(settings.moments_comment_material, "本周新增夜班岗")
        self.assertEqual(GatewaySettings().moments_poll_interval_seconds, 0.0, "默认关")

    def test_界面上改的评论上限和间隔_真的会落到闸上(self) -> None:
        """朋友圈评论（用户 2026-09-07：「做成可配置」）：上限进配额表、间隔进节拍表。"""
        from wxclient.gateway.config import GatewaySettings
        from wxclient.gateway.guard import (
            ACTION_MOMENTS_COMMENT,
            DAY,
            MOMENTS_COMMENT_WRITE_ACTION,
            action_limits_from,
            write_intervals_from,
        )

        with mock.patch.dict(os.environ, {
            "WXCLIENT_GATEWAY_MAX_MOMENT_COMMENTS_PER_DAY": "3",
            "WXCLIENT_GATEWAY_MOMENT_COMMENT_INTERVAL_SECONDS": "900",
        }):
            settings = GatewaySettings()
            limits = action_limits_from(settings)
            intervals = write_intervals_from(settings)

        self.assertEqual(limits[ACTION_MOMENTS_COMMENT].max_count, 3)
        self.assertEqual(limits[ACTION_MOMENTS_COMMENT].window_seconds, DAY, "★ 评论按天算")
        self.assertEqual(intervals[MOMENTS_COMMENT_WRITE_ACTION], 900.0)

        defaults = action_limits_from(GatewaySettings())
        self.assertEqual(defaults[ACTION_MOMENTS_COMMENT].max_count, 5, "评论是公开文字，默认比点赞小一个量级")


class RealActionsTests(unittest.TestCase):
    """★ 默认就真的发消息，右下角那句 dry-run 提示因此不出现（用户 2026-08-16）。

    要点是**怎么让它不出现**：靠把档位改成真发，不是把提示藏起来。dry-run 下每条写指令
    照样回 done，AI 端会照着告诉用户「已经发过去了」——这时候还不提示，就成了骗人。
    """

    def _notice(self, *, env: dict[str, str] | None = None) -> str:
        import argparse

        from wxclient.ui.__main__ import _dry_run_notice
        from wxclient.ui.config import UiSettings

        args = argparse.Namespace(allow_real_actions=False)
        with mock.patch.dict(os.environ, env or {}, clear=False):
            return _dry_run_notice(args, UiSettings())

    def test_real_actions_are_on_by_default(self) -> None:
        from wxclient.ui.config import UiSettings

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WXCLIENT_UI_ALLOW_REAL_ACTIONS", None)
            self.assertTrue(UiSettings().allow_real_actions)

    def test_no_dry_run_banner_by_default(self) -> None:
        self.assertEqual(self._notice(), "")

    def test_the_banner_comes_back_when_dry_run_is_switched_on(self) -> None:
        """★ 提示只随档位出现或消失。哪天有人想「把这句话删掉」，先看这条测试。"""
        self.assertIn("不会真的发消息",
                      self._notice(env={"WXCLIENT_UI_ALLOW_REAL_ACTIONS": "0"}))


class SelfCheckTests(unittest.TestCase):
    """★ `--self-check` 是「冻结包缺东西」这一类问题的唯一闸（打包时 build.py 会跑它）。

    2026-08-16 一天撞两次（logger/logging.conf 没收进去、win32ui 被 excludes 砍掉），
    表现都是「装到别人机器上才炸」。所以它必须覆盖**每一条只在冻结包里会断的链**。
    """

    def test_it_also_proves_an_image_can_be_rendered(self) -> None:
        """★ PIL 的 Tk 桥（_imagingtk）是个 C 扩展，最容易被打包漏掉。

        漏了的表现是弹框上一块空白、登录页背景一片白，而且**不抛到眼前**——
        `logo_photo` 自己吞异常（一个图标不该让弹框弹不出来）。所以得由自检去问它。
        """
        from wxclient.ui import __main__ as main_mod

        lines: list[str] = []
        self.assertTrue(main_mod._check_images(lines), f"图像自检没过：{lines}")
        self.assertIn("logo=ok", lines)

    def test_a_broken_image_pipeline_fails_the_check(self) -> None:
        """出不来图就得判失败——否则这道闸等于没有。"""
        from wxclient.ui import __main__ as main_mod
        from wxclient.ui import icons

        lines: list[str] = []
        with mock.patch.object(icons, "logo_photo", lambda *_a, **_k: None):
            self.assertFalse(main_mod._check_images(lines))
        self.assertTrue(any("FAIL" in line for line in lines), "得说清是哪儿断了")


class NoSystemDialogsTests(unittest.TestCase):
    """★ 界面里不许再出现系统弹框（用户 2026-08-16）。

    系统弹框是**另一个程序的东西**：灰底、宋体、系统按钮、屏幕正中蹦出来。人正在这个
    窗口里做事，冷不丁弹一个长得不一样的框，第一反应是「这是谁弹的」。
    自己那份在 `wxclient/ui/dialog.py`，行为测试在 `test_ui_window.DialogTests`。
    """

    def test_no_ui_module_imports_messagebox(self) -> None:
        ui_dir = Path(__file__).resolve().parent.parent / "wxclient" / "ui"
        offenders = []
        for path in sorted(ui_dir.glob("*.py")):
            if path.name == "dialog.py":
                continue  # 它的文档里提到这个名字，是在说「替掉的就是它」
            text = path.read_text(encoding="utf-8")
            if "messagebox" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "★ 这些还在用系统弹框，改走 ui/dialog.py")


class TrayAfterLoginTests(unittest.TestCase):
    """★ 托盘只在登录之后露面（用户 2026-08-16：「登录之后才启动托盘，Loading 中不好
    出现这个」）。

    这里按接线验「谁调、什么时候调」；`Tray` 自己的开关行为在
    `tests/test_ui_window.py::TrayLifecycleTests`。
    """

    def _main_source(self) -> str:
        from wxclient.ui import __main__ as main_mod

        return Path(main_mod.__file__).read_text(encoding="utf-8")

    def test_the_tray_does_not_come_up_during_startup(self) -> None:
        source = self._main_source()
        self.assertNotIn("tray.run_detached()", source,
                         "★ 启动时不许直接把托盘拉起来——那正是 Loading 里冒出图标的原因")
        self.assertNotIn('splash.stage("正在启动托盘', source, "连这句进度都不该有了")

    def test_login_starts_it_and_logout_takes_it_away(self) -> None:
        """两头都要：只在登录时起、退出登录不收，人就退回了登录页而图标还挂着。"""
        source = self._main_source()
        starts = source.count("tray.start()")
        self.assertEqual(starts, 2,
                         "★ 两条登录路各一次：新登录（on_authenticated）和恢复上次登录")
        self.assertIn("tray.stop()", source)


class StdlibShadowGuardTests(unittest.TestCase):
    """★ 仓库根多一个和标准库重名的 .py，打包就会炸得看不出原因（2026-08-16 真机）。

    当时报的是 `SubprocessDiedError: Child process died calling
    discover_hook_directories()`——跟「多了个 enum.py」这个真因毫无关系。
    所以这一条在 build.py 开头就拦。
    """

    def _build_module(self):
        """按路径加载 packaging/build.py。

        ★ 不能写 `from packaging import build`：`packaging` 是 PyPI 上的知名库，
        site-packages 里就有一个，import 到的会是**它**而不是这个目录。
        """
        import importlib.util

        path = Path(__file__).resolve().parent.parent / "packaging" / "build.py"
        spec = importlib.util.spec_from_file_location("_lxz_build", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_shadowing_file_stops_the_build(self) -> None:
        build = self._build_module()
        self.assertIn("enum", sys.stdlib_module_names, "前提：enum 确实是标准库")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "enum.py").write_text("", encoding="utf-8")
            (root / "wxclient.py").write_text("", encoding="utf-8")  # 不重名的不该被拦
            with self.assertRaises(SystemExit) as caught:
                build.check_no_stdlib_shadows(root)

        message = str(caught.exception)
        self.assertIn("enum.py", message)
        self.assertNotIn("wxclient.py", message, "只拦和标准库重名的")
        self.assertIn("挪走或删掉", message, "得说清楚怎么办")

    def test_a_clean_repo_passes(self) -> None:
        build = self._build_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "setup.py").write_text("", encoding="utf-8")
            build.check_no_stdlib_shadows(root)  # 不该抛


class CloseOnLoginPageTests(unittest.TestCase):
    """★ 登录页上点 × 要**真退出**（用户 2026-08-16）。

    这里只能验「接线在不在」——点击行为要真 Tk，在 `tests/test_ui_window.py` 里。
    分开验是有理由的：`window.close_requested` 没接到退出动作时**什么都不做**，
    真机上的表现就是「点 × 没反应」。所以接线本身得有人看着。
    """

    def _main_source(self) -> str:
        from wxclient.ui import __main__ as main_mod

        return Path(main_mod.__file__).read_text(encoding="utf-8")

    def test_the_window_gets_a_quit_action(self) -> None:
        self.assertIn("window.set_quit_action(", self._main_source())

    def test_the_x_and_the_tray_quit_share_one_shutdown(self) -> None:
        """两个入口收的是同一份尾：漏掉 reporter.shutdown() 的那个会让企业端
        一直显示这台机器在线。"""
        source = self._main_source()
        self.assertIn("window.set_quit_action(quit_requested)", source)
        self.assertIn("on_quit=lambda: root.after(0, quit_requested)", source)


class ProbeReasonTests(unittest.TestCase):
    """★ 探测失败时界面上摆的是人话，不是异常原文。

    2026-08-16 真机上那格显示的是「UIA 探测失败：Z:\\」——异常原文是一条路径，
    被状态格截到 12 个字符就只剩一个盘符。对人零信息，还把安装路径漏了出去。
    """

    def test_the_broken_reason_fits_the_status_cell(self) -> None:
        from wxclient.uia_probe.probe import PROBE_BROKEN_REASON, STATUS_CELL_BUDGET

        self.assertLessEqual(len(PROBE_BROKEN_REASON), STATUS_CELL_BUDGET,
                             "写长了会被截成半句话——照这个宽度写，别指望截断")

    def test_the_reason_is_not_the_exception_text(self) -> None:
        from wxclient.uia_probe import probe as probe_mod

        source = Path(probe_mod.__file__).read_text(encoding="utf-8")
        self.assertNotIn('f"UIA 探测失败：{exc}"', source,
                         "异常原文只进日志和 error_detail，不进 reason")

    def test_the_window_does_not_truncate_the_reason(self) -> None:
        source = (Path(__file__).resolve().parent.parent
                  / "wxclient/ui/window.py").read_text(encoding="utf-8")
        self.assertNotIn('probe.get("reason") or "未登录")[:12]', source,
                         "别再截断了：截出来的半句话比不显示更糟")


class InstallerTests(unittest.TestCase):
    """安装包的硬约束。这几条在没装 Inno Setup 的机器上也守得住——判的是 iss 文本。"""

    ROOT = Path(__file__).resolve().parent.parent
    ISS = ROOT / "packaging/installer.iss"
    BUILD = ROOT / "packaging/build.py"

    def setUp(self) -> None:
        self.iss = self.ISS.read_text(encoding="utf-8")

    def test_desktop_shortcut_points_at_the_exe(self) -> None:
        """★ 桌面快捷方式必须**直接指 exe**。

        2026-08 真机上排查过一轮「每次打开先闪一个黑窗」，当时怀疑是打包的问题，
        真凶是快捷方式挂在 `wscript.exe some.vbs` 上（更早的版本挂过 .bat）。
        .bat 必然拉起 cmd 窗口，python.exe 是控制台版，中间套一层 vbs 则让「进程到底
        是谁」在任务管理器里对不上号。安装包是这条最后的闸——它建的快捷方式没人会去改。
        """
        icons = [line for line in self.iss.splitlines()
                 if line.startswith("Name:") and "autodesktop" in line]
        self.assertEqual(len(icons), 1, "桌面快捷方式应当只有一条")
        self.assertIn('Filename: "{app}\\{#AppExe}"', icons[0])
        for forbidden in (".bat", ".vbs", ".cmd", "wscript", "python.exe", "cmd.exe"):
            self.assertNotIn(forbidden, icons[0], f"快捷方式不许经过 {forbidden}")

    def test_it_refuses_to_install_on_windows_it_cannot_run_on(self) -> None:
        """★ 装不了的系统要**当场拒绝**，不能装完了双击没反应。

        包里的 Python 是 3.12（3.9 起不支持 Win7），而 Inno 不写 MinVersion 时默认
        放行到 Win7 SP1。那种失败最贵：用户既看不到报错，也不知道该换什么。
        """
        line = [x for x in self.iss.splitlines() if x.startswith("MinVersion=")]
        self.assertEqual(len(line), 1, "没写 MinVersion：Inno 会放行到 Win7 SP1")
        # Inno 的写法允许带 service pack 后缀（`6.1sp1`），取前面的主次版本号就够判。
        value = line[0].split("=", 1)[1].strip()
        major = float(re.match(r"\d+(\.\d+)?", value).group(0))
        self.assertGreaterEqual(major, 10.0,
                                f"MinVersion={value}：★ Win10 是我们唯一真机验过的档")

    def test_it_only_installs_on_64_bit_windows(self) -> None:
        """冻结包是 64 位的；32 位系统上装了也起不来。"""
        self.assertIn("ArchitecturesAllowed=x64compatible", self.iss)

    def test_installs_without_elevation(self) -> None:
        """★ 不许要管理员权限。

        不是图省事：用 UIA 驱动微信要求两个程序**权限级别相同**（客户端契约 §8 的
        `WECHAT_UIA_PERMISSION_DENIED`）。微信是普通权限跑的，我们提权装、提权跑，
        结果是连不上微信，而报错只说「无法访问微信窗口」——没人查得到是装的时候埋的。
        """
        self.assertIn("PrivilegesRequired=lowest", self.iss)
        self.assertIn("DefaultDirName={localappdata}", self.iss)

    def test_uninstall_keeps_user_data_unless_asked(self) -> None:
        """★ 卸载不默认删数据（登录状态、参数、日志）。卸载多半只是为了装新版。"""
        self.assertIn("MB_DEFBUTTON2", self.iss, "那个问框的默认答案必须是「否」")
        self.assertNotIn("[UninstallDelete]", self.iss,
                         "别用 UninstallDelete 无条件删数据目录，要人明确点头")

    def test_app_id_is_fixed(self) -> None:
        """★ AppId 是产品身份，升级安装靠它认出「装过的是同一个」。改了 = 装成两份。"""
        self.assertRegex(self.iss, r"AppId=\{\{[0-9A-F-]{36}")

    def test_version_has_a_single_source(self) -> None:
        """版本号只来自 `wxclient/__init__.py`：iss 里除了兜底默认值不许再写一个。"""
        self.assertIn("#define AppVersion", self.iss, "只允许出现 #ifndef 的兜底默认值")
        self.assertIn("AppVersion={#AppVersion}", self.iss)
        self.assertIn("__version__", self.BUILD.read_text(encoding="utf-8"))

    def test_build_script_and_iss_agree_on_the_output_name(self) -> None:
        """★ 产物文件名两处都写了一遍，对不上的表现是「打完了说没找到包」。

        这条本来要跑五分钟的打包才暴露，放在单测里两毫秒就知道。
        """
        self.assertIn("OutputBaseFilename={#AppName}_v{#AppVersion}_安装包", self.iss)
        self.assertIn('f"{APP_NAME}_v{version}_安装包.exe"',
                      self.BUILD.read_text(encoding="utf-8"))

    def test_wizard_images_are_ours(self) -> None:
        """★ 向导用自己的图，不用 Inno 自带那张蓝色抽象图（装谁都长一样）。

        三个尺寸都要在：只给一张的表现是 125%/150% 缩放的新电脑上糊成一片。
        """
        self.assertIn("WizardImageFile=assets\\wizard-large.bmp", self.iss)
        assets = self.ROOT / "packaging/assets"
        for stem in ("wizard-large", "wizard-small"):
            for suffix in ("", "@1.5x", "@2x"):
                path = assets / f"{stem}{suffix}.bmp"
                self.assertTrue(path.exists(), f"缺 {path.name}，跑 packaging/make_wizard_images.py")

    def test_chinese_wizard(self) -> None:
        """中文向导。Inno 官方包里没有中文，语言文件是随仓库带的，别指望打包机上有。"""
        self.assertIn("languages\\ChineseSimplified.isl", self.iss)
        self.assertTrue((self.ROOT / "packaging/languages/ChineseSimplified.isl").exists())


class SettingsStoreTests(unittest.TestCase):
    def test_round_trip_keeps_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings_store.save(
                data_dir,
                {
                    "WXCLIENT_UI_ACCOUNTS": "wxid_a,wxid_b",
                    "WXCLIENT_GATEWAY_AI_WS_URL": "wss://example/v1/ws",
                },
            )

            values = settings_store.load(data_dir)

        self.assertEqual(values["WXCLIENT_UI_ACCOUNTS"], "wxid_a,wxid_b")
        self.assertEqual(values["WXCLIENT_GATEWAY_AI_WS_URL"], "wss://example/v1/ws")

    def test_manual_keys_survive_a_save_from_the_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings_store.save(data_dir, {"WXCLIENT_GATEWAY_ALLOW_INSECURE_TRANSPORT": "1"})

            reloaded = settings_store.load(data_dir)

        self.assertEqual(reloaded["WXCLIENT_GATEWAY_ALLOW_INSECURE_TRANSPORT"], "1")

    def test_real_environment_wins_unless_forced(self) -> None:
        environ = {"WXCLIENT_UI_ACCOUNTS": "命令行设的"}

        settings_store.apply_to_environ({"WXCLIENT_UI_ACCOUNTS": "文件里的"}, environ)
        self.assertEqual(environ["WXCLIENT_UI_ACCOUNTS"], "命令行设的")

        settings_store.apply_to_environ({"WXCLIENT_UI_ACCOUNTS": "文件里的"}, environ, force=True)
        self.assertEqual(environ["WXCLIENT_UI_ACCOUNTS"], "文件里的")

    def test_empty_value_never_overwrites(self) -> None:
        environ = {"WXCLIENT_UI_ACCOUNTS": "已有"}

        settings_store.apply_to_environ({"WXCLIENT_UI_ACCOUNTS": ""}, environ, force=True)

        self.assertEqual(environ["WXCLIENT_UI_ACCOUNTS"], "已有")

    def test_effective_falls_back_to_environment(self) -> None:
        # 命令行设的参数文件里没有，界面必须显示出来，否则保存时会把它抹掉
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings_store.save(data_dir, {"WXCLIENT_UI_ACCOUNTS": "文件里的"})

            values = settings_store.effective(
                data_dir,
                {"WXCLIENT_UI_ACCOUNTS": "环境里的", "WXCLIENT_GATEWAY_AI_WS_URL": "wss://env/v1/ws"},
            )

        self.assertEqual(values["WXCLIENT_UI_ACCOUNTS"], "文件里的")  # 文件有值就以文件为准
        self.assertEqual(values["WXCLIENT_GATEWAY_AI_WS_URL"], "wss://env/v1/ws")

    def test_the_business_address_is_filled_in_out_of_the_box(self) -> None:
        """★ 装机的人不该去抄一串域名：抄错的表现是「登录一直转圈」，而人根本不知道
        该怀疑地址。默认值就是正式地址，改了以文件/环境变量为准。
        """
        key = "WXCLIENT_GATEWAY_BUSINESS_API_URL"
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            fresh = settings_store.effective(data_dir, {})
            self.assertEqual(fresh[key], settings_store.DEFAULT_BUSINESS_API_URL)
            self.assertTrue(fresh[key].startswith("https://"), "凭证不得明文过网")

            settings_store.save(data_dir, {key: "https://staging.example"})
            self.assertEqual(settings_store.effective(data_dir, {})[key],
                             "https://staging.example", "填过的地址不会被默认值盖掉")

    def test_no_credential_is_kept_in_the_settings_file(self) -> None:
        """★ 2026-08-15 起这份 env 文件里**一个凭证都没有**。

        原来有一把手抄的 internal key。现在换票用的是登录签发的客户端 Token，它加密存在
        `client-session.json`（DPAPI）。再往 FIELDS 里加一个 secret 字段，就是把凭证又
        请回了明文文件——先想清楚它为什么不能走登录接口。
        """
        secrets = [field.key for field in settings_store.FIELDS if field.secret]

        self.assertEqual(secrets, [], "配置中心不该再有任何密钥输入框")
        self.assertNotIn("WXCLIENT_GATEWAY_INTERNAL_KEY",
                         [field.key for field in settings_store.FIELDS])


class LogLineTests(unittest.TestCase):
    def test_splits_time_level_and_message(self) -> None:
        parsed = logline.parse("2026-08-12 16:38:47 [信息] 网关启动，accounts=1")

        self.assertEqual(parsed.time, "2026-08-12 16:38:47")
        self.assertEqual(parsed.level, "INFO")
        self.assertEqual(parsed.message, "网关启动，accounts=1")
        self.assertEqual(parsed.severity, "info")

    def test_warning_and_error_get_their_own_severity(self) -> None:
        self.assertEqual(logline.parse("2026-08-12 16:38:47 [警告] 慢").severity, "warn")
        self.assertEqual(logline.parse("2026-08-12 16:38:47 [错误] 炸").severity, "error")

    def test_unparsable_line_is_kept_verbatim(self) -> None:
        # 异常栈那种多行输出必须原样留着，少一行日志比多一行难查得多
        parsed = logline.parse("Traceback (most recent call last):")

        self.assertEqual(parsed.time, "")
        self.assertEqual(parsed.level, "")
        self.assertEqual(parsed.message, "Traceback (most recent call last):")

    def test_角色单独成一列(self) -> None:
        parsed = logline.parse("2026-08-30 09:00:00 [信息] [AI端] 已连上 AI 端")

        self.assertEqual(parsed.role, "AI端")
        self.assertEqual(parsed.message, "已连上 AI 端")
        self.assertEqual(parsed.level, "INFO")

    def test_老日志没有角色也照样解析(self) -> None:
        """日志目录里留着 30 天的历史文件，那些行没有角色段——不能因此变成解析不了。"""
        parsed = logline.parse("2026-08-12 16:38:47 [信息] 网关启动，accounts=1")

        self.assertEqual(parsed.role, "")
        self.assertEqual(parsed.message, "网关启动，accounts=1")

    def test_不是角色的方括号原样留在正文里(self) -> None:
        """判据是「正好是认得的那几个角色之一」，否则当正文——猜错会吃掉半句话。"""
        parsed = logline.parse("2026-08-30 09:00:00 [警告] [pyweixin] 库自己打的一行")

        self.assertEqual(parsed.role, "")
        self.assertEqual(parsed.message, "[pyweixin] 库自己打的一行")


class AccountNormalizationTests(unittest.TestCase):
    def test_splits_dedupes_and_trims(self) -> None:
        self.assertEqual(normalize_accounts(" a , b；a ; "), ("a", "b"))

    def test_accepts_a_list(self) -> None:
        self.assertEqual(normalize_accounts([" a ", "", "b"]), ("a", "b"))

    def test_empty_input_yields_nothing(self) -> None:
        self.assertEqual(normalize_accounts(None), ())


class SingleInstanceTests(unittest.TestCase):
    """★ 076 单实例：绝不允许两个进程同时驱动同一个微信。

    2026-08-13 真机出过一次（重启点了两下）：同一条消息被读两遍、回两遍，
    两边还各按各的节拍动窗口——风控最不喜欢的形状，账号状态机也彻底失去意义。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_first_one_wins_second_one_is_told_no(self) -> None:
        first = SingleInstance(self.data_dir)
        second = SingleInstance(self.data_dir)
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire(), "第二个实例必须被挡住")
        first.release()
        self.assertTrue(second.acquire(), "前一个退出后，位置要让出来")
        second.release()

    def test_lock_is_released_when_the_process_dies(self) -> None:
        """★ 锁必须跟着**进程**走：被强杀、蓝屏之后不能留下一把要人工清理的死锁。

        用真子进程验，不是 mock —— 这条的全部价值就在于「操作系统会不会替我们放锁」。
        """
        script = (
            "import sys, time;"
            f"sys.path.insert(0, {str(Path.cwd())!r});"
            "from wxclient.ui.single_instance import SingleInstance;"
            f"g = SingleInstance({str(self.data_dir)!r});"
            "print('OK' if g.acquire() else 'NO', flush=True);"
            "time.sleep(60)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "OK")
            self.assertFalse(
                SingleInstance(self.data_dir).acquire(), "子进程占着锁，这边不该拿到"
            )
        finally:
            child.kill()          # 模拟强杀：没有任何清理机会
            child.wait(timeout=10)
        # ★ 强杀之后别当场断言。Windows 的 TerminateProcess 只是让进程结束，句柄由内核
        #   随后关掉，而 msvcrt 的锁是跟着句柄走的——中间有一小段窗口锁还在。
        #   2026-08-24 真机上这条稳定失败，查下来就是这几十毫秒，产品侧没有问题
        #   （现实里没人能在强杀后的同一瞬间重启应用）。这里等的是「不用人工清理就能
        #   自己好」，不是把断言放宽：等不到就照样红。
        deadline = time.monotonic() + 5.0
        while True:
            guard = SingleInstance(self.data_dir)
            if guard.acquire():
                guard.release()   # 别把句柄留着，tearDown 要删这个目录
                break
            if time.monotonic() >= deadline:
                self.fail("进程没了，锁必须由操作系统自动放掉（等满 5 秒还没放）")
            time.sleep(0.05)

    def test_unusable_data_dir_lets_the_app_start_anyway(self) -> None:
        """★ 锁文件建不出来就放行：为了防重复把**唯一**一个实例也拦在门外，比偶尔重复更糟。

        造一个「父路径是个文件」的数据目录——mkdir 必然失败，且不挑运行身份（root 也一样）。
        """
        blocker = self.data_dir / "not-a-dir"
        blocker.write_text("x")
        self.assertTrue(SingleInstance(blocker / "sub").acquire())

    def test_context_manager_releases(self) -> None:
        with SingleInstance(self.data_dir) as acquired:
            self.assertTrue(acquired)
            self.assertFalse(SingleInstance(self.data_dir).acquire())
        self.assertTrue(SingleInstance(self.data_dir).acquire())
if __name__ == "__main__":
    unittest.main()


class ServiceBacklogLogTests(unittest.TestCase):
    """「积压」这件事从状态栏搬到实时日志（用户 2026-08-21：「欠这个意思也不懂」）。

    状态栏那一格只放得下几个字，塞进去的「欠 N 批」是记账口径不是人话；实时日志有整行的
    地方，能一次讲清「发生了什么、要不要管、会不会重复」。搬过来之后有两条新规矩要钉：
    **只在变了的时候报**（轮询一秒一次，每次都报就是每秒一行刷屏），**没在跑就不报**
    （那时 backlog 恒为 0，停服务会白报一句「清空了」）。
    """

    def _service(self):
        from wxclient.ui.service import GatewayService, ServiceState

        service = GatewayService(lambda: None)  # type: ignore[arg-type,return-value]
        service._state = ServiceState.RUNNING
        return service

    def _events(self, service, values: list[int]) -> list[tuple[int, int]]:
        seen: list[tuple[int, int]] = []
        with mock.patch("wxclient.ui.service._log") as log:
            for value in values:
                service._report_backlog(value)
            for call in log.info.call_args_list:
                self.assertEqual(call.args[0], "gateway.backlog.changed")
                seen.append((call.kwargs["previous"], call.kwargs["backlog"]))
        return seen

    def test_只在变了的时候报一行(self) -> None:
        service = self._service()
        self.assertEqual(
            self._events(service, [0, 0, 3, 3, 3, 5, 5, 0, 0]),
            [(0, 3), (3, 5), (5, 0)],
            "同一个数连着报好几遍就是每秒一行刷屏",
        )

    def test_刚起来就没积压不报(self) -> None:
        """「一切正常」不该在日志里占一行——它是常态，不是事件。"""
        service = self._service()
        self.assertEqual(self._events(service, [0]), [])

    def test_服务没在跑就不报(self) -> None:
        """停服务时 backlog 会归 0，照报就会莫名其妙地说一句「积压清空了」。"""
        from wxclient.ui.service import ServiceState

        service = self._service()
        self._events(service, [3])
        service._state = ServiceState.STOPPED
        self.assertEqual(self._events(service, [0]), [])

    def test_停了再起来会重新报一次(self) -> None:
        """重启之后那份积压还在（它记在库里），得让人重新知道一次。"""
        from wxclient.ui.service import ServiceState

        service = self._service()
        self._events(service, [3])
        service._state = ServiceState.STOPPED
        self._events(service, [0])
        service._state = ServiceState.RUNNING
        self.assertEqual(self._events(service, [3]), [(0, 3)])

    def test_那句话得说人话(self) -> None:
        """钉的是「看的人能不能懂」：不许再出现「欠」，得说清不会丢、不会重复回。"""
        from wxclient.logging_config import ChineseLogRenderer

        def render(**kw: object) -> str:
            return ChineseLogRenderer()(
                logging.getLogger("t"), "info",
                {"timestamp": "2026-08-21 10:00:00", "level": "info",
                 "event": "gateway.backlog.changed", **kw},
            )

        rising = render(backlog=5, previous=0)
        self.assertIn("5 批", rising)
        self.assertIn("读进来", rising)
        self.assertIn("还没回出去", rising)
        self.assertIn("不会丢", rising)
        self.assertNotIn("欠", rising)

        falling = render(backlog=2, previous=5)
        self.assertIn("降到 2 批", falling)
        self.assertIn("补投成功 3 批", falling)

        cleared = render(backlog=0, previous=5)
        self.assertIn("积压清空了", cleared)
        self.assertNotIn("欠", cleared)
