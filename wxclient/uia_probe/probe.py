from __future__ import annotations

import platform
from typing import Any

import structlog

from .constants import MIN_UIA_EXPOSED_DESCENDANTS, UIA_LOGIN_CLASSES, UIA_MAIN_CLASSES
from .windows import find_wechat_windows

_log = structlog.get_logger(__name__)

#: 状态格放得下多少字。它和「已连接」「3 个会话」那几行并排，长了会把整栏撑歪。
#: ★ 所有 `reason` 都**照这个长度写**，而不是写长了再截——
#: 2026-08-16 真机上界面那格显示的是「UIA 探测失败：Z:\」：异常原文是一条路径，
#: 被截到 12 个字符就只剩一个盘符。对人零信息，还把安装路径漏了出去。
STATUS_CELL_BUDGET = 12

#: 探测炸了时给人看的那一句。**不许把异常原文摆上去**：它可能是一条路径、一个 COM
#: HRESULT、一段英文堆栈，三种都不是坐在这台电脑前的人能拿来做点什么的东西。
#: 怎么恢复由上报层那份失败码表告诉企业端（契约 §8 的 `failure.message`），
#: 真正的原因（异常类型 + 原文）进日志——那儿才是排障该去的地方。
PROBE_BROKEN_REASON = "微信读取异常"


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
        # 异常原文进日志、不进界面：它可能是一条路径、一个 COM HRESULT、一段英文堆栈，
        # 三种都不是坐在这台电脑前的人能拿来做点什么的东西。
        _log.warning("微信 UIA 探测异常", error_type=type(exc).__name__,
                     error=str(exc)[:300])
        return {
            "platform": "Windows",
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": PROBE_BROKEN_REASON,
            # 给上报层和诊断用；**界面不读这个键**。
            "error_detail": f"{type(exc).__name__}: {exc}"[:300],
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
