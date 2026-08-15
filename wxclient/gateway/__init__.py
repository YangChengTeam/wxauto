"""WS 网关：把入站微信消息翻译成一次 AI 对话轮，再把产出翻译回微信消息。

本仓唯一的常驻入口，跑在 ``queue -> wechat_api`` 底座之上。
接口契约见仓库根 ``CLAUDE.md`` §4。
"""

from __future__ import annotations

from .commands import (
    COMMAND_EVENT,
    COMMANDS,
    RESULT_OP,
    CommandError,
    CommandExecutor,
    CommandResult,
)
from .config import GatewaySettings
from .friends import FriendWatcher
from .guard import (
    GuardDecision,
    OutboundGuard,
    RateLimit,
    action_limits_from,
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
from .pacing import clamp_interval, jittered
from .runner import ConnectionPool, GatewayRunner
from .sessions import GatewayStore
from .ticket import (
    ClientTokenTicketProvider,
    LocalFileTicketProvider,
    TicketError,
    TicketProvider,
)
from .pacing import PACING_EVENT
from .watchlist import PUSH_EVENT, Watchlist, WatchScope
from .wechat_io import (
    QueueMessageSink,
    QueueMessageSource,
    is_self_message,
    is_timestamp_row,
    message_signature,
    split_new_messages,
    to_inbound,
)
from .wsclient import (
    AgentWsClient,
    AgentWsError,
    TurnResult,
    TurnTimeout,
    Unauthorized,
)

__all__ = [
    "COMMANDS",
    "COMMAND_EVENT",
    "PACING_EVENT",
    "PUSH_EVENT",
    "RESULT_OP",
    "AgentWsClient",
    "AgentWsError",
    "ClientTokenTicketProvider",
    "CommandError",
    "CommandExecutor",
    "CommandResult",
    "ConnectionPool",
    "Debouncer",
    "DeliveryResult",
    "FriendWatcher",
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
    "RateLimit",
    "TicketError",
    "TicketProvider",
    "TurnResult",
    "TurnTimeout",
    "Unauthorized",
    "WatchScope",
    "Watchlist",
    "action_limits_from",
    "clamp_interval",
    "extract_reply",
    "format_transcript",
    "is_self_message",
    "is_timestamp_row",
    "jittered",
    "looks_like_rejection",
    "message_signature",
    "parse_active_hours",
    "split_new_messages",
    "to_inbound",
    "within_active_hours",
]
