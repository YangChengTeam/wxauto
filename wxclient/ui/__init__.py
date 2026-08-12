"""桌面端：托盘常驻 + 日志窗口，包着同一个网关服务。

界面不含任何业务逻辑——它只是 ``wxclient.gateway`` 的一层皮：启动/停止服务、显示日志。
可测的部分（日志缓冲、服务状态机）在 ``logbuffer`` / ``service``，与 GUI 完全分开，
所以能在 Linux 上跑单测；``window`` / ``tray`` 是 Windows 皮，只在真机验证。
"""

from __future__ import annotations

from .config import UiSettings
from .logbuffer import LogBuffer, LogBufferHandler, attach_to_root
from .service import STATE_LABELS, GatewayService, ServiceState

__all__ = [
    "STATE_LABELS",
    "GatewayService",
    "LogBuffer",
    "LogBufferHandler",
    "ServiceState",
    "UiSettings",
    "attach_to_root",
]
