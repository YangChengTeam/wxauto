from __future__ import annotations

import ctypes
import platform
from ctypes import wintypes
from typing import Any

from .constants import (
    UIA_LOGIN_CLASSES,
    UIA_MAIN_CLASSES,
    WECHAT_MAIN_CLASSES,
    WECHAT_PROCESS_NAMES,
    WECHAT_UTILITY_CLASSES,
    WECHAT_WINDOW_TITLE,
)


def find_wechat_windows(max_depth: int, max_nodes: int) -> list[dict[str, Any]]:
    """Return read-only snapshots of WeChat windows and their UIA trees."""
    items: list[dict[str, Any]] = []
    for candidate in _find_wechat_window_candidates():
        handle = candidate.get("handle")
        if not handle:
            continue
        try:
            window = _desktop("uia").window(handle=handle)
        except Exception:
            window = candidate.get("_window")
        descendant_count = _count_descendants(window, max_depth=max_depth, max_nodes=max_nodes)
        items.append(
            {
                "handle": handle,
                "process_id": candidate.get("process_id"),
                "process_name": candidate.get("process_name"),
                "process_path": candidate.get("process_path"),
                # ★ 这份字典是探测结果**对外的形状**，上报层只看得见这里的键。
                # 2026-08-15：版本号加进了候选窗口却漏在这一层，于是上报出去的
                # wechatInstance.processVersion 一直是 undefined，服务端每次退回 400。
                "process_version": candidate.get("process_version"),
                "class_name": candidate.get("class_name"),
                "uia_class_name": candidate.get("uia_class_name"),
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


def foreground_window_handle() -> int:
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def safe_wechat_window_candidates() -> list[dict[str, Any]]:
    try:
        return _find_wechat_window_candidates()
    except Exception:
        return []


def process_info(process_id: Any) -> dict[str, str]:
    if not isinstance(process_id, int):
        return {}
    try:
        import psutil

        process = psutil.Process(process_id)
        return {"name": process.name(), "path": process.exe()}
    except Exception:
        return {}


#: exe 路径 → 文件版本号。版本号一个进程里问一次就够，别每 15 秒的探测都去读一遍文件。
_VERSION_CACHE: dict[str, str] = {}


def file_version(path: Any) -> str:
    """读微信 exe 的文件版本（如 ``4.0.1.15``）。

    上报给业务端做兼容性判断用（客户端状态接口契约 §8 的 ``processVersion``）。
    读不到就给空串——这个字段是可选的，编一个版本号出来只会让兼容性判断更糟。
    """
    if not isinstance(path, str) or not path:
        return ""
    if path in _VERSION_CACHE:
        return _VERSION_CACHE[path]
    version = ""
    try:
        import win32api

        info = win32api.GetFileVersionInfo(path, "\\")
        most, least = info["FileVersionMS"], info["FileVersionLS"]
        version = ".".join(str(part) for part in (
            most >> 16, most & 0xFFFF, least >> 16, least & 0xFFFF))
    except Exception:
        version = ""
    _VERSION_CACHE[path] = version
    return version


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
        info = process_info(process_id)
        process_name = str(info.get("name") or "").strip()
        if not is_wechat_window_candidate(title, class_name, process_name):
            continue
        seen_handles.add(handle)
        uia_class_name, uia_descendant_count = _uia_snapshot_for_handle(handle)
        visibility = _window_visibility_snapshot(handle)
        candidates.append(
            {
                "_window": window,
                "handle": handle,
                "process_id": process_id,
                "process_name": process_name,
                "process_path": info.get("path"),
                "process_version": file_version(info.get("path")),
                "class_name": class_name,
                "uia_class_name": uia_class_name,
                "uia_descendant_count": uia_descendant_count,
                "is_visible": visibility.get("visible"),
                "is_iconic": visibility.get("iconic"),
                "rect": visibility.get("rect"),
                "title": title,
                "score": _wechat_candidate_score(
                    title, class_name, uia_class_name, uia_descendant_count
                ),
            }
        )
    candidates.sort(key=lambda item: -int(item.get("score") or 0))
    return candidates


def is_wechat_window_candidate(title: str, class_name: str, process_name: str) -> bool:
    if process_name.lower() not in WECHAT_PROCESS_NAMES:
        return False
    if title != WECHAT_WINDOW_TITLE:
        return False
    if class_name in WECHAT_UTILITY_CLASSES or "TrayIcon" in class_name:
        return False
    if class_name in WECHAT_MAIN_CLASSES:
        return True
    return class_name.startswith("Qt") and "QWindowIcon" in class_name


def _wechat_candidate_score(
    title: str,
    class_name: str,
    uia_class_name: str = "",
    uia_descendant_count: int = 0,
) -> int:
    score = 30 if title == WECHAT_WINDOW_TITLE else 0
    if class_name in WECHAT_MAIN_CLASSES:
        score += 10
    if uia_class_name in UIA_MAIN_CLASSES | UIA_LOGIN_CLASSES:
        score += 10
    if uia_descendant_count > 0:
        score += min(uia_descendant_count, 20)
    if "QWindowIcon" in class_name:
        score += 5
    return score


def _uia_snapshot_for_handle(handle: int) -> tuple[str, int]:
    try:
        window = _desktop("uia").window(handle=handle)
        class_name = str(_safe_call(window.class_name) or "")
        return class_name, _count_descendants(window, max_depth=6, max_nodes=500)
    except Exception:
        return "", 0


def _window_visibility_snapshot(handle: int) -> dict[str, Any]:
    if platform.system() != "Windows" or not handle:
        return {}
    try:
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        has_rect = bool(user32.GetWindowRect(handle, ctypes.byref(rect)))
        return {
            "visible": bool(user32.IsWindowVisible(handle)),
            "iconic": bool(user32.IsIconic(handle)),
            "rect": (
                int(rect.left) if has_rect else 0,
                int(rect.top) if has_rect else 0,
                int(rect.right) if has_rect else 0,
                int(rect.bottom) if has_rect else 0,
            ),
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


def _safe_call(func: Any) -> Any:
    try:
        return func()
    except Exception:
        return None
