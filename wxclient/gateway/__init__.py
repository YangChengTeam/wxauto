"""WS 网关：把入站微信消息翻译成一次 AI 对话轮，再把产出翻译回微信消息。

本仓唯一的常驻入口，跑在 ``queue -> wechat_api`` 底座之上。
接口契约见仓库根 ``CLAUDE.md`` §4。
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
from .runner import ConnectionPool, GatewayRunner
from .sessions import GatewayStore
from .wechat_io import (
    QueueMessageSink,
    QueueMessageSource,
    message_signature,
    split_new_messages,
    to_inbound,
)
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
    "ConnectionPool",
    "Debouncer",
    "DeliveryResult",
    "GatewayRunner",
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
    "QueueMessageSink",
    "QueueMessageSource",
    "TicketError",
    "TicketProvider",
    "TurnResult",
    "TurnTimeout",
    "Unauthorized",
    "extract_reply",
    "format_transcript",
    "looks_like_rejection",
    "message_signature",
    "parse_active_hours",
    "split_new_messages",
    "to_inbound",
    "within_active_hours",
]
