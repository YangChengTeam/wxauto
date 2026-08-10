"""WS 网关：把入站微信消息翻译成一次 AI 对话轮，再把产出翻译回微信消息。

与被动的 ``wxclient.mcp_server`` 并列，是本仓第二个入口，共用同一套
``queue -> wechat_api`` 底座。接口契约见仓库根 ``CLAUDE.md`` §4。
"""

from __future__ import annotations

from .config import GatewaySettings
from .guard import (
    GuardDecision,
    OutboundGuard,
    looks_like_rejection,
    parse_active_hours,
    within_active_hours,
)
from .inbound import (
    Debouncer,
    InboundLoop,
    InboundMessage,
    MessageSource,
    format_transcript,
)
from .outbound import DeliveryResult, MessageSink, OutboundSender, extract_reply
from .sessions import GatewayStore
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
    "Debouncer",
    "DeliveryResult",
    "GatewaySettings",
    "GatewayStore",
    "GuardDecision",
    "InboundLoop",
    "InboundMessage",
    "LocalFileTicketProvider",
    "MessageSink",
    "MessageSource",
    "OutboundGuard",
    "OutboundSender",
    "TicketError",
    "TicketProvider",
    "TurnResult",
    "TurnTimeout",
    "Unauthorized",
    "extract_reply",
    "format_transcript",
    "looks_like_rejection",
    "parse_active_hours",
    "within_active_hours",
]
