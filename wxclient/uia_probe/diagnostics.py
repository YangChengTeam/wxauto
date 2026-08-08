from __future__ import annotations

import ctypes
import getpass
import platform
from ctypes import wintypes
from typing import Any

from .constants import WECHAT_PROCESS_NAMES
from .windows import foreground_window_handle, process_info, safe_wechat_window_candidates


def format_uia_diagnostics(
    probe: dict[str, Any] | None = None,
    *,
    context: str = "UIA 诊断",
) -> list[str]:
    """Format read-only UIA and window snapshots for GUI logs."""
    probe = probe or {}
    lines = [f"{context}：平台={platform.system()}，用户={_safe_username()}，管理员={_admin_label()}"]
    lines.append(_format_foreground_line(context))
    lines.extend(_format_wechat_window_lines(context))
    lines.extend(_format_probe_lines(context, probe))
    return lines


def _format_foreground_line(context: str) -> str:
    handle = foreground_window_handle()
    snapshot = _window_snapshot(handle)
    process_name = str(snapshot.get("process_name") or "").lower()
    return (
        f"{context}：前台窗口 handle={handle or '-'}，"
        f"title={snapshot.get('title') or '-'}，class={snapshot.get('class_name') or '-'}，"
        f"pid={snapshot.get('process_id') or '-'}，process={snapshot.get('process_name') or '-'}，"
        f"is_wechat={_yes_no(process_name in WECHAT_PROCESS_NAMES)}"
    )


def _format_wechat_window_lines(context: str) -> list[str]:
    candidates = safe_wechat_window_candidates()
    if not candidates:
        return [f"{context}：微信候选窗口=0 个"]
    lines = [f"{context}：微信候选窗口={len(candidates)} 个"]
    for index, item in enumerate(candidates[:5], start=1):
        lines.append(
            f"{context}：微信窗口#{index} handle={item.get('handle') or '-'}，"
            f"title={item.get('title') or '-'}，class={item.get('class_name') or '-'}，"
            f"uia_class={item.get('uia_class_name') or '-'}，"
            f"uia_nodes={item.get('uia_descendant_count', '-')}，"
            f"pid={item.get('process_id') or '-'}，process={item.get('process_name') or '-'}"
        )
    return lines


def _format_probe_lines(context: str, probe: dict[str, Any]) -> list[str]:
    if not probe:
        return [f"{context}：probe=无"]
    lines = [
        f"{context}：probe logged_in={_yes_no(bool(probe.get('logged_in')))}，"
        f"login_window={_yes_no(bool(probe.get('login_window')))}，"
        f"uia_exposed={_yes_no(bool(probe.get('uia_exposed')))}，"
        f"window_count={probe.get('window_count', '-')}，reason={probe.get('reason') or '-'}"
    ]
    selected = probe.get("selected_window")
    if isinstance(selected, dict):
        lines.append(
            f"{context}：selected handle={selected.get('handle') or '-'}，"
            f"kind={selected.get('kind') or '-'}，class={selected.get('class_name') or '-'}，"
            f"descendants={selected.get('descendant_count', '-')}"
        )
    return lines


def _window_snapshot(handle: int) -> dict[str, Any]:
    if platform.system() != "Windows" or not handle:
        return {}
    try:
        user32 = ctypes.windll.user32
        title_buffer = ctypes.create_unicode_buffer(512)
        class_buffer = ctypes.create_unicode_buffer(256)
        process_id = wintypes.DWORD()
        user32.GetWindowTextW(handle, title_buffer, len(title_buffer))
        user32.GetClassNameW(handle, class_buffer, len(class_buffer))
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        info = process_info(int(process_id.value))
        return {
            "title": title_buffer.value,
            "class_name": class_buffer.value,
            "process_id": int(process_id.value),
            "process_name": info.get("name"),
        }
    except Exception:
        return {}


def _safe_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "-"


def _admin_label() -> str:
    if platform.system() != "Windows":
        return "不适用"
    try:
        return _yes_no(bool(ctypes.windll.shell32.IsUserAnAdmin()))
    except Exception:
        return "未知"


def _yes_no(value: bool) -> str:
    return "是" if value else "否"
