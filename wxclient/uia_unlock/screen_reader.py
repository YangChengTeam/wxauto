from __future__ import annotations

import ctypes
import platform
from ctypes import wintypes

SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047


def get_screen_reader_active() -> bool | None:
    """Return the Windows screen-reader accessibility flag, if readable."""
    if platform.system() != "Windows":
        return None
    enabled = wintypes.BOOL()
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETSCREENREADER,
            0,
            ctypes.byref(enabled),
            0,
        )
    except Exception:
        return None
    if not ok:
        return None
    return bool(enabled.value)


def set_screen_reader_active(active: bool) -> bool:
    """Set the volatile Windows screen-reader flag for the current desktop session."""
    if platform.system() != "Windows":
        return False
    try:
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETSCREENREADER,
            int(bool(active)),
            None,
            0,
        )
    except Exception:
        return False
    return bool(ok)


class ScreenReaderFlagSession:
    """Temporarily enable the screen-reader flag and restore the previous value."""

    def __init__(self) -> None:
        self.previous: bool | None = None
        self.enabled = False
        self.restored = False

    def enable(self) -> str:
        self.previous = get_screen_reader_active()
        if self.previous is True:
            self.enabled = True
            return "屏幕阅读器标志已处于开启状态。"
        self.enabled = set_screen_reader_active(True)
        if self.enabled:
            return "屏幕阅读器标志已临时开启。"
        if self.previous is None:
            return "屏幕阅读器标志读取/开启失败，继续尝试其他 UIA 解锁方式。"
        return "屏幕阅读器标志开启失败，继续尝试其他 UIA 解锁方式。"

    def restore(self) -> str | None:
        if self.restored:
            return None
        self.restored = True
        if self.previous is None or self.previous is True:
            return None
        if set_screen_reader_active(False):
            return "屏幕阅读器标志已恢复为关闭。"
        return "屏幕阅读器标志恢复失败，请检查系统辅助功能状态。"

    def snapshot(self) -> dict[str, bool | None]:
        return {
            "previous": self.previous,
            "enabled": self.enabled,
            "restored": self.restored,
            "current": get_screen_reader_active(),
        }
