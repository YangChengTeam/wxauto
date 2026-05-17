from __future__ import annotations

import logging
import os
import platform
import threading
import time
from pathlib import Path
from typing import Any
import ctypes

from .constants import (
    DEFAULT_UIA_PROBE_MAX_DEPTH,
    DEFAULT_UIA_PROBE_MAX_NODES,
    MIN_UIA_EXPOSED_DESCENDANTS,
    NATIVE_UIA_CLIENT_CLSID,
    NATIVE_UIA_LOGIN_CLASSES,
    NATIVE_UIA_MAIN_CLASSES,
    NATIVE_UIA_WINDOW_CLASSES,
    WECHAT_PROCESS_NAMES,
)
from .pywinauto_probe import probe_wechat_uia
from .windows import _process_info, bring_wechat_to_front

_UIA_DOTNET_ASSEMBLY_NAMES = ("UIAutomationTypes.dll", "UIAutomationClient.dll")
_UIA_DOTNET_LOCK = threading.Lock()
_UIA_DOTNET_HANDLES: list[Any] = []
_UIA_DOTNET_STATE: dict[str, Any] = {
    "attempted": False,
    "loaded": [],
    "missing": [],
    "errors": [],
}


def probe_wechat_uia_native(max_depth: int = 10, max_nodes: int = 5000) -> dict[str, Any]:
    """Read-only probe through native COM UIAutomation, bypassing pywinauto."""
    if platform.system() != "Windows":
        return {
            "platform": platform.system(),
            "provider": "native_com",
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": "UIA probing is only available on Windows",
        }

    try:
        uia = _create_native_uia_client()
        return _probe_wechat_uia_native_with_client(
            uia,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    except Exception as exc:
        return {
            "platform": "Windows",
            "provider": "native_com",
            "logged_in": False,
            "login_window": False,
            "uia_exposed": False,
            "reason": f"native COM probe failed: {exc}",
        }


def try_unlock_with_native_com_client(
    timeout: float = 5.0,
    *,
    probe_max_depth: int = DEFAULT_UIA_PROBE_MAX_DEPTH,
    probe_max_nodes: int = DEFAULT_UIA_PROBE_MAX_NODES,
    log: Any | None = None,
) -> dict[str, Any]:
    """Keep a native UIAutomation client alive briefly to trigger WeChat UIA exposure."""
    if platform.system() != "Windows":
        return {
            "success": False,
            "reason": "UIA unlocking is only available on Windows",
            "native_probe": probe_wechat_uia_native(
                max_depth=probe_max_depth,
                max_nodes=probe_max_nodes,
            ),
        }

    try:
        uia = _create_native_uia_client(log=log)
    except Exception as exc:
        return {
            "success": False,
            "reason": f"native COM client unavailable: {exc}",
            "native_probe": {},
        }

    bring_wechat_to_front(timeout=2.0)
    deadline = time.monotonic() + max(timeout, 0.0)
    native_probe: dict[str, Any] = {}
    standard_probe: dict[str, Any] = {}

    while True:
        native_probe = _probe_wechat_uia_native_with_client(
            uia,
            max_depth=probe_max_depth,
            max_nodes=probe_max_nodes,
        )
        standard_probe = probe_wechat_uia(
            max_depth=probe_max_depth,
            max_nodes=probe_max_nodes,
        )
        if standard_probe.get("uia_exposed"):
            return {
                "success": True,
                "reason": "UIA exposed",
                "probe": standard_probe,
                "native_probe": native_probe,
            }
        if time.monotonic() >= deadline:
            break
        time.sleep(1)

    return {
        "success": False,
        "reason": "native COM client did not expose pywinauto UIA tree",
        "probe": standard_probe,
        "native_probe": native_probe,
    }


class NativeComClientSession:
    """Keep a native UIAutomation client alive for the lifetime of the app."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Any | None = None
        self.error: str | None = None
        self.started = False

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self.started)

    def start(self, timeout: float = 3.0, log: Any | None = None) -> str:
        if platform.system() != "Windows":
            self.error = "native COM UIAutomation is only available on Windows"
            return "原生 COM UIAutomation 常驻客户端仅支持 Windows。"
        if self.alive:
            return "原生 COM UIAutomation 常驻客户端已在运行。"
        self._stop.clear()
        self._ready.clear()
        self.error = None
        self._log = log
        self.started = False
        self._thread = threading.Thread(
            target=self._run,
            name="wx-native-uia-client",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=timeout)
        if self.alive:
            return "原生 COM UIAutomation 常驻客户端已启动。"
        if self.error:
            return f"原生 COM UIAutomation 常驻客户端启动失败：{self.error}"
        return "原生 COM UIAutomation 常驻客户端启动超时。"

    def stop(self, timeout: float = 3.0) -> str | None:
        if self._thread is None:
            return None
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            return "原生 COM UIAutomation 常驻客户端停止超时。"
        self.started = False
        self._thread = None
        return "原生 COM UIAutomation 常驻客户端已停止。"

    def snapshot(self) -> dict[str, Any]:
        return {
            "alive": self.alive,
            "started": self.started,
            "error": self.error,
        }

    def _run(self) -> None:
        initialized = False
        try:
            ctypes.windll.ole32.CoInitialize(None)
            initialized = True
            self._client = _create_native_uia_client(log=self._log)
            self.started = True
            self._ready.set()
            while not self._stop.wait(0.5):
                pass
        except Exception as exc:
            self.error = str(exc)
            self._ready.set()
        finally:
            self._client = None
            self.started = False
            if initialized:
                ctypes.windll.ole32.CoUninitialize()


def _probe_wechat_uia_native_with_client(
    uia: Any,
    *,
    max_depth: int,
    max_nodes: int,
) -> dict[str, Any]:
    windows = _find_wechat_windows_native(
        uia,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    selected = windows[0] if windows else None
    main_windows = [item for item in windows if item.get("kind") == "main"]
    login_windows = [item for item in windows if item.get("kind") == "login"]
    exposed = bool(
        selected
        and selected.get("kind") == "main"
        and int(selected.get("descendant_count") or 0) >= MIN_UIA_EXPOSED_DESCENDANTS
    )
    if exposed:
        reason = "UIA exposed"
    elif login_windows and not main_windows:
        reason = "WeChat login window is open"
    elif windows:
        reason = "WeChat UIA is not exposed"
    else:
        reason = "WeChat window not found through native COM"

    return {
        "platform": "Windows",
        "provider": "native_com",
        "logged_in": bool(main_windows),
        "login_window": bool(login_windows),
        "uia_exposed": exposed,
        "window_count": len(windows),
        "selected_window": selected,
        "windows": windows,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "reason": reason,
    }


def _create_native_uia_client(log: Any | None = None) -> Any:
    load_uia_dotnet_client_assemblies(log=log)
    previous_disable_level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
    finally:
        logging.disable(previous_disable_level)
    from comtypes.gen.UIAutomationClient import IUIAutomation

    return comtypes.client.CreateObject(
        NATIVE_UIA_CLIENT_CLSID,
        interface=IUIAutomation,
    )


def load_uia_dotnet_client_assemblies(log: Any | None = None) -> dict[str, Any]:
    """Load .NET UIAutomation client assemblies into this Python process."""
    if platform.system() != "Windows":
        return _uia_dotnet_assembly_snapshot()

    with _UIA_DOTNET_LOCK:
        if _UIA_DOTNET_STATE["attempted"]:
            snapshot = _uia_dotnet_assembly_snapshot()
            _log_dotnet_uia_assembly_snapshot(log, snapshot, already_loaded=True)
            return snapshot

        _UIA_DOTNET_STATE["attempted"] = True
        for name, path in _uia_dotnet_assembly_paths().items():
            if path is None:
                _UIA_DOTNET_STATE["missing"].append(name)
                continue
            try:
                _UIA_DOTNET_HANDLES.append(ctypes.WinDLL(str(path)))
                _UIA_DOTNET_STATE["loaded"].append(str(path))
            except Exception as exc:
                _UIA_DOTNET_STATE["errors"].append(f"{name}: {exc}")
        snapshot = _uia_dotnet_assembly_snapshot()
        _log_dotnet_uia_assembly_snapshot(log, snapshot, already_loaded=False)
        return snapshot


def uia_dotnet_assembly_snapshot() -> dict[str, Any]:
    return _uia_dotnet_assembly_snapshot()


def _uia_dotnet_assembly_snapshot() -> dict[str, Any]:
    return {
        "attempted": bool(_UIA_DOTNET_STATE["attempted"]),
        "loaded": list(_UIA_DOTNET_STATE["loaded"]),
        "missing": list(_UIA_DOTNET_STATE["missing"]),
        "errors": list(_UIA_DOTNET_STATE["errors"]),
    }


def _uia_dotnet_assembly_paths() -> dict[str, Path | None]:
    windir = Path(os.environ.get("WINDIR") or r"C:\Windows")
    preferred = "Framework64" if ctypes.sizeof(ctypes.c_void_p) == 8 else "Framework"
    frameworks = [preferred, "Framework" if preferred == "Framework64" else "Framework64"]
    roots = [windir / "Microsoft.NET" / framework / "v4.0.30319" / "WPF" for framework in frameworks]

    resolved: dict[str, Path | None] = {}
    for name in _UIA_DOTNET_ASSEMBLY_NAMES:
        resolved[name] = next((root / name for root in roots if (root / name).exists()), None)
    return resolved


def _log_dotnet_uia_assembly_snapshot(
    log: Any | None,
    snapshot: dict[str, Any],
    *,
    already_loaded: bool,
) -> None:
    if log is None:
        return
    loaded = snapshot.get("loaded") if isinstance(snapshot.get("loaded"), list) else []
    missing = snapshot.get("missing") if isinstance(snapshot.get("missing"), list) else []
    errors = snapshot.get("errors") if isinstance(snapshot.get("errors"), list) else []
    prefix = ".NET UIAutomation 程序集已加载" if already_loaded else ".NET UIAutomation 程序集加载完成"
    try:
        log(f"{prefix}：loaded={len(loaded)}，missing={len(missing)}，errors={len(errors)}。")
        for path in loaded:
            log(f".NET UIAutomation 已加载：{path}")
        for name in missing:
            log(f".NET UIAutomation 未找到：{name}")
        for error in errors:
            log(f".NET UIAutomation 加载失败：{error}")
    except Exception:
        return


def _find_wechat_windows_native(
    uia: Any,
    *,
    max_depth: int,
    max_nodes: int,
) -> list[dict[str, Any]]:
    from comtypes.gen.UIAutomationClient import (
        TreeScope_Children,
        TreeScope_Subtree,
        UIA_ClassNamePropertyId,
        UIA_ControlTypePropertyId,
        UIA_NamePropertyId,
        UIA_NativeWindowHandlePropertyId,
        UIA_ProcessIdPropertyId,
    )

    root = uia.GetRootElement()
    true_condition = uia.CreateTrueCondition()
    seen_handles: set[int] = set()
    items: list[dict[str, Any]] = []

    for scope in (TreeScope_Children, TreeScope_Subtree):
        for class_name in NATIVE_UIA_WINDOW_CLASSES:
            condition = uia.CreatePropertyCondition(UIA_ClassNamePropertyId, class_name)
            collection = root.FindAll(scope, condition)
            for index in range(collection.Length):
                element = collection.GetElement(index)
                handle = _native_int_property(
                    element,
                    UIA_NativeWindowHandlePropertyId,
                )
                if handle and handle in seen_handles:
                    continue
                process_id = _native_int_property(element, UIA_ProcessIdPropertyId)
                process_info = _process_info(process_id)
                process_name = str(process_info.get("name") or "").strip()
                if process_name and process_name.lower() not in WECHAT_PROCESS_NAMES:
                    continue
                current_class = str(
                    _native_property(element, UIA_ClassNamePropertyId) or class_name
                )
                kind = _native_wechat_window_kind(current_class)
                if kind == "other":
                    continue
                if handle:
                    seen_handles.add(handle)
                descendants = _count_native_descendants(
                    element,
                    uia,
                    true_condition,
                    max_depth=max_depth,
                    max_nodes=max_nodes,
                )
                items.append(
                    {
                        "handle": handle,
                        "process_id": process_id,
                        "process_name": process_name,
                        "process_path": process_info.get("path"),
                        "class_name": current_class,
                        "title": str(_native_property(element, UIA_NamePropertyId) or ""),
                        "control_type": _native_int_property(
                            element,
                            UIA_ControlTypePropertyId,
                        ),
                        "kind": kind,
                        "descendant_count": descendants,
                        "candidate_score": _native_wechat_candidate_score(current_class),
                    }
                )
        if items:
            break

    items.sort(
        key=lambda item: (
            0 if item["kind"] == "main" else 1,
            -int(item["descendant_count"]),
            -int(item["candidate_score"]),
        )
    )
    return items


def _count_native_descendants(
    element: Any,
    uia: Any,
    true_condition: Any,
    *,
    max_depth: int,
    max_nodes: int,
) -> int:
    from comtypes.gen.UIAutomationClient import TreeScope_Children

    count = 0
    queue: list[tuple[Any, int]] = [(element, 0)]
    while queue and count < max_nodes:
        node, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = node.FindAll(TreeScope_Children, true_condition)
        except Exception:
            continue
        child_count = int(children.Length)
        count += child_count
        if count >= max_nodes:
            return max_nodes
        queue.extend(
            (children.GetElement(index), depth + 1) for index in range(child_count)
        )
    return count


def _native_property(element: Any, property_id: int) -> Any:
    try:
        return element.GetCurrentPropertyValue(property_id)
    except Exception:
        return None


def _native_int_property(element: Any, property_id: int) -> int:
    value = _native_property(element, property_id)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _native_wechat_window_kind(class_name: str) -> str:
    if class_name in NATIVE_UIA_MAIN_CLASSES:
        return "main"
    if class_name in NATIVE_UIA_LOGIN_CLASSES:
        return "login"
    return "other"


def _native_wechat_candidate_score(class_name: str) -> int:
    if class_name == "mmui::MainWindow":
        return 30
    if class_name == "WeChatMainWndForPC":
        return 20
    if class_name in NATIVE_UIA_LOGIN_CLASSES:
        return 10
    return 0
