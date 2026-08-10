"""WS 网关：把入站微信消息翻译成一次 AI 对话轮，再把产出翻译回微信消息。

与被动的 ``wxclient.mcp_server`` 并列，是本仓第二个入口，共用同一套
``queue -> wechat_api`` 底座。接口契约见仓库根 ``CLAUDE.md`` §4。
"""

from __future__ import annotations

from .config import GatewaySettings
from .ticket import (
    BusinessTicketProvider,
    LocalFileTicketProvider,
    TicketError,
    TicketProvider,
)
from .wsclient import (
    AgentWsClient,
    AgentWsError,
    TurnResult,
    TurnTimeout,
    Unauthorized,
)

__all__ = [
    "AgentWsClient",
    "AgentWsError",
    "BusinessTicketProvider",
    "GatewaySettings",
    "LocalFileTicketProvider",
    "TicketError",
    "TicketProvider",
    "TurnResult",
    "TurnTimeout",
    "Unauthorized",
]
