from __future__ import annotations

import platform
from typing import Any

from .constants import MIN_UIA_EXPOSED_DESCENDANTS, UIA_LOGIN_CLASSES, UIA_MAIN_CLASSES
from .windows import find_wechat_windows


def probe_wechat_uia(max_depth: int = 10, max_nodes: int = 5000) -> dict[str, Any]:
    """Inspect current WeChat UIA exposure without changing system or window state."""
    if platform.system() != "Windows":
        return {
            "platform": platform.system(),
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": "UIA 探测仅支持 Windows",
        }

    try:
        windows = find_wechat_windows(max_depth=max_depth, max_nodes=max_nodes)
    except Exception as exc:
        return {
            "platform": "Windows",
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": f"UIA 探测失败：{exc}",
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
        reason = "UIA 已暴露"
    elif login_windows and not main_windows:
        reason = "微信登录窗口已打开"
    elif classified_windows:
        reason = "微信 UIA 未暴露"
    else:
        reason = "未找到微信窗口"

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
    if class_names & UIA_MAIN_CLASSES:
        item["kind"] = "main"
    elif class_names & UIA_LOGIN_CLASSES:
        item["kind"] = "login"
    else:
        item["kind"] = "other"
    return item
