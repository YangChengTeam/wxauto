from __future__ import annotations

import atexit
import platform
import threading
from typing import Any, Callable

from .a11y_hook import start_a11y_child, stop_a11y_child
from .native_com import NativeComClientSession
from .screen_reader import ScreenReaderFlagSession

LogFn = Callable[[str], None]


class UiaAccessibilityKeepAlive:
    """Keep the accessibility-facing UIA clients alive while the app runs."""

    def __init__(self) -> None:
        self.screen_reader = ScreenReaderFlagSession()
        self.native_com = NativeComClientSession()
        self.a11y_child: tuple[int, threading.Thread, threading.Event] | None = None
        self.started = False

    def start(self, log: LogFn | None = None) -> None:
        self.start_native_fallback(log=log)

    def start_screen_reader(self, log: LogFn | None = None) -> None:
        if platform.system() != "Windows":
            _log(log, "屏幕阅读器标志仅支持 Windows。")
            return
        if self.screen_reader.enabled and not self.screen_reader.restored:
            _log(log, "屏幕阅读器标志会话已在运行。")
            self.started = True
            return
        _log(log, self.screen_reader.enable())
        self.started = True

    def start_a11y_hook(self, log: LogFn | None = None) -> None:
        if platform.system() != "Windows":
            _log(log, "辅助功能钩子仅支持 Windows。")
            return
        if self.a11y_child is None or not self.a11y_child[1].is_alive():
            self.a11y_child = start_a11y_child()
            _log(log, "辅助功能钩子常驻线程已启动。")
        else:
            _log(log, "辅助功能钩子常驻线程已在运行。")
        self.started = True

    def start_native_fallback(self, log: LogFn | None = None) -> None:
        if platform.system() != "Windows":
            _log(log, "UIA 辅助功能常驻会话仅支持 Windows。")
            return
        self.start_screen_reader(log=log)
        self.start_a11y_hook(log=log)
        _log(log, self.native_com.start(log=log))
        self.started = True

    def stop(self, log: LogFn | None = None) -> None:
        child = self.a11y_child
        self.a11y_child = None
        if child is not None:
            try:
                stop_a11y_child(child)
                _log(log, "辅助功能钩子常驻线程已停止。")
            except Exception as exc:
                _log(log, f"辅助功能钩子常驻线程停止失败：{exc}")

        native_message = self.native_com.stop()
        if native_message:
            _log(log, native_message)

        restore_message = self.screen_reader.restore()
        if restore_message:
            _log(log, restore_message)
        self.started = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "screen_reader": self.screen_reader.snapshot(),
            "native_com": self.native_com.snapshot(),
            "a11y_hook": {
                "alive": bool(self.a11y_child and self.a11y_child[1].is_alive()),
                "thread_id": self.a11y_child[0] if self.a11y_child else None,
            },
        }


_LOCK = threading.Lock()
_SESSION: UiaAccessibilityKeepAlive | None = None


def ensure_uia_keepalive(log: LogFn | None = None) -> UiaAccessibilityKeepAlive:
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            _SESSION = UiaAccessibilityKeepAlive()
        _SESSION.start_native_fallback(log=log)
        return _SESSION


def ensure_screen_reader_keepalive(log: LogFn | None = None) -> UiaAccessibilityKeepAlive:
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            _SESSION = UiaAccessibilityKeepAlive()
        _SESSION.start_screen_reader(log=log)
        return _SESSION


def ensure_a11y_hook_keepalive(log: LogFn | None = None) -> UiaAccessibilityKeepAlive:
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            _SESSION = UiaAccessibilityKeepAlive()
        _SESSION.start_a11y_hook(log=log)
        return _SESSION


def stop_uia_keepalive(log: LogFn | None = None) -> None:
    global _SESSION
    with _LOCK:
        if _SESSION is None:
            return
        _SESSION.stop(log=log)
        _SESSION = None


def uia_keepalive_snapshot() -> dict[str, Any]:
    with _LOCK:
        if _SESSION is None:
            return {}
        return _SESSION.snapshot()


def _log(log: LogFn | None, message: str | None) -> None:
    if log is not None and message:
        log(message)


atexit.register(stop_uia_keepalive)
