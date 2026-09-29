"""WS 网关：把入站微信消息翻译成一次 AI 对话轮，再把产出翻译回微信消息。

本仓唯一的常驻入口，跑在 ``queue -> wechat_api`` 底座之上。
接口契约见仓库根 ``CLAUDE.md`` §4。
"""

from __future__ import annotations

from .commands import (
    COMMAND_EVENT,
    COMMANDS,
    NOTICE_EVENT,
    RESULT_OP,
    CommandError,
    CommandExecutor,
    CommandResult,
)
from .config import GatewaySettings
from .contacts import ContactDirectory
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
from .message_events import (
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    MessageEventError,
    MessageEventReporter,
    build_event,
    content_digest,
    event_id_for,
    local_message_id,
)
from .outbound import DeliveryResult, MessageSink, OutboundSender, extract_reply
from .pacing import PACING_EVENT, clamp_interval, jittered
from .runner import ConnectionPool, GatewayRunner
from .sessions import GatewayStore
from .ticket import (
    ClientTokenTicketProvider,
    LocalFileTicketProvider,
    TicketError,
    TicketProvider,
)
from .watchlist import PUSH_EVENT, Watchlist, WatchScope
from .wechat_io import (
    QueueMessageSink,
    QueueMessageSource,
    SendNotConfirmed,
    is_self_message,
    is_timestamp_row,
    latest_cursor,
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
    "DIRECTION_INBOUND",
    "DIRECTION_OUTBOUND",
    "NOTICE_EVENT",
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
    "ContactDirectory",
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
    "MessageEventError",
    "MessageEventReporter",
    "MessageSink",
    "MessageSource",
    "OutboundGuard",
    "OutboundSender",
    "QueueMessageSink",
    "QueueMessageSource",
    "RateLimit",
    "SendNotConfirmed",
    "TicketError",
    "TicketProvider",
    "TurnResult",
    "TurnTimeout",
    "Unauthorized",
    "WatchScope",
    "Watchlist",
    "action_limits_from",
    "build_event",
    "clamp_interval",
    "content_digest",
    "event_id_for",
    "extract_reply",
    "format_transcript",
    "is_self_message",
    "is_timestamp_row",
    "jittered",
    "local_message_id",
    "looks_like_rejection",
    "latest_cursor",
    "message_signature",
    "parse_active_hours",
    "split_new_messages",
    "to_inbound",
    "within_active_hours",
]
