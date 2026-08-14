"""桌面端：托盘常驻 + 日志窗口，包着同一个网关服务。

界面不含任何业务逻辑——它只是 ``wxclient.gateway`` 的一层皮：启动/停止服务、显示日志。
可测的部分（日志缓冲、服务状态机）在 ``logbuffer`` / ``service``，与 GUI 完全分开，
所以能在 Linux 上跑单测；``window`` / ``tray`` 是 Windows 皮，只在真机验证。
"""

from __future__ import annotations

from typing import Any

# ★ 这里**故意不 import 任何东西**。`python -m wxclient.ui` 会先把这个包 __init__ 跑一遍，
# 原来这三行在双击到「看见启动动画」之间横着一道墙：真机实测冷启动 4.7 秒，其中
# `.config` 拉起 pydantic_settings 约 0.4 秒（冷盘上是它的好几倍）、`.service` 拉起
# structlog（连带 rich + asyncio + pydantic.dataclasses）约 0.13 秒。这些东西**建完窗口
# 再要也不迟**，而在它们回来之前屏幕上一片空白——用户看到的就是「点了没反应」。
# PEP 562 的模块级 __getattr__ 让 `from wxclient.ui import GatewayService` 照旧能用，
# 只是推迟到真正取的那一刻。
_LAZY = {
    "UiSettings": ".config",
    "LogBuffer": ".logbuffer",
    "LogBufferHandler": ".logbuffer",
    "attach_to_root": ".logbuffer",
    "STATE_LABELS": ".service",
    "GatewayService": ".service",
    "ServiceState": ".service",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "STATE_LABELS",
    "GatewayService",
    "LogBuffer",
    "LogBufferHandler",
    "ServiceState",
    "UiSettings",
    "attach_to_root",
]
