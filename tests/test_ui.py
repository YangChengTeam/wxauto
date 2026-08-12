"""桌面端里不含 GUI 的那部分：日志缓冲、服务状态机、参数读写、装配。

窗口和托盘本身只能在 Windows 真机看，所以刻意把逻辑都挤在这些能测的模块里。
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from wxclient.gateway.factory import normalize_accounts
from wxclient.ui import logline
from wxclient.ui import settings_store
from wxclient.ui.logbuffer import LogBuffer, attach_to_root
from wxclient.ui.service import GatewayService, ServiceState


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

    def test_stop_before_start_is_harmless(self) -> None:
        service = GatewayService(lambda: _FakeGateway(threading.Event()))

        service.stop(timeout=1)

        self.assertIs(service.state, ServiceState.STOPPED)


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
