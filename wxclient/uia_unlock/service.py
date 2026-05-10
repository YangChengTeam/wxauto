from __future__ import annotations

import platform
import subprocess
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any


MAIN_WINDOW_CLASS = "mmui::MainWindow"
LOGIN_WINDOW_CLASS = "mmui::LoginWindow"
SPI_GETSCREENREADER = 0x0046
SPI_SETSCREENREADER = 0x0047
SPIF_SENDCHANGE = 0x0002


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

    main_windows = [item for item in windows if item["class_name"] == MAIN_WINDOW_CLASS]
    login_windows = [item for item in windows if item["class_name"] == LOGIN_WINDOW_CLASS]
    selected = main_windows[0] if main_windows else None
    exposed = bool(selected and selected["descendant_count"] > 10)

    return {
        "platform": "Windows",
        "logged_in": bool(main_windows),
        "login_window": bool(login_windows) and not main_windows,
        "uia_exposed": exposed,
        "main_window_count": len(main_windows),
        "login_window_count": len(login_windows),
        "selected_window": selected,
        "windows": windows,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "reason": "UIA exposed" if exposed else "WeChat UIA is not exposed",
    }


def ensure_wechat_uia(
    restart_if_needed: bool = False,
    wechat_path: str | Path | None = None,
    startup_wait: float = 35.0,
) -> bool:
    probe = probe_wechat_uia()
    if probe.get("uia_exposed") is True:
        return True
    if probe.get("platform") != "Windows":
        return False

    if not restart_if_needed:
        return False

    return unlock_with_narrator(
        wechat_path=wechat_path,
        restart_wechat=True,
        startup_wait=startup_wait,
    )


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
    """Set the Windows screen reader flag used by WeChat to expose UIA nodes."""
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
    return bool(result)


def unlock_with_narrator(
    wechat_path: str | Path | None = None,
    restart_wechat: bool = True,
    startup_wait: float = 35.0,
    stop_narrator_after_wechat: bool = True,
) -> bool:
    if platform.system() != "Windows":
        return False

    start_narrator()
    if restart_wechat:
        close_wechat_windows()
        time.sleep(2)
        start_wechat(wechat_path)

    deadline = time.monotonic() + startup_wait
    exposed = False
    while time.monotonic() < deadline:
        probe = probe_wechat_uia()
        if probe.get("uia_exposed") is True:
            exposed = True
            break
        time.sleep(1)

    if stop_narrator_after_wechat:
        stop_narrator()
        time.sleep(1)
        exposed = bool(probe_wechat_uia().get("uia_exposed"))
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
    for window in _desktop().windows():
        class_name = _safe_call(window.class_name)
        if class_name in {MAIN_WINDOW_CLASS, LOGIN_WINDOW_CLASS}:
            try:
                window.close()
            except Exception:
                continue


def _find_wechat_windows(max_depth: int, max_nodes: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for window in _desktop().windows():
        class_name = _safe_call(window.class_name)
        if class_name not in {MAIN_WINDOW_CLASS, LOGIN_WINDOW_CLASS}:
            continue
        handle = _safe_call(lambda: window.handle)
        text = _safe_call(window.window_text)
        process_id = _safe_call(window.process_id)
        descendant_count = _count_descendants(
            window,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        items.append(
            {
                "handle": handle,
                "process_id": process_id,
                "class_name": class_name,
                "title": text,
                "descendant_count": descendant_count,
            }
        )
    items.sort(key=lambda item: 0 if item["class_name"] == MAIN_WINDOW_CLASS else 1)
    return items


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


def _desktop() -> Any:
    from pywinauto import Desktop

    return Desktop(backend="uia")


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
