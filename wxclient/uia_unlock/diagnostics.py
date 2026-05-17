from __future__ import annotations

import ctypes
import getpass
import platform
from ctypes import wintypes
from typing import Any

from .constants import WECHAT_PROCESS_NAMES
from .native_com import uia_dotnet_assembly_snapshot
from .screen_reader import get_screen_reader_active
from .windows import (
    _foreground_window_handle,
    _process_info,
    _safe_wechat_window_candidates,
)


def format_uia_diagnostics(
    result: dict[str, Any] | None = None,
    *,
    context: str = "UIA 诊断",
) -> list[str]:
    """Build concise GUI-log lines for comparing UIA environments."""
    result = result or {}
    lines = [
        (
            f"{context}：平台={platform.system()}，"
            f"用户={_safe_username()}，管理员={_admin_label()}"
        )
    ]
    lines.append(f"{context}：屏幕阅读器标志={_optional_bool_label(get_screen_reader_active())}")
    lines.append(_format_dotnet_uia_assemblies_line(context))
    screen_reader = _dict_value(result, "screen_reader")
    if screen_reader:
        lines.append(
            (
                f"{context}：屏幕阅读器会话 "
                f"previous={_optional_bool_label(_optional_bool(screen_reader.get('previous')))}，"
                f"enabled={_yes_no(bool(screen_reader.get('enabled')))}，"
                f"restored={_yes_no(bool(screen_reader.get('restored')))}，"
                f"current={_optional_bool_label(_optional_bool(screen_reader.get('current')))}"
            )
        )
    keepalive = _dict_value(result, "keepalive")
    if keepalive:
        native_com = _dict_value(keepalive, "native_com")
        a11y_hook = _dict_value(keepalive, "a11y_hook")
        lines.append(
            (
                f"{context}：常驻会话 started={_yes_no(bool(keepalive.get('started')))}，"
                f"native_com_alive={_yes_no(bool(native_com.get('alive')))}，"
                f"a11y_hook_alive={_yes_no(bool(a11y_hook.get('alive')))}，"
                f"a11y_thread={a11y_hook.get('thread_id') or '-'}"
            )
        )
    lines.append(_format_foreground_line(context))
    lines.extend(_format_wechat_window_lines(context))
    lines.extend(_format_probe_lines(context, "pywinauto", _dict_value(result, "probe")))
    native_probe = _dict_value(result, "native_probe")
    if native_probe:
        lines.extend(_format_probe_lines(context, "native COM", native_probe))
    else:
        lines.append(_format_skipped_native_probe_line(context, str(result.get("mode") or "")))
    return lines


def _format_dotnet_uia_assemblies_line(context: str) -> str:
    snapshot = uia_dotnet_assembly_snapshot()
    loaded = snapshot.get("loaded") if isinstance(snapshot.get("loaded"), list) else []
    missing = snapshot.get("missing") if isinstance(snapshot.get("missing"), list) else []
    errors = snapshot.get("errors") if isinstance(snapshot.get("errors"), list) else []
    return (
        f"{context}：.NET UIAutomation 程序集 "
        f"attempted={_yes_no(bool(snapshot.get('attempted')))}，"
        f"loaded={len(loaded)}，missing={len(missing)}，errors={len(errors)}"
    )


def _format_foreground_line(context: str) -> str:
    handle = _foreground_window_handle()
    snapshot = _window_snapshot(handle)
    is_wechat = _handle_belongs_to_wechat(handle)
    return (
        f"{context}：前台窗口 handle={handle or '-'}，"
        f"title={snapshot.get('title') or '-'}，"
        f"class={snapshot.get('class_name') or '-'}，"
        f"pid={snapshot.get('process_id') or '-'}，"
        f"process={snapshot.get('process_name') or '-'}，"
        f"is_wechat={_yes_no(is_wechat)}"
    )


