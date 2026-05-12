from __future__ import annotations

import platform
import subprocess
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any


WECHAT_WINDOW_TITLE = "微信"
WECHAT_PROCESS_NAMES = {"wechat.exe", "weixin.exe"}
WECHAT_MAIN_CLASSES = {"WeChatMainWndForPC", "WeChatLoginWndForPC"}
WECHAT_UTILITY_CLASSES = {
    "Base_PowerMessageWindow",
    "Chrome_SystemMessageWindow",
    "DisplayICC_SystemMessageWindow",
    "IME",
    "MSCTFIME UI",
    "Qt51514WxTrayIconMessageWindowClass",
    "Sogou_TSF_UI",
    "SoWB_Status",
    "SoWB_UI",
    "libusb-1.0-windows-hotplug",
}
SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
SPIF_SENDCHANGE = 0x0002
SW_SHOW = 5
SW_RESTORE = 9


def probe_wechat_uia(max_depth: int = 10, max_nodes: int = 5000) -> dict[str, Any]:
    """Read-only probe for current WeChat UIA exposure."""
    if platform.system() != "Windows":
        return {
            "platform": platform.system(),
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": "UIA probing is only available on Windows",
        }

    try:
        windows = _find_wechat_windows(max_depth=max_depth, max_nodes=max_nodes)
    except Exception as exc:
        return {
            "platform": "Windows",
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": f"probe failed: {exc}",
        }

    selected = windows[0] if windows else None
    exposed = bool(selected and selected["descendant_count"] > 10)

    return {
        "platform": "Windows",
        "logged_in": bool(windows),
        "login_window": False,
        "uia_exposed": exposed,
        "window_count": len(windows),
        "selected_window": selected,
        "windows": windows,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "reason": "UIA exposed" if exposed else "WeChat UIA is not exposed",
    }


def bootstrap_wechat_uia(
    wechat_path: str | Path | None = None,
    *,
    restart_if_needed: bool = False,
    startup_wait: float = 35.0,
    quick_timeout: float = 5.0,
    probe_max_depth: int = 4,
    probe_max_nodes: int = 120,
) -> dict[str, Any]:
    """Try a fast unlock first, then optionally fall back to Narrator + restart."""
    bring_wechat_to_front(timeout=2.0)
    probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
    if probe.get("uia_exposed") is True:
        return {"success": True, "mode": "already", "probe": probe}
    if probe.get("platform") != "Windows":
        return {"success": False, "mode": "unsupported", "probe": probe}

    if try_unlock_without_restart(
        wechat_path=wechat_path,
        timeout=quick_timeout,
        probe_max_depth=probe_max_depth,
        probe_max_nodes=probe_max_nodes,
    ):
        probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
        return {"success": bool(probe.get("uia_exposed")), "mode": "quick", "probe": probe}

    probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
    if probe.get("uia_exposed") is True:
        return {"success": True, "mode": "quick", "probe": probe}
    if not restart_if_needed:
        return {"success": False, "mode": "quick_failed", "probe": probe}

    success = unlock_with_narrator(
        wechat_path=wechat_path,
        restart_wechat=True,
        startup_wait=startup_wait,
        probe_max_depth=probe_max_depth,
        probe_max_nodes=probe_max_nodes,
    )
    probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
    return {
        "success": bool(success and probe.get("uia_exposed")),
        "mode": "restart" if success and probe.get("uia_exposed") else "restart_failed",
        "probe": probe,
    }


def ensure_wechat_uia(
    restart_if_needed: bool = False,
    wechat_path: str | Path | None = None,
    startup_wait: float = 35.0,
) -> bool:
    result = bootstrap_wechat_uia(
        wechat_path=wechat_path,
        restart_if_needed=restart_if_needed,
        startup_wait=startup_wait,
    )
    return bool(result.get("success"))


def get_screen_reader_active() -> bool | None:
    """Return the Windows screen reader flag, or None when it cannot be read."""
    if platform.system() != "Windows":
        return None

    try:
        value = wintypes.BOOL()
        result = _system_parameters_info(
            SPI_GETSCREENREADER,
            0,
            ctypes.byref(value),
            0,
        )
    except Exception:
        return None
    if not result:
        return None
    return bool(value.value)


def set_screen_reader_active(active: bool) -> bool:
    """Set the Windows screen reader flag and broadcast the change to all windows."""
    if platform.system() != "Windows":
        return False

    try:
        result = _system_parameters_info(
            SPI_SETSCREENREADER,
            1 if active else 0,
            None,
            SPIF_SENDCHANGE,
        )
    except Exception:
        return False
    if not result:
        return False
    notify_screen_reader_change()
    return True


