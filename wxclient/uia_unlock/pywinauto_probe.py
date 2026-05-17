from __future__ import annotations

import platform
from typing import Any

from .constants import (
    MIN_UIA_EXPOSED_DESCENDANTS,
    NATIVE_UIA_LOGIN_CLASSES,
    NATIVE_UIA_MAIN_CLASSES,
)
from .windows import find_wechat_windows


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
        windows = find_wechat_windows(max_depth=max_depth, max_nodes=max_nodes)
    except Exception as exc:
        return {
            "platform": "Windows",
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": f"probe failed: {exc}",
        }

    classified_windows = [_classify_wechat_window(window) for window in windows]
    selected = classified_windows[0] if classified_windows else None
    main_windows = [item for item in classified_windows if item.get("kind") == "main"]
    login_windows = [item for item in classified_windows if item.get("kind") == "login"]
    exposed = bool(
        selected
        and selected.get("kind") == "main"
        and int(selected.get("descendant_count") or 0) >= MIN_UIA_EXPOSED_DESCENDANTS
    )
    if exposed:
        reason = "UIA exposed"
    elif login_windows and not main_windows:
        reason = "WeChat login window is open"
    elif classified_windows:
        reason = "WeChat UIA is not exposed"
    else:
        reason = "WeChat window not found"

    return {
        "platform": "Windows",
        "logged_in": bool(main_windows),
        "login_window": bool(login_windows),
        "uia_exposed": exposed,
        "window_count": len(classified_windows),
        "selected_window": selected,
        "windows": classified_windows,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "reason": reason,
    }


def _classify_wechat_window(window: dict[str, Any]) -> dict[str, Any]:
    item = dict(window)
    class_names = {
        str(item.get("uia_class_name") or ""),
        str(item.get("class_name") or ""),
    }
    if class_names & NATIVE_UIA_MAIN_CLASSES:
        item["kind"] = "main"
    elif class_names & NATIVE_UIA_LOGIN_CLASSES:
        item["kind"] = "login"
    else:
        item["kind"] = "other"
    return item