def _format_wechat_window_lines(context: str) -> list[str]:
    candidates = _safe_wechat_window_candidates()
    if not candidates:
        return [f"{context}：微信候选窗口=0 个"]

    lines = [f"{context}：微信候选窗口={len(candidates)} 个"]
    for index, candidate in enumerate(candidates[:5], start=1):
        lines.append(
            (
                f"{context}：微信窗口#{index} handle={candidate.get('handle') or '-'}，"
                f"title={candidate.get('title') or '-'}，"
                f"class={candidate.get('class_name') or '-'}，"
                f"uia_class={candidate.get('uia_class_name') or '-'}，"
                f"uia_nodes={candidate.get('uia_descendant_count', '-')}，"
                f"visible={_optional_bool_label(_optional_bool(candidate.get('is_visible')))}，"
                f"minimized={_optional_bool_label(_optional_bool(candidate.get('is_iconic')))}，"
                f"rect={candidate.get('rect') or '-'}，"
                f"pid={candidate.get('process_id') or '-'}，"
                f"process={candidate.get('process_name') or '-'}，"
                f"score={candidate.get('score') or 0}，"
                f"path={candidate.get('process_path') or '-'}"
            )
        )
    return lines


def _format_probe_lines(
    context: str,
    provider: str,
    probe: dict[str, Any],
) -> list[str]:
    if not probe:
        return [f"{context}：{provider} probe=无"]

    lines = [
        (
            f"{context}：{provider} probe "
            f"logged_in={_yes_no(bool(probe.get('logged_in')))}，"
            f"login_window={_yes_no(bool(probe.get('login_window')))}，"
            f"uia_exposed={_yes_no(bool(probe.get('uia_exposed')))}，"
            f"window_count={probe.get('window_count', '-')}，"
            f"reason={probe.get('reason') or '-'}"
        )
    ]
    selected = _dict_value(probe, "selected_window")
    if selected:
        lines.append(
            (
                f"{context}：{provider} selected "
                f"handle={selected.get('handle') or '-'}，"
                f"kind={selected.get('kind') or '-'}，"
                f"class={selected.get('class_name') or '-'}，"
                f"descendants={selected.get('descendant_count', '-')}，"
                f"pid={selected.get('process_id') or '-'}，"
                f"process={selected.get('process_name') or '-'}"
            )
        )
    return lines


def _format_skipped_native_probe_line(context: str, mode: str) -> str:
    if mode == "pywinauto":
        reason = "pywinauto 已可用，未进入 native 兜底"
    elif mode == "foreground_failed":
        reason = "pywinauto 已检测到 UIA，但微信未能切到前台"
    else:
        reason = "尚未进入 native 兜底"
    return f"{context}：native COM probe=未执行（{reason}）"


def _window_snapshot(handle: int) -> dict[str, Any]:
    if platform.system() != "Windows" or not handle:
        return {}
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        title_length = max(user32.GetWindowTextLengthW(handle), 0) + 1
        title_buffer = ctypes.create_unicode_buffer(title_length)
        user32.GetWindowTextW(handle, title_buffer, title_length)

        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, class_buffer, 256)

        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
        process_info = _process_info(int(process_id.value))
        return {
            "title": title_buffer.value,
            "class_name": class_buffer.value,
            "process_id": int(process_id.value),
            "process_name": process_info.get("name"),
            "process_path": process_info.get("path"),
        }
    except Exception:
        return {}


def _handle_belongs_to_wechat(handle: int) -> bool:
    if not handle:
        return False
    for candidate in _safe_wechat_window_candidates():
        if candidate.get("handle") == handle:
            return True
    snapshot = _window_snapshot(handle)
    process_name = str(snapshot.get("process_name") or "").lower()
    return process_name in WECHAT_PROCESS_NAMES


def _admin_label() -> str:
    if platform.system() != "Windows":
        return "未知"
    try:
        return _yes_no(bool(ctypes.windll.shell32.IsUserAnAdmin()))
    except Exception:
        return "未知"


def _safe_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "未知"


def _dict_value(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _optional_bool_label(value: bool | None) -> str:
    if value is None:
        return "未知"
    return _yes_no(value)


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None