def notify_screen_reader_change() -> bool:
    """Broadcast WM_SETTINGCHANGE so running apps re-check accessibility settings."""
    if platform.system() != "Windows":
        return False
    try:
        user32 = ctypes.windll.user32
        SMTO_ABORTIFHUNG = 0x0002
        SMTO_BLOCK = 0x0020
        result = ctypes.c_ulong_PTR(0)
        user32.SendMessageTimeoutW.restype = ctypes.c_ulong_PTR
        user32.SendMessageTimeoutW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
            ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_ulong_PTR),
        ]
        ret = user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Accessibility",
            SMTO_ABORTIFHUNG | SMTO_BLOCK, 5000, result,
        )
        return bool(ret)
    except Exception:
        return False


def try_unlock_without_restart(
    wechat_path: str | Path | None = None,
    timeout: float = 5.0,
    *,
    probe_max_depth: int = 4,
    probe_max_nodes: int = 120,
) -> bool:
    """Try to unlock UIA by setting the screen reader flag + broadcast, without restarting WeChat."""
    if platform.system() != "Windows":
        return False
    bring_wechat_to_front(timeout=2.0)
    probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
    if probe.get("uia_exposed"):
        return True
    if not set_screen_reader_active(True):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes).get("uia_exposed"):
            return True
    return False


def unlock_with_narrator(
    wechat_path: str | Path | None = None,
    restart_wechat: bool = True,
    startup_wait: float = 35.0,
    stop_narrator_after_wechat: bool = True,
    *,
    probe_max_depth: int = 4,
    probe_max_nodes: int = 120,
) -> bool:
    if platform.system() != "Windows":
        return False

    if not restart_wechat:
        bring_wechat_to_front(timeout=2.0)
    start_narrator()
    if restart_wechat:
        close_wechat_windows()
        time.sleep(2)
        start_wechat(wechat_path)
        bring_wechat_to_front(timeout=min(8.0, startup_wait))

    deadline = time.monotonic() + startup_wait
    exposed = False
    while time.monotonic() < deadline:
        probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
        if probe.get("uia_exposed") is True:
            exposed = True
            break
        time.sleep(1)

    if stop_narrator_after_wechat:
        stop_narrator()
        time.sleep(1)
        exposed = bool(
            probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes).get(
                "uia_exposed"
            )
        )
    return exposed


def start_narrator() -> None:
    subprocess.Popen(
        ["Narrator.exe"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def stop_narrator() -> None:
    subprocess.run(
        ["taskkill", "/IM", "Narrator.exe", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def start_wechat(wechat_path: str | Path | None = None) -> None:
    path = Path(wechat_path) if wechat_path else _guess_wechat_path()
    if not path or not path.exists():
        raise RuntimeError("WeChat executable path was not found")
    subprocess.Popen(
        [str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def close_wechat_windows() -> None:
    for candidate in _find_wechat_window_candidates():
        window = candidate.get("_window")
        if window is None:
            continue
        try:
            window.close()
        except Exception:
            continue


def bring_wechat_to_front(timeout: float = 3.0) -> bool:
    """Best-effort foreground activation before UIA unlock attempts."""
    if platform.system() != "Windows":
        return False

    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        for candidate in _find_wechat_window_candidates():
            if _focus_wechat_candidate(candidate):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def _find_wechat_windows(max_depth: int, max_nodes: int) -> list[dict[str, Any]]:
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


def _restore_and_foreground_window(handle: int) -> bool:
    try:
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        if not user32.IsWindow(handle):
            return False

        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        show_command = SW_RESTORE if user32.IsIconic(handle) else SW_SHOW
        user32.ShowWindow(handle, show_command)
        user32.BringWindowToTop(handle)
        return bool(user32.SetForegroundWindow(handle))
    except Exception:
        return False


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
        candidates.append(
            {
                "_window": window,
                "handle": handle,
                "process_id": process_id,
                "process_name": process_name,
                "process_path": process_info.get("path"),
                "class_name": class_name,
                "title": title,
                "score": _wechat_candidate_score(title, class_name),
            }
        )
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


def _wechat_candidate_score(title: str, class_name: str) -> int:
    score = 0
    if title == WECHAT_WINDOW_TITLE:
        score += 30
    if class_name in WECHAT_MAIN_CLASSES:
        score += 10
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


def _system_parameters_info(
    action: int,
    ui_param: int,
    pv_param: Any,
    flags: int,
) -> bool:
    user32 = ctypes.windll.user32
    system_parameters_info = user32.SystemParametersInfoW
    system_parameters_info.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        wintypes.UINT,
    ]
    system_parameters_info.restype = wintypes.BOOL
    return bool(system_parameters_info(action, ui_param, pv_param, flags))


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
