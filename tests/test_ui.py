"""桌面端里不含 GUI 的那部分：日志缓冲、服务状态机、参数读写、装配。

窗口和托盘本身只能在 Windows 真机看，所以刻意把逻辑都挤在这些能测的模块里。
"""

from __future__ import annotations

import itertools
import logging
import os
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

    def test_secret_field_is_declared_as_secret(self) -> None:
        # 内部 key 一旦当成普通字段就会明文显示在界面上
        internal_key = next(f for f in settings_store.FIELDS if f.key.endswith("INTERNAL_KEY"))
        self.assertTrue(internal_key.secret)


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


class AccountNormalizationTests(unittest.TestCase):
    def test_splits_dedupes_and_trims(self) -> None:
        self.assertEqual(normalize_accounts(" a , b；a ; "), ("a", "b"))

    def test_accepts_a_list(self) -> None:
        self.assertEqual(normalize_accounts([" a ", "", "b"]), ("a", "b"))

    def test_empty_input_yields_nothing(self) -> None:
        self.assertEqual(normalize_accounts(None), ())


if __name__ == "__main__":
    unittest.main()


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
        self.assertTrue(
            SingleInstance(self.data_dir).acquire(), "进程没了，锁必须由操作系统自动放掉"
        )

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
