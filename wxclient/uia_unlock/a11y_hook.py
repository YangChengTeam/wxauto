from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes

from .native_com import _create_native_uia_client


def start_a11y_child() -> tuple[int, threading.Thread, threading.Event]:
    """Start a background thread with WinEvent hooks to trigger UIA exposure."""
    stop_event = threading.Event()
    ready_event = threading.Event()

    def _hook_loop() -> None:
        user32 = ctypes.windll.user32
        ole32 = ctypes.windll.ole32

        # Initialize COM on this thread so the UIAutomation client is a real AT client.
        ole32.CoInitialize(None)

        uia_client = None
        try:
            uia_client = _create_native_uia_client()
        except Exception:
            pass

        callback_type = ctypes.WINFUNCTYPE(
            None,
            ctypes.c_void_p,
            ctypes.c_uint,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
        )
        callback = callback_type(lambda *a: None)
        hooks: list[int] = []
        for lo, hi in ((3, 3), (0x8000, 0x8005)):
            hook = user32.SetWinEventHook(lo, hi, None, callback, 0, 0, 2)
            if hook:
                hooks.append(hook)
        ready_event.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if stop_event.is_set():
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        for hook in hooks:
            user32.UnhookWinEvent(hook)
        del uia_client
        ole32.CoUninitialize()

    thread = threading.Thread(target=_hook_loop, daemon=True, name="wx-a11y-hook")
    thread.start()
    ready_event.wait(timeout=2.0)
    return thread.ident or 0, thread, stop_event


def stop_a11y_child(child: tuple[int, threading.Thread, threading.Event]) -> None:
    thread_id, thread, stop_event = child
    stop_event.set()
    # Post WM_QUIT to break the GetMessageW loop.
    ctypes.windll.user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)
    thread.join(timeout=5)
