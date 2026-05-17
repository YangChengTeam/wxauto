from __future__ import annotations

import platform
import time
from pathlib import Path
from typing import Any, Callable

from .constants import (
    DEFAULT_UIA_PROBE_MAX_DEPTH,
    DEFAULT_UIA_PROBE_MAX_NODES,
    MIN_UIA_EXPOSED_DESCENDANTS,
)
from .keepalive import (
    ensure_a11y_hook_keepalive,
    ensure_screen_reader_keepalive,
    ensure_uia_keepalive,
    uia_keepalive_snapshot,
)
from .native_com import (
    _native_wechat_window_kind,
    probe_wechat_uia_native,
    try_unlock_with_native_com_client,
)
from .pywinauto_probe import probe_wechat_uia
from .windows import (
    _is_wechat_window_candidate,
    bring_wechat_to_front,
    close_wechat_windows,
    ensure_wechat_foreground,
    start_wechat,
)

__all__ = [
    "bootstrap_wechat_uia",
    "bring_wechat_to_front",
    "ensure_wechat_foreground",
    "ensure_wechat_uia",
    "probe_wechat_uia",
    "probe_wechat_uia_native",
    "try_unlock_with_native_com_client",
    "_is_wechat_window_candidate",
    "_native_wechat_window_kind",
]


