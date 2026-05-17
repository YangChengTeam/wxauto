from __future__ import annotations

import ctypes
import platform
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .constants import (
    DEFAULT_UIA_PROBE_MAX_DEPTH,
    DEFAULT_UIA_PROBE_MAX_NODES,
    HWND_TOP,
    MIN_UIA_EXPOSED_DESCENDANTS,
    SW_SHOW,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOOWNERZORDER,
    SWP_SHOWWINDOW,
    NATIVE_UIA_LOGIN_CLASSES,
    NATIVE_UIA_MAIN_CLASSES,
    WECHAT_MAIN_CLASSES,
    WECHAT_PROCESS_NAMES,
    WECHAT_UTILITY_CLASSES,
    WECHAT_WINDOW_TITLE,
)

FOREGROUND_PROBE_MAX_DEPTH = DEFAULT_UIA_PROBE_MAX_DEPTH
FOREGROUND_PROBE_MAX_NODES = DEFAULT_UIA_PROBE_MAX_NODES
MIN_FOREGROUND_WINDOW_WIDTH = 240
MIN_FOREGROUND_WINDOW_HEIGHT = 180


def start_wechat(wechat_path: str | Path | None = None) -> bool:
    if _is_wechat_process_running():
        return False
    path = Path(wechat_path) if wechat_path else _guess_wechat_path()
    if not path or not path.exists():
        raise RuntimeError("WeChat executable path was not found")
    subprocess.Popen(
        [str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def close_wechat_windows() -> None:
    for candidate in _find_wechat_window_candidates():
        window = candidate.get("_window")
        if window is None:
            continue
        try:
            window.close()
        except Exception:
            continue


def ensure_wechat_foreground(timeout: float = 3.0) -> bool:
    """Bring WeChat to the foreground and verify it is the active window."""
    if platform.system() != "Windows":
        return False
    if _is_wechat_foreground():
        return True
    return bring_wechat_to_front(timeout=timeout) and _is_wechat_foreground()


def bring_wechat_to_front(timeout: float = 3.0) -> bool:
    """Best-effort foreground activation before UIA unlock attempts."""
    if platform.system() != "Windows":
        return False

    use_pyweixin = timeout > 0
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        if use_pyweixin:
            handle = _bring_wechat_to_front_with_pyweixin()
            if handle and _is_foreground_window(handle):
                return True
            if _is_wechat_foreground():
                return True

        candidates = _safe_wechat_window_candidates()
        for candidate in _foreground_candidates(candidates):
            handle = candidate.get("handle")
            if isinstance(handle, int) and handle > 0:
                _restore_and_foreground_window(handle)
                if _is_foreground_window(handle):
                    return True

        hwnd = _find_wechat_window_handle_by_title()
        if hwnd:
            _restore_and_foreground_window(hwnd)
        if hwnd and _is_foreground_window(hwnd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def find_wechat_windows(max_depth: int, max_nodes: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for candidate in _find_wechat_window_candidates():
        handle = candidate.get("handle")
        if not handle:
            continue
        try:
            window = _desktop("uia").window(handle=handle)
        except Exception:
            window = candidate.get("_window")
        descendant_count = _count_descendants(
            window,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        items.append(
            {
                "handle": handle,
                "process_id": candidate.get("process_id"),
                "process_name": candidate.get("process_name"),
                "process_path": candidate.get("process_path"),
                "class_name": candidate.get("class_name"),
                "uia_class_name": candidate.get("uia_class_name"),
                "uia_descendant_count": candidate.get("uia_descendant_count"),
                "is_visible": candidate.get("is_visible"),
                "is_iconic": candidate.get("is_iconic"),
                "rect": candidate.get("rect"),
                "title": candidate.get("title"),
                "descendant_count": descendant_count,
                "candidate_score": candidate.get("score", 0),
            }
        )
    items.sort(key=lambda item: (-item["descendant_count"], -item["candidate_score"]))
    return items


def _focus_wechat_candidate(candidate: dict[str, Any]) -> bool:
    handle = candidate.get("handle")
    if isinstance(handle, int) and handle > 0:
        if _restore_and_foreground_window(handle):
            return True

    window = candidate.get("_window")
    if window is None:
        return False
    try:
        window.restore()
    except Exception:
        pass
    try:
        window.set_focus()
        return True
    except Exception:
        return False


def _bring_wechat_to_front_with_pyweixin() -> int:
    try:
        navigator = _pyweixin_class("Navigator")
        main_window = navigator.open_weixin(is_maximize=False)
        handle = int(getattr(main_window, "handle", 0) or 0)
        return handle
    except Exception:
        return 0


def _pyweixin_class(name: str) -> Any:
    vendor = Path(__file__).resolve().parents[2] / "third_party" / "pywechat"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    module = __import__("pyweixin", fromlist=[name, "GlobalConfig"])
    config = getattr(module, "GlobalConfig", None)
    if config is not None:
        config.close_weixin = False
        config.is_maximize = False
    return getattr(module, name)


def _restore_and_foreground_window(handle: int) -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        if not user32.IsWindow(handle):
            return False

        visibility = _window_visibility_snapshot(handle)
        if visibility.get("iconic") or visibility.get("offscreen_minimized"):
            return False

        try:
            user32.ShowWindowAsync(handle, SW_SHOW)
        except Exception:
            user32.ShowWindow(handle, SW_SHOW)
        user32.SetWindowPos(
            handle,
            HWND_TOP,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOOWNERZORDER,
        )
        user32.BringWindowToTop(handle)
        focused = _set_foreground_window_with_thread_input(handle)
        if focused:
            time.sleep(0.05)
        return focused or _is_foreground_window(handle)
    except Exception:
        return False


def _set_foreground_window_with_thread_input(handle: int) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    foreground = _foreground_window_handle()
    target_thread_id = _window_thread_id(handle)
    foreground_thread_id = _window_thread_id(foreground)
    current_thread_id = int(kernel32.GetCurrentThreadId())

    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL

    attached_pairs: list[tuple[int, int]] = []
    for source_thread_id in {current_thread_id, foreground_thread_id}:
        if (
            source_thread_id
            and target_thread_id
            and source_thread_id != target_thread_id
            and user32.AttachThreadInput(source_thread_id, target_thread_id, True)
        ):
            attached_pairs.append((source_thread_id, target_thread_id))

    try:
        try:
            user32.AllowSetForegroundWindow(0xFFFFFFFF)
        except Exception:
            pass
        user32.BringWindowToTop(handle)
        user32.SetActiveWindow(handle)
        user32.SetFocus(handle)
        return bool(user32.SetForegroundWindow(handle))
    finally:
        for source_thread_id, target_thread_id in reversed(attached_pairs):
            user32.AttachThreadInput(source_thread_id, target_thread_id, False)


def _is_wechat_foreground() -> bool:
    handle = _foreground_window_handle()
    if not handle:
        return False
    for candidate in _safe_wechat_window_candidates():
        if candidate.get("handle") == handle and _is_wechat_identity_candidate(candidate):
            return True
    class_name = _window_class_name(handle)
    process_name = _process_name_for_handle(handle)
    return _is_wechat_window_candidate(WECHAT_WINDOW_TITLE, class_name, process_name)


def _is_foreground_window(handle: int) -> bool:
    return bool(handle and _foreground_window_handle() == handle)


def _foreground_window_handle() -> int:
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _find_wechat_window_handle_by_title() -> int:
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = wintypes.HWND
        handle = int(user32.FindWindowW(None, WECHAT_WINDOW_TITLE) or 0)
        if not _is_foregroundable_wechat_handle(handle):
            return 0
        return handle
    except Exception:
        return 0


def _safe_wechat_window_candidates() -> list[dict[str, Any]]:
    try:
        return _find_wechat_window_candidates()
    except Exception:
        return []


def _foreground_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [candidate for candidate in candidates if _is_foregroundable_wechat_candidate(candidate)]


def _is_qt_shell_candidate(candidate: dict[str, Any]) -> bool:
    class_name = str(candidate.get("class_name") or "")
    return class_name.startswith("Qt") and "QWindowIcon" in class_name


def _is_foregroundable_wechat_handle(handle: int) -> bool:
    if not handle:
        return False
    class_name = _window_class_name(handle)
    if not _is_wechat_window_candidate(WECHAT_WINDOW_TITLE, class_name, _process_name_for_handle(handle)):
        return False
    candidate = _candidate_from_handle(handle, class_name=class_name, title=WECHAT_WINDOW_TITLE)
    return _is_foregroundable_wechat_candidate(candidate)


def _is_foregroundable_wechat_candidate(candidate: dict[str, Any]) -> bool:
    class_name = str(candidate.get("class_name") or "")
    if class_name in WECHAT_MAIN_CLASSES:
        return True
    if not _is_qt_shell_candidate(candidate):
        return False
    handle = candidate.get("handle")
    if isinstance(handle, int) and handle > 0:
        visibility = _window_visibility_snapshot(handle)
        if visibility.get("iconic") or visibility.get("offscreen_minimized"):
            return False
        if _is_normal_visible_window(visibility):
            return True

    uia_class_name = str(candidate.get("uia_class_name") or "")
    if uia_class_name not in NATIVE_UIA_MAIN_CLASSES | NATIVE_UIA_LOGIN_CLASSES:
        return False

    descendant_count = int(candidate.get("uia_descendant_count") or 0)
    return descendant_count >= MIN_UIA_EXPOSED_DESCENDANTS


def _is_wechat_identity_candidate(candidate: dict[str, Any]) -> bool:
    class_name = str(candidate.get("class_name") or "")
    if class_name in WECHAT_MAIN_CLASSES:
        return True
    if not _is_qt_shell_candidate(candidate):
        return False
    uia_class_name = str(candidate.get("uia_class_name") or "")
    return uia_class_name in NATIVE_UIA_MAIN_CLASSES | NATIVE_UIA_LOGIN_CLASSES


def _candidate_from_handle(
    handle: int,
    *,
    class_name: str,
    title: str,
    process_id: int | None = None,
) -> dict[str, Any]:
    process_id = process_id if process_id is not None else _window_process_id(handle)
    process_info = _process_info(process_id)
    uia_class_name, uia_descendant_count = _uia_snapshot_for_handle(handle)
    visibility = _window_visibility_snapshot(handle)
    return {
        "handle": handle,
        "process_id": process_id,
        "process_name": process_info.get("name"),
        "process_path": process_info.get("path"),
        "class_name": class_name,
        "uia_class_name": uia_class_name,
        "uia_descendant_count": uia_descendant_count,
        "is_visible": visibility.get("visible"),
        "is_iconic": visibility.get("iconic"),
        "rect": visibility.get("rect"),
        "title": title,
        "score": _wechat_candidate_score(title, class_name, uia_class_name, uia_descendant_count),
    }


def _window_visibility_snapshot(handle: int) -> dict[str, Any]:
    if platform.system() != "Windows" or not handle:
        return {}
    try:
        user32 = ctypes.windll.user32
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL

        rect = wintypes.RECT()
        has_rect = bool(user32.GetWindowRect(handle, ctypes.byref(rect)))
        left = int(rect.left) if has_rect else 0
        top = int(rect.top) if has_rect else 0
        right = int(rect.right) if has_rect else 0
        bottom = int(rect.bottom) if has_rect else 0
        width = max(0, right - left)
        height = max(0, bottom - top)
        return {
            "visible": bool(user32.IsWindowVisible(handle)),
            "iconic": bool(user32.IsIconic(handle)),
            "rect": (left, top, right, bottom),
            "width": width,
            "height": height,
            "offscreen_minimized": left <= -30000 or top <= -30000,
        }
    except Exception:
        return {}


def _is_normal_visible_window(visibility: dict[str, Any]) -> bool:
    return bool(
        visibility.get("visible")
        and int(visibility.get("width") or 0) >= MIN_FOREGROUND_WINDOW_WIDTH
        and int(visibility.get("height") or 0) >= MIN_FOREGROUND_WINDOW_HEIGHT
    )


def _window_class_name(handle: int) -> str:
    try:
        user32 = ctypes.windll.user32
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, buffer, len(buffer))
        return buffer.value
    except Exception:
        return ""


def _window_process_id(handle: int) -> int:
    try:
        user32 = ctypes.windll.user32
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        return int(process_id.value)
    except Exception:
        return 0


def _window_thread_id(handle: int) -> int:
    if not handle:
        return 0
    try:
        user32 = ctypes.windll.user32
        process_id = wintypes.DWORD()
        return int(user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id)) or 0)
    except Exception:
        return 0


def _process_name_for_handle(handle: int) -> str:
    process_info = _process_info(_window_process_id(handle))
    return str(process_info.get("name") or "")


def _uia_snapshot_for_handle(handle: int) -> tuple[str, int]:
    try:
        window = _desktop("uia").window(handle=handle)
        class_name = str(_safe_call(window.class_name) or "")
        descendants = _count_descendants(
            window,
            max_depth=FOREGROUND_PROBE_MAX_DEPTH,
            max_nodes=FOREGROUND_PROBE_MAX_NODES,
        )
        return class_name, descendants
    except Exception:
        return "", 0


def _find_wechat_window_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_handles: set[int] = set()
    for window in _desktop("win32").windows():
        title = str(_safe_call(window.window_text) or "").strip()
        class_name = str(_safe_call(window.class_name) or "").strip()
        handle = _safe_call(lambda: window.handle)
        if not isinstance(handle, int) or handle in seen_handles:
            continue
        process_id = _safe_call(window.process_id)
        process_info = _process_info(process_id)
        process_name = str(process_info.get("name") or "").strip()
        if not _is_wechat_window_candidate(title, class_name, process_name):
            continue
        seen_handles.add(handle)
        candidate = _candidate_from_handle(
            handle,
            class_name=class_name,
            title=title,
            process_id=process_id,
        )
        candidate["_window"] = window
        candidate["process_name"] = process_name
        candidate["process_path"] = process_info.get("path")
        candidates.append(candidate)
    candidates.sort(key=lambda item: -int(item.get("score") or 0))
    return candidates


def _is_wechat_window_candidate(title: str, class_name: str, process_name: str) -> bool:
    if process_name.lower() not in WECHAT_PROCESS_NAMES:
        return False
    if title != WECHAT_WINDOW_TITLE:
        return False
    if class_name in WECHAT_UTILITY_CLASSES:
        return False
    if title == "WxTrayIconMessageWindow" or "TrayIcon" in class_name:
        return False
    if class_name in WECHAT_MAIN_CLASSES:
        return True
    return "QWindowIcon" in class_name and class_name.startswith("Qt")


def _wechat_candidate_score(
    title: str,
    class_name: str,
    uia_class_name: str = "",
    uia_descendant_count: int = 0,
) -> int:
    score = 0
    if title == WECHAT_WINDOW_TITLE:
        score += 30
    if class_name in WECHAT_MAIN_CLASSES:
        score += 10
    if uia_class_name in NATIVE_UIA_MAIN_CLASSES | NATIVE_UIA_LOGIN_CLASSES:
        score += 10
    if uia_descendant_count > 0:
        score += min(uia_descendant_count, 20)
    if "QWindowIcon" in class_name:
        score += 5
    return score


def _process_info(process_id: Any) -> dict[str, str]:
    if not isinstance(process_id, int):
        return {}
    try:
        import psutil

        process = psutil.Process(process_id)
        return {
            "name": process.name(),
            "path": process.exe(),
        }
    except Exception:
        return {}


def _is_wechat_process_running() -> bool:
    try:
        import psutil

        for process in psutil.process_iter(["name"]):
            name = str(process.info.get("name") or "").lower()
            if name in WECHAT_PROCESS_NAMES:
                return True
    except Exception:
        return False
    return False


def _count_descendants(window: Any, max_depth: int, max_nodes: int) -> int:
    count = 0
    queue: list[tuple[Any, int]] = [(window, 0)]
    while queue and count < max_nodes:
        node, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = node.children()
        except Exception:
            continue
        count += len(children)
        if count >= max_nodes:
            return max_nodes
        queue.extend((child, depth + 1) for child in children)
    return count


def _desktop(backend: str = "uia") -> Any:
    from pywinauto import Desktop

    return Desktop(backend=backend)


def _guess_wechat_path() -> Path | None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin") as key:
            install_dir = winreg.QueryValueEx(key, "InstallPath")[0]
        return Path(install_dir) / "Weixin.exe"
    except Exception:
        return None


def _safe_call(func: Any) -> Any:
    try:
        return func()
    except Exception:
        return None