def bootstrap_wechat_uia(
    wechat_path: str | Path | None = None,
    *,
    restart_if_needed: bool = False,
    startup_wait: float = 35.0,
    quick_timeout: float = 5.0,
    probe_max_depth: int = DEFAULT_UIA_PROBE_MAX_DEPTH,
    probe_max_nodes: int = DEFAULT_UIA_PROBE_MAX_NODES,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Unlock UIA by trying each mode in a stable, foreground-safe order."""

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    if platform.system() != "Windows":
        return {
            "success": False,
            "mode": "unsupported",
            "probe": _unsupported_probe(),
            "foreground": False,
        }

    _log("先开启屏幕阅读器标志，再连接微信 UIA...")
    ensure_screen_reader_keepalive(log=_log)
    _log("屏幕阅读器标志已设置，启动辅助功能 Hook 后再用 pywinauto 检测...")
    ensure_a11y_hook_keepalive(log=_log)
    wechat_started = False
    try:
        wechat_started = start_wechat(wechat_path)
    except RuntimeError:
        _log("未检测到微信进程，但未找到微信启动路径，继续检测当前窗口状态。")
    if wechat_started:
        _log("未检测到微信进程，已启动微信，等待主窗口出现...")
        time.sleep(2)
    _log("正在通过 pyweixin 检查并激活微信主窗口...")
    if not ensure_wechat_foreground(timeout=8.0 if wechat_started else 2.0):
        _log("pyweixin 未能把微信主窗口切到前台，继续检测 UIA 状态。")

    result = _run_unlock_modes(
        wechat_path=wechat_path,
        restart_if_needed=restart_if_needed,
        startup_wait=startup_wait,
        quick_timeout=quick_timeout,
        probe_max_depth=probe_max_depth,
        probe_max_nodes=probe_max_nodes,
        log=_log,
    )
    _attach_keepalive_snapshot(result)
    return result


def _run_unlock_modes(
    wechat_path: str | Path | None,
    *,
    restart_if_needed: bool,
    startup_wait: float,
    quick_timeout: float,
    probe_max_depth: int,
    probe_max_nodes: int,
    log: Callable[[str], None],
) -> dict[str, Any]:
    _log = log
    _log("先使用 pywinauto 检测微信 UIA...")
    probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
    if probe.get("uia_exposed"):
        foreground = ensure_wechat_foreground(timeout=2.0)
        if foreground:
            _log("pywinauto 检测到 UIA 已可用，跳过 native COM 兜底。")
        else:
            _log("pywinauto 检测到 UIA 已可用；微信当前未在前台，执行动作前会再次尝试切换。")
        return {
            "success": True,
            "mode": "pywinauto",
            "probe": probe,
            "native_probe": {},
            "foreground": foreground,
        }

    _log(f"pywinauto 检测 UIA 不可用：{probe.get('reason') or '未知原因'}，等待 Hook 生效后继续用 pywinauto 复查。")
    if _wait_for_uia_exposed(quick_timeout, probe_max_depth, probe_max_nodes):
        foreground = ensure_wechat_foreground(timeout=2.0)
        probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
        if foreground:
            _log("辅助功能 Hook 生效，pywinauto 检测到 UIA 已解锁。")
        else:
            _log("辅助功能 Hook 生效，pywinauto 检测到 UIA 已解锁；微信当前未在前台，执行动作前会再次尝试切换。")
        return {
            "success": bool(probe.get("uia_exposed")),
            "mode": "hook",
            "probe": probe,
            "native_probe": {},
            "foreground": foreground,
        }

    _log("Hook 后 pywinauto 复查仍不可用，启动 native COM 兜底。")
    ensure_uia_keepalive(log=_log)
    _log("启动原生 COM UIAutomation 客户端，然后检测 UIA...")
    native_result = try_unlock_with_native_com_client(
        timeout=quick_timeout,
        probe_max_depth=probe_max_depth,
        probe_max_nodes=probe_max_nodes,
        log=_log,
    )
    native_probe = native_result.get("native_probe")
    if native_result.get("success"):
        probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
        if probe.get("uia_exposed"):
            foreground = ensure_wechat_foreground(timeout=2.0)
            if not foreground:
                _log("原生 COM UIAutomation 客户端已解锁 UIA；微信当前未在前台，执行动作前会再次尝试切换。")
                return {
                    "success": True,
                    "mode": "native_com",
                    "probe": probe,
                    "native_probe": native_probe,
                    "foreground": False,
                }
            _log("原生 COM UIAutomation 客户端生效，UIA 已解锁。")
            return {
                "success": True,
                "mode": "native_com",
                "probe": probe,
                "native_probe": native_probe,
                "foreground": True,
            }
        _log("原生 COM UIAutomation 客户端探测到短暂可用，复查未稳定，继续兜底...")
    _log("原生 COM UIAutomation 客户端启动后检测仍未解锁，继续等待常驻辅助功能钩子生效...")
    time.sleep(2)

    if _wait_for_uia_exposed(quick_timeout, probe_max_depth, probe_max_nodes):
        foreground = ensure_wechat_foreground(timeout=2.0)
        probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
        if foreground:
            _log("常驻辅助功能钩子生效，UIA 已解锁。")
        else:
            _log("常驻辅助功能钩子已解锁 UIA；微信当前未在前台，执行动作前会再次尝试切换。")
        return {
            "success": bool(probe.get("uia_exposed")),
            "mode": "quick",
            "probe": probe,
            "native_probe": native_probe,
            "foreground": foreground,
        }

    if not restart_if_needed:
        foreground = ensure_wechat_foreground(timeout=2.0)
        probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
        mode = "quick_failed"
        if _is_empty_qt_shell_probe(probe):
            mode = "account_shell_locked"
            _log(
                "检测到微信窗口存在但只暴露 Qt 空壳，常见于切换账号后该账号/数据目录未开放 UIA；"
                "请完全退出微信，再由本程序启动微信后登录该账号。"
            )
        else:
            _log("常驻辅助功能钩子未生效，UIA 仍未解锁。")
        return {
            "success": False,
            "mode": mode,
            "probe": probe,
            "native_probe": native_probe,
            "foreground": foreground,
        }

    _log("常驻辅助功能钩子未生效，关闭微信准备重启...")
    close_wechat_windows()
    time.sleep(2)
    try:
        started = start_wechat(wechat_path)
        if started:
            _log("微信已重新启动，等待 UIA 节点暴露...")
        else:
            _log("检测到微信进程仍在运行，未启动新的微信。")
    except RuntimeError:
        foreground = ensure_wechat_foreground(timeout=2.0)
        probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
        _log("微信启动失败。")
        return {
            "success": False,
            "mode": "winevent_failed",
            "probe": probe,
            "native_probe": native_probe,
            "foreground": foreground,
        }
    ensure_wechat_foreground(timeout=min(8.0, startup_wait))

    exposed = _wait_for_uia_exposed(startup_wait, probe_max_depth, probe_max_nodes)
    time.sleep(2)

    foreground = ensure_wechat_foreground(timeout=2.0)
    probe = probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes)
    exposed = exposed and probe.get("uia_exposed")
    return {
        "success": bool(exposed),
        "mode": _unlock_result_mode(
            success_mode="winevent",
            exposed=bool(exposed),
            foreground=foreground,
            failure_mode="winevent_failed",
        ),
        "probe": probe,
        "native_probe": native_probe,
        "foreground": foreground,
    }


def ensure_wechat_uia(
    restart_if_needed: bool = False,
    wechat_path: str | Path | None = None,
    startup_wait: float = 35.0,
    log: Callable[[str], None] | None = None,
) -> bool:
    def _log(message: str) -> None:
        if log is not None:
            log(message)

    if platform.system() != "Windows":
        _log("动作前 UIA 检查失败：当前平台不是 Windows。")
        return False

    # Business actions call this before every WeChat operation. If the UIA tree is
    # already exposed, do not rerun the heavier unlock flow because native COM may
    # see a LoginWindow-like shell while pywinauto can already operate the tree.
    _log("动作前复查微信 UIA...")
    foreground = ensure_wechat_foreground(timeout=2.0)
    if not foreground:
        _log("动作前未能确认微信在前台，继续尝试检测 UIA。")
    probe = probe_wechat_uia(
        max_depth=DEFAULT_UIA_PROBE_MAX_DEPTH,
        max_nodes=DEFAULT_UIA_PROBE_MAX_NODES,
    )
    if probe.get("uia_exposed"):
        if not foreground:
            _log("动作前 UIA 已可用，但微信未在前台；请先手动打开微信主界面后重试。")
            return False
        _log("动作前 UIA 已可用，继续执行当前微信操作。")
        return True
    _log(f"动作前 UIA 不可用：{probe.get('reason') or '未知原因'}，正在现场解锁。")

    kwargs: dict[str, Any] = {
        "wechat_path": wechat_path,
        "restart_if_needed": restart_if_needed,
        "startup_wait": startup_wait,
    }
    if log is not None:
        kwargs["log"] = log
    result = bootstrap_wechat_uia(**kwargs)
    success = bool(result.get("success"))
    if success and result.get("foreground") is not False:
        _log("现场解锁成功，继续执行当前微信操作。")
    elif success:
        _log("现场 UIA 已解锁，但微信未在前台；请先手动打开微信主界面后重试。")
        success = False
    else:
        probe = result.get("probe") if isinstance(result.get("probe"), dict) else {}
        _log(f"现场解锁失败：{probe.get('reason') or result.get('mode') or '未知原因'}。")
    return success


def _wait_for_uia_exposed(
    timeout: float,
    probe_max_depth: int,
    probe_max_nodes: int,
) -> bool:
    ensure_wechat_foreground(timeout=2.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if probe_wechat_uia(max_depth=probe_max_depth, max_nodes=probe_max_nodes).get(
            "uia_exposed"
        ):
            return True
    return False


def _unlock_result_mode(
    *,
    success_mode: str,
    exposed: bool,
    foreground: bool,
    failure_mode: str,
) -> str:
    if exposed:
        return success_mode
    return failure_mode


def _is_empty_qt_shell_probe(probe: dict[str, Any]) -> bool:
    if probe.get("uia_exposed") or probe.get("login_window") or probe.get("logged_in"):
        return False
    selected = probe.get("selected_window")
    if not isinstance(selected, dict):
        return False
    class_name = str(selected.get("class_name") or "")
    descendant_count = int(selected.get("descendant_count") or 0)
    return (
        selected.get("kind") == "other"
        and class_name.startswith("Qt")
        and "QWindowIcon" in class_name
        and descendant_count < MIN_UIA_EXPOSED_DESCENDANTS
    )


def _attach_keepalive_snapshot(result: dict[str, Any]) -> None:
    keepalive = uia_keepalive_snapshot()
    if keepalive:
        result["keepalive"] = keepalive
        screen_reader = keepalive.get("screen_reader")
        if isinstance(screen_reader, dict):
            result["screen_reader"] = screen_reader


def _unsupported_probe() -> dict[str, Any]:
    return {
        "platform": platform.system(),
        "logged_in": False,
        "login_window": False,
        "uia_exposed": False,
        "reason": "UIA probing is only available on Windows",
    }
