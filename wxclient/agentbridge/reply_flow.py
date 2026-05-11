from __future__ import annotations

import json
import time
from typing import Any, Protocol

import structlog

from wxclient.agents import ChatAgent

DEFAULT_CONTEXT_MESSAGE_LIMIT = 12
logger = structlog.get_logger(__name__)


class ChatAutomationApi(Protocol):
    def check_unread_messages(self, dry_run: bool | None = None) -> dict[str, Any]:
        ...

    def read_chat_sessions(self, dry_run: bool | None = None) -> dict[str, Any]:
        ...

    def scan_messages(
        self,
        friend: str,
        duration: str = "30s",
        select_sender: bool = False,
        save_file: bool = False,
        save_media: bool = False,
        separate_window: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        ...

    def send_message(
        self,
        friend: str,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        ...

    def send_current_chat_message(
        self,
        text: str,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        ...

    def read_visible_messages(
        self,
        friend: str,
        separate_window: bool = True,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        ...

    def read_current_chat(self, dry_run: bool | None = None) -> dict[str, Any]:
        ...


class MessageCursorStore(Protocol):
    def get_message_cursor(
        self,
        account_id: str,
        chat_name: str,
        source: str = "visible",
    ) -> dict[str, Any] | None:
        ...

    def set_message_cursor(
        self,
        account_id: str,
        chat_name: str,
        cursor: dict[str, Any],
        source: str = "visible",
    ) -> None:
        ...


def reply_to_chat_messages(
    api: ChatAutomationApi,
    agent: ChatAgent,
    friend: str,
    duration: str,
    real: bool,
    reply_all: bool = False,
    select_sender: bool = False,
    source: str = "new",
    context_message_limit: int = DEFAULT_CONTEXT_MESSAGE_LIMIT,
    separate_window: bool = True,
) -> dict[str, Any]:
    source_result = _read_source_messages(
        api=api,
        friend=friend,
        duration=duration,
        real=real,
        select_sender=select_sender,
        source=source,
        separate_window=separate_window,
    )
    messages = source_result["messages"]
    context_messages = source_result["context_messages"]
    target_messages = _reply_target_messages(messages, reply_all=reply_all)
    logger.info(
        "reply.chat_messages.read",
        chat=friend,
        message_source=source,
        message_count=len(messages),
        replyable_count=len(_reply_target_messages(messages, reply_all=True)),
        latest_message=_message_log_text(messages[-1] if messages else None),
        window_reused=source_result["raw_result"].get("window_reused"),
        window_mode=source_result["raw_result"].get("window_mode"),
    )
    _log_chat_messages(
        event="reply.chat_messages.message",
        chat_name=friend,
        messages=messages,
    )
    if messages and not target_messages:
        logger.info(
            "reply.chat_messages.skipped",
            chat=friend,
            reason=_reply_skip_reason(messages[-1]),
            latest_message=_message_log_text(messages[-1]),
        )
    replies: list[dict[str, Any]] = []

    for message in target_messages:
        inbound_text = str(message.get("text") or "").strip()
        if not inbound_text:
            continue
        sender = _context_sender_label(message, friend)
        context = _build_context_lines(
            chat_name=friend,
            messages=context_messages,
            latest_message=message,
            limit=context_message_limit,
        )
        started = time.perf_counter()
        logger.info(
            "reply.agent.request",
            chat=friend,
            source=source,
            input_message=_truncate_text(inbound_text),
            context_count=len(context),
            sender=sender,
            latest_is_self=message.get("is_self"),
            latest_is_today=message.get("timestamp_is_today"),
        )
        reply_text = agent.reply(inbound_text, context=context).strip()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "reply.agent.response",
            chat=friend,
            source=source,
            reply_message=_truncate_text(reply_text),
            context_count=len(context),
            sender=sender,
            latest_is_self=message.get("is_self"),
            latest_is_today=message.get("timestamp_is_today"),
            elapsed_ms=elapsed_ms,
        )
        if not reply_text:
            continue
        send_result = api.send_message(friend=friend, text=reply_text, dry_run=not real)
        logger.info(
            "reply.chat_messages.replied",
            chat=friend,
            inbound_message=_truncate_text(inbound_text),
            reply_message=_truncate_text(reply_text),
            context_count=len(context),
        )
        replies.append(
            {
                "inbound_text": inbound_text,
                "reply_text": reply_text,
                "context_count": len(context),
                "send_result": send_result,
            }
        )

    return {
        "action": "reply_chat",
        "friend": friend,
        "duration": duration,
        "dry_run": not real,
        "message_source": source,
        "new_message_count": len(messages) if source == "new" else 0,
        "visible_message_count": len(messages) if source == "visible" else None,
        "context_message_count": len(context_messages),
        "reply_count": len(replies),
        "replies": replies,
        "source_result": source_result["raw_result"],
    }


def reply_to_new_chat_messages(
    api: ChatAutomationApi,
    agent: ChatAgent,
    friend: str,
    duration: str,
    real: bool,
    reply_all: bool = False,
    select_sender: bool = False,
    separate_window: bool = True,
) -> dict[str, Any]:
    return reply_to_chat_messages(
        api=api,
        agent=agent,
        friend=friend,
        duration=duration,
        real=real,
        reply_all=reply_all,
        select_sender=select_sender,
        source="new",
        separate_window=separate_window,
    )


def watch_chats_messages(
    api: ChatAutomationApi,
    agent: ChatAgent,
    friends: list[str],
    duration: str,
    real: bool,
    auto_reply: bool = True,
    reply_all: bool = False,
    select_sender: bool = False,
    context_message_limit: int = DEFAULT_CONTEXT_MESSAGE_LIMIT,
    cursor_store: MessageCursorStore | None = None,
    account_id: str = "default",
) -> dict[str, Any]:
    targets = _dedupe_chat_names(friends)
    if not targets:
        raise ValueError("至少需要一个监听对象")
    effective_select_sender = bool(select_sender)

    chat_results: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    message_count = 0
    new_message_count = 0
    visible_message_count = 0
    cursor_seed_count = 0
    session_summaries = _try_read_chat_sessions(api, real)

    for chat_name in targets:
        logger.info(
            "watch.chats.start",
            chat=chat_name,
            duration=duration,
            auto_reply=auto_reply,
        )
        cursor = cursor_store.get_message_cursor(account_id, chat_name, "watch") if cursor_store is not None else None
        outbound_texts = _cursor_outbound_texts(cursor)
        session_summary = _find_chat_session(session_summaries, chat_name)
        session_preview = _session_summary_preview(session_summary)
        session_timestamp_text = _session_summary_timestamp_text(session_summary)
        session_is_today = _session_summary_is_today(session_summary)
        preview_self_source = _session_preview_self_reason(session_preview, outbound_texts)
        try:
            source_result = _read_source_messages(
                api=api,
                friend=chat_name,
                duration=duration,
                real=real,
                select_sender=False,
                source="visible",
                separate_window=False,
            )
            visible_messages = source_result["messages"]
            visible_message_count += len(visible_messages)

            if cursor_store is not None:
                _mark_recent_outbound_messages(visible_messages, outbound_texts)
            latest_message_row = _latest_activity_message(visible_messages)
            if (
                session_preview is not None
                and isinstance(latest_message_row, dict)
                and latest_message_row.get("is_self") is None
                and _session_preview_is_self_message(session_preview, _message_text(latest_message_row))
            ):
                latest_message_row["is_self"] = True
                latest_message_row["sender"] = "\u6211"
            seeded = False
            reply_messages = _watch_latest_reply_messages(latest_message_row)
            reply_message_source = "visible_latest" if reply_messages else None

            chat_replies: list[dict[str, Any]] = []
            if auto_reply:
                chat_replies = _reply_to_chat_batch(
                    api=api,
                    agent=agent,
                    chat_name=chat_name,
                    messages=reply_messages,
                    context_messages=visible_messages,
                    reply_all=reply_all,
                    real=real,
                    source="watch",
                    context_message_limit=context_message_limit,
                )
                replies.extend(chat_replies)

            if cursor_store is not None:
                next_cursor = _cursor_from_messages(visible_messages, previous_cursor=cursor)
                _append_outbound_texts(
                    next_cursor,
                    [str(reply.get("reply_text") or "") for reply in chat_replies],
                )
                cursor_store.set_message_cursor(
                    account_id,
                    chat_name,
                    next_cursor,
                    "watch",
                )

            latest_message = _message_log_text(latest_message_row) or _truncate_text(session_preview)
            latest_message_source = _watch_latest_message_source(
                session_preview=session_preview,
                new_messages=[],
                visible_messages=visible_messages,
            )
            latest_is_self = latest_message_row.get("is_self") if isinstance(latest_message_row, dict) else None
            latest_is_self_source = _message_self_source(latest_message_row, outbound_texts) or preview_self_source
            decision_reason = _watch_decision_reason(
                auto_reply=auto_reply,
                latest_message=latest_message_row,
                reply_messages=reply_messages,
                replies=chat_replies,
            )

            result = {
                "action": "watch_chat",
                "friend": chat_name,
                "duration": duration,
                "dry_run": not real,
                "auto_reply": auto_reply,
                "visible_message_count": len(visible_messages),
                "new_message_count": len(reply_messages),
                "reply_count": len(chat_replies),
                "cursor_seeded": seeded,
                "source_result": source_result["raw_result"],
                "latest_message": latest_message,
                "latest_message_source": latest_message_source,
                "latest_is_self": latest_is_self,
                "latest_is_self_source": latest_is_self_source,
                "decision_reason": decision_reason,
                "fallback_window_opened": False,
                "reply_message_source": reply_message_source,
                "session_preview": session_preview,
                "timestamp_text": latest_message_row.get("timestamp_text") if isinstance(latest_message_row, dict) else session_timestamp_text,
                "latest_is_today": latest_message_row.get("timestamp_is_today") if isinstance(latest_message_row, dict) else session_is_today,
            }
            chat_results.append(result)

            message_count += len(reply_messages)
            new_message_count += len(reply_messages)
            logger.info(
                "watch.chats.summary",
                chat=chat_name,
                message_count=len(reply_messages),
                visible_message_count=len(visible_messages),
                reply_count=int(result.get("reply_count") or 0),
                latest_message=latest_message,
                latest_message_source=latest_message_source,
                latest_is_self=latest_is_self,
                latest_is_self_source=latest_is_self_source,
                timestamp_text=result["timestamp_text"],
                latest_is_today=result["latest_is_today"],
                decision_reason=decision_reason,
                fallback_window_opened=False,
                reply_message_source=reply_message_source,
            )
            _log_chat_messages(
                event="watch.chats.message",
                chat_name=chat_name,
                messages=visible_messages,
            )
            if auto_reply and not chat_replies:
                logger.info(
                    "watch.chats.skip_summary",
                    chat=chat_name,
                    reason=decision_reason,
                    visible_message_count=len(visible_messages),
                    latest_message=latest_message,
                    latest_message_source=latest_message_source,
                    latest_is_self=latest_is_self,
                    latest_is_self_source=latest_is_self_source,
                    timestamp_text=result["timestamp_text"],
                    latest_is_today=result["latest_is_today"],
                    decision_reason=decision_reason,
                )
            for reply in chat_replies:
                if not isinstance(reply, dict):
                    continue
                logger.info(
                    "watch.chats.replied",
                    chat=chat_name,
                    inbound_message=_truncate_text(str(reply.get("inbound_text") or "")),
                    reply_message=_truncate_text(str(reply.get("reply_text") or "")),
                    context_count=reply.get("context_count", 0),
                )
        except Exception as exc:
            errors.append({"chat": chat_name, "error": str(exc)})
            logger.warning("watch.chats.failed", chat=chat_name, error=str(exc))

    return {
        "action": "watch_chats",
        "dry_run": not real,
        "duration": duration,
        "auto_reply": auto_reply,
        "reply_all": reply_all,
        "select_sender": effective_select_sender,
        "chat_count": len(targets),
        "message_count": message_count,
        "new_message_count": new_message_count,
        "visible_message_count": visible_message_count,
        "cursor_seed_count": cursor_seed_count,
        "reply_count": len(replies),
        "error_count": len(errors),
        "friends": targets,
        "replies": replies,
        "errors": errors,
        "results": chat_results,
    }


def _try_read_chat_sessions(api: ChatAutomationApi, real: bool) -> list[dict[str, Any]]:
    read_sessions = getattr(api, "read_chat_sessions", None)
    if read_sessions is None:
        return []
    try:
        result = read_sessions(dry_run=not real)
    except Exception:
        return []
    return _extract_chat_sessions(result)


def _extract_chat_sessions(result: dict[str, Any]) -> list[dict[str, Any]]:
    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        return []
    return [item for item in sessions if isinstance(item, dict)]


def _find_chat_session(sessions: list[dict[str, Any]], chat_name: str) -> dict[str, Any] | None:
    if not sessions:
        return None
    matches = [
        session
        for session in sessions
        if _chat_matches_filter(str(session.get("name") or ""), chat_name)
    ]
    if not matches:
        return None
    return matches[0]


def _session_summary_preview(session: dict[str, Any] | None) -> str | None:
    if not session:
        return None
    preview = session.get("preview")
    preview_text = str(preview or "").strip()
    return preview_text or None


def _session_summary_timestamp_text(session: dict[str, Any] | None) -> str | None:
    if not session:
        return None
    value = str(session.get("timestamp_text") or "").strip()
    return value or None


def _session_summary_is_today(session: dict[str, Any] | None) -> bool | None:
    if not session:
        return None
    value = session.get("timestamp_is_today")
    if value is None:
        return None
    return bool(value)


def _session_preview_self_reason(preview: str | None, outbound_texts: list[str]) -> str | None:
    preview_text = str(preview or "").strip()
    if not preview_text:
        return None
    self_prefixes = (
        "你:",
        "你：",
        "我:",
        "我：",
        "You:",
        "Me:",
    )
    outbound = {str(text or "").strip() for text in outbound_texts if str(text or "").strip()}
    for prefix in self_prefixes:
        if preview_text.startswith(prefix):
            tail = preview_text[len(prefix) :].strip()
            if not outbound or not tail or tail in outbound or preview_text in outbound:
                return f"preview_prefix:{prefix}"
    if outbound and preview_text in outbound:
        return "preview_matches_outbound"
    return None


def _session_preview_is_self_message(preview: str | None, message_text: str) -> bool:
    preview_text = str(preview or "").strip()
    message = str(message_text or "").strip()
    if not preview_text or not message:
        return False
    self_prefixes = (
        "\u4f60:",
        "\u4f60\uff1a",
        "\u6211:",
        "\u6211\uff1a",
        "You:",
        "Me:",
    )
    for prefix in self_prefixes:
        if preview_text.startswith(prefix) and preview_text[len(prefix) :].strip() == message:
            return True
    return False


def _message_self_source(message: dict[str, Any] | None, outbound_texts: list[str]) -> str | None:
    if message is None:
        return None
    if message.get("is_self") is True:
        text = _message_text(message)
        if text and text in set(outbound_texts):
            return "recent_outbound_cache"
        return "message_annotation"
    if message.get("is_self") is False:
        return "message_annotation"
    return None


def _watch_latest_message_source(
    session_preview: str | None,
    new_messages: list[dict[str, Any]],
    visible_messages: list[dict[str, Any]],
) -> str:
    if new_messages:
        return "scan_messages"
    if visible_messages:
        return "visible_messages"
    if session_preview:
        return "session_preview"
    return "unknown"


def _watch_decision_reason(
    *,
    auto_reply: bool,
    latest_message: dict[str, Any] | None,
    reply_messages: list[dict[str, Any]],
    replies: list[dict[str, Any]],
) -> str:
    if not auto_reply:
        return "auto_reply_disabled"
    if replies:
        return "replied"
    if latest_message is None:
        return "no_visible_message"
    if not reply_messages:
        return _watch_reply_skip_reason(latest_message)
    return "empty_agent_reply"


def _read_source_messages(
    api: ChatAutomationApi,
    friend: str,
    duration: str,
    real: bool,
    select_sender: bool,
    source: str,
    separate_window: bool,
) -> dict[str, Any]:
    if source == "new":
        scan_result = api.scan_messages(
            friend=friend,
            duration=duration,
            select_sender=select_sender,
            separate_window=separate_window,
            dry_run=not real,
        )
        return {
            "messages": _extract_scan_messages(scan_result),
            "context_messages": _try_read_visible_messages(api, friend, real, separate_window),
            "raw_result": scan_result,
        }

    if source == "visible":
        read_result = api.read_visible_messages(
            friend=friend,
            separate_window=separate_window,
            dry_run=not real,
        )
        visible_messages = _extract_visible_messages(read_result)
        return {
            "messages": visible_messages,
            "context_messages": visible_messages,
            "raw_result": read_result,
        }

    raise ValueError(f"Unsupported reply source: {source}")


def _log_chat_messages(
    event: str,
    chat_name: str,
    messages: list[dict[str, Any]],
    limit: int = 20,
) -> None:
    if not messages:
        return
    display_messages = [message for message in messages if not message.get("is_timestamp")]
    if not display_messages:
        return
    start_index = max(0, len(display_messages) - limit)
    total = len(display_messages)
    for offset, message in enumerate(display_messages[start_index:], start=start_index + 1):
        logger.info(
            event,
            chat=chat_name,
            index=offset,
            total=total,
            sender=_display_sender(message, chat_name),
            text=_truncate_text(_message_text(message)),
            is_self=message.get("is_self"),
            timestamp_text=message.get("timestamp_text"),
            timestamp_is_today=message.get("timestamp_is_today"),
        )


def reply_to_unread_messages(
    api: ChatAutomationApi,
    agent: ChatAgent,
    real: bool,
    reply_all: bool = False,
    friend: str | None = None,
    cursor_store: MessageCursorStore | None = None,
    account_id: str = "default",
    visible_chats: list[str] | None = None,
    context_message_limit: int = DEFAULT_CONTEXT_MESSAGE_LIMIT,
) -> dict[str, Any]:
    current_result = _reply_to_current_chat_latest(
        api=api,
        agent=agent,
        cursor_store=cursor_store,
        account_id=account_id,
        real=real,
        friend=friend,
        context_message_limit=context_message_limit,
    )
    check_result = api.check_unread_messages(dry_run=not real)
    unread_messages = _extract_unread_messages(check_result, friend=friend)
    logger.info(
        "reply.unread_messages.summary",
        chat_count=len(unread_messages),
        message_count=sum(len(messages) for messages in unread_messages.values()),
    )

    replies: list[dict[str, Any]] = list(current_result.get("replies") or [])
    processed_chats: set[str] = set(current_result.get("processed_chats") or [])

    for chat_name, messages in unread_messages.items():
        logger.info(
            "reply.unread_messages.read",
            chat=chat_name,
            message_count=len(messages),
            latest_message=_message_log_text(messages[-1] if messages else None),
        )
        if chat_name in processed_chats:
            logger.info(
                "reply.unread_messages.skipped",
                chat=chat_name,
                reason="already_processed_current_chat",
            )
            continue
        processed_chats.add(chat_name)
        context_messages = messages
        replies.extend(
            _reply_to_chat_batch(
                api=api,
                agent=agent,
                chat_name=chat_name,
                messages=messages,
                context_messages=context_messages,
                reply_all=reply_all,
                real=real,
                source="unread",
                context_message_limit=context_message_limit,
            )
        )
        if cursor_store is not None:
            cursor_store.set_message_cursor(
                account_id,
                chat_name,
                _cursor_from_messages(context_messages),
                "visible",
            )

    visible_result = _reply_to_visible_watch_chats(
        api=api,
        agent=agent,
        cursor_store=cursor_store,
        account_id=account_id,
        visible_chats=visible_chats or [],
        processed_chats=processed_chats,
        reply_all=reply_all,
        real=real,
        friend=friend,
        context_message_limit=context_message_limit,
    )
    replies.extend(visible_result.get("replies") or [])

    return {
        "action": "reply_unread",
        "dry_run": not real,
        "friend_filter": friend,
        "chat_count": len(unread_messages),
        "message_count": sum(len(messages) for messages in unread_messages.values()),
        "matched_chat_count": len(unread_messages),
        "current_chat_checked": bool(current_result.get("checked")),
        "current_chat_replied": bool(current_result.get("replied")),
        "current_chat": current_result.get("chat"),
        "current_chat_latest_is_today": current_result.get("latest_is_today"),
        "visible_chat_count": visible_result.get("visible_chat_count", 0),
        "visible_message_count": visible_result.get("visible_message_count", 0),
        "cursor_seed_count": visible_result.get("cursor_seed_count", 0),
        "reply_count": len(replies),
        "replies": replies,
        "check_result": check_result,
    }


def _reply_to_current_chat_latest(
    api: ChatAutomationApi,
    agent: ChatAgent,
    cursor_store: MessageCursorStore | None,
    account_id: str,
    real: bool,
    friend: str | None,
    context_message_limit: int,
) -> dict[str, Any]:
    read_current = getattr(api, "read_current_chat", None)
    if read_current is None:
        return {
            "checked": False,
            "replied": False,
            "chat": None,
            "latest_is_today": None,
            "replies": [],
            "processed_chats": [],
        }

    try:
        current = read_current(dry_run=not real)
    except Exception as exc:
        logger.warning("reply.current_chat.read_failed", error=str(exc))
        return {
            "checked": False,
            "replied": False,
            "chat": None,
            "latest_is_today": None,
            "replies": [],
            "processed_chats": [],
        }

    chat_name = str(current.get("chat") or "").strip()
    messages = _extract_visible_messages(current)
    latest = current.get("latest_message")
    if not isinstance(latest, dict) and messages:
        latest = messages[-1]
    cursor = None
    if cursor_store is not None and chat_name:
        cursor = cursor_store.get_message_cursor(account_id, chat_name, "visible")
        _mark_recent_outbound_messages(messages, _cursor_outbound_texts(cursor))
        if isinstance(latest, dict):
            _mark_recent_outbound_messages([latest], _cursor_outbound_texts(cursor))
    logger.info(
        "reply.current_chat.read",
        chat=chat_name or None,
        message_count=len(messages),
        latest_message=_message_log_text(latest if isinstance(latest, dict) else None),
        latest_is_self=_message_is_self(current, latest if isinstance(latest, dict) else None),
        latest_is_today=latest.get("timestamp_is_today") if isinstance(latest, dict) else None,
        timestamp_text=latest.get("timestamp_text") if isinstance(latest, dict) else None,
    )
    if not chat_name or not isinstance(latest, dict):
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name or None,
            "latest_is_today": None,
            "replies": [],
            "processed_chats": [],
        }
    if friend and not _chat_matches_filter(chat_name, friend):
        logger.info(
            "reply.current_chat.skipped",
            chat=chat_name,
            reason="friend_filter",
            latest_message=_message_log_text(latest),
        )
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name,
            "latest_is_today": latest.get("timestamp_is_today"),
            "replies": [],
            "processed_chats": [],
        }

    latest_is_today = latest.get("timestamp_is_today")
    if latest_is_today is False:
        if cursor_store is not None:
            cursor_store.set_message_cursor(
                account_id,
                chat_name,
                _cursor_from_messages(messages, previous_cursor=cursor),
                "visible",
            )
        logger.info(
            "reply.current_chat.skipped",
            chat=chat_name,
            reason="not_today",
            latest_message=_message_log_text(latest),
            timestamp_text=latest.get("timestamp_text"),
        )
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name,
            "latest_is_today": latest_is_today,
            "replies": [],
            "processed_chats": [],
        }

    latest_is_self = current.get("latest_is_self")
    if latest.get("is_self") is not None:
        latest_is_self = latest.get("is_self")
    if latest_is_self is True:
        if cursor_store is not None:
            cursor_store.set_message_cursor(
                account_id,
                chat_name,
                _cursor_from_messages(messages, previous_cursor=cursor),
                "visible",
            )
        logger.info(
            "reply.current_chat.skipped",
            chat=chat_name,
            reason="latest_is_self",
            latest_message=_message_log_text(latest),
        )
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name,
            "latest_is_today": latest_is_today,
            "replies": [],
            "processed_chats": [],
        }

    if latest_is_self is None:
        logger.info(
            "reply.current_chat.skipped",
            chat=chat_name,
            reason="unknown_sender",
            latest_message=_message_log_text(latest),
        )
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name,
            "latest_is_today": latest_is_today,
            "replies": [],
            "processed_chats": [],
        }

    if _cursor_contains_message(cursor, latest):
        logger.info(
            "reply.current_chat.skipped",
            chat=chat_name,
            reason="already_processed_current_chat",
            latest_message=_message_log_text(latest),
        )
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name,
            "latest_is_today": latest_is_today,
            "replies": [],
            "processed_chats": [],
        }

    inbound_text = _message_text(latest)
    if not inbound_text:
        logger.info(
            "reply.current_chat.skipped",
            chat=chat_name,
            reason="empty_latest_message",
        )
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name,
            "latest_is_today": latest_is_today,
            "replies": [],
            "processed_chats": [],
        }

    context = _build_context_lines(
        chat_name=chat_name,
        messages=messages,
        latest_message=latest,
        limit=context_message_limit,
    )
    sender = _context_sender_label(latest, chat_name)
    prompt = _build_unread_reply_prompt(
        chat_name=chat_name,
        sender=sender,
        inbound_text=inbound_text,
    )
    started = time.perf_counter()
    logger.info(
        "reply.agent.request",
        chat=chat_name,
        source="unread",
        input_message=_truncate_text(inbound_text),
        context_count=len(context),
        sender=sender,
        latest_is_self=latest.get("is_self"),
        latest_is_today=latest.get("timestamp_is_today"),
    )
    reply_text = agent.reply(prompt, context=context).strip()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "reply.agent.response",
        chat=chat_name,
        source="unread",
        reply_message=_truncate_text(reply_text),
        context_count=len(context),
        sender=sender,
        latest_is_self=latest.get("is_self"),
        latest_is_today=latest.get("timestamp_is_today"),
        elapsed_ms=elapsed_ms,
    )
    if not reply_text:
        logger.info(
            "reply.current_chat.skipped",
            chat=chat_name,
            reason="empty_agent_reply",
            latest_message=_message_log_text(latest),
        )
        return {
            "checked": True,
            "replied": False,
            "chat": chat_name,
            "latest_is_today": latest_is_today,
            "replies": [],
            "processed_chats": [],
        }

    send_current = getattr(api, "send_current_chat_message", None)
    if send_current is not None:
        send_result = send_current(text=reply_text, dry_run=not real)
    else:
        send_result = api.send_message(friend=chat_name, text=reply_text, dry_run=not real)
    logger.info(
        "reply.current_chat.replied",
        chat=chat_name,
        inbound_message=_truncate_text(inbound_text),
        reply_message=_truncate_text(reply_text),
        context_count=len(context),
    )

    if cursor_store is not None:
        refreshed = _try_read_current_chat_messages(api, real)
        cursor_store.set_message_cursor(
            account_id,
            chat_name,
            _cursor_from_messages(refreshed or messages, previous_cursor=cursor, outbound_text=reply_text),
            "visible",
        )

    return {
        "checked": True,
        "replied": True,
        "chat": chat_name,
        "latest_is_today": latest_is_today,
        "processed_chats": [chat_name],
        "replies": [
            {
                "chat": chat_name,
                "sender": sender,
                "source": "current",
                "inbound_text": inbound_text,
                "reply_text": reply_text,
                "context_count": len(context),
                "send_result": send_result,
            }
        ],
    }


def _reply_to_visible_watch_chats(
    api: ChatAutomationApi,
    agent: ChatAgent,
    cursor_store: MessageCursorStore | None,
    account_id: str,
    visible_chats: list[str],
    processed_chats: set[str],
    reply_all: bool,
    real: bool,
    friend: str | None,
    context_message_limit: int,
) -> dict[str, Any]:
    if cursor_store is None:
        return {
            "replies": [],
            "visible_chat_count": 0,
            "visible_message_count": 0,
            "cursor_seed_count": 0,
            "processed_chats": [],
        }

    replies: list[dict[str, Any]] = []
    visible_message_count = 0
    cursor_seed_count = 0
    watched_chats = _dedupe_chat_names(visible_chats)
    scanned_chats: list[str] = []

    for chat_name in watched_chats:
        if chat_name in processed_chats:
            continue
        if friend and not _chat_matches_filter(chat_name, friend):
            continue
        read_result = api.read_visible_messages(friend=chat_name, dry_run=not real)
        visible_messages = _extract_visible_messages(read_result)
        visible_message_count += len(visible_messages)
        cursor = cursor_store.get_message_cursor(account_id, chat_name, "visible")
        new_messages, seeded = _messages_after_cursor(visible_messages, cursor)
        if seeded:
            cursor_seed_count += 1
            cursor_store.set_message_cursor(
                account_id,
                chat_name,
                _cursor_from_messages(visible_messages),
                "visible",
            )
            continue
        if not new_messages:
            cursor_store.set_message_cursor(
                account_id,
                chat_name,
                _cursor_from_messages(visible_messages),
                "visible",
            )
            continue

        scanned_chats.append(chat_name)
        replies.extend(
            _reply_to_chat_batch(
                api=api,
                agent=agent,
                chat_name=chat_name,
                messages=new_messages,
                context_messages=visible_messages,
                reply_all=reply_all,
                real=real,
                source="visible",
                context_message_limit=context_message_limit,
            )
        )
        _refresh_visible_cursor(
            api=api,
            cursor_store=cursor_store,
            account_id=account_id,
            chat_name=chat_name,
            real=real,
            fallback_messages=visible_messages,
        )

    return {
        "replies": replies,
        "visible_chat_count": len(watched_chats),
        "visible_message_count": visible_message_count,
        "cursor_seed_count": cursor_seed_count,
        "processed_chats": scanned_chats,
    }


def _reply_to_chat_batch(
    api: ChatAutomationApi,
    agent: ChatAgent,
    chat_name: str,
    messages: list[dict[str, Any]],
    context_messages: list[dict[str, Any]],
    reply_all: bool,
    real: bool,
    source: str,
    context_message_limit: int,
) -> list[dict[str, Any]]:
    replies: list[dict[str, Any]] = []
    target_messages = _reply_target_messages(messages, reply_all=reply_all)
    for message in target_messages:
        inbound_text = _message_text(message)
        if not inbound_text:
            continue
        sender = _context_sender_label(message, chat_name)
        context = _build_context_lines(
            chat_name=chat_name,
            messages=context_messages,
            latest_message=message,
            limit=context_message_limit,
        )
        prompt = _build_unread_reply_prompt(
            chat_name=chat_name,
            sender=sender,
            inbound_text=inbound_text,
        )
        started = time.perf_counter()
        logger.info(
            "reply.agent.request",
            chat=chat_name,
            source=source,
            input_message=_truncate_text(inbound_text),
            context_count=len(context),
            sender=sender,
            latest_is_self=message.get("is_self"),
            latest_is_today=message.get("timestamp_is_today"),
        )
        reply_text = agent.reply(prompt, context=context).strip()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "reply.agent.response",
            chat=chat_name,
            source=source,
            reply_message=_truncate_text(reply_text),
            context_count=len(context),
            sender=sender,
            latest_is_self=message.get("is_self"),
            latest_is_today=message.get("timestamp_is_today"),
            elapsed_ms=elapsed_ms,
        )
        if not reply_text:
            continue
        send_result = api.send_message(
            friend=chat_name,
            text=reply_text,
            dry_run=not real,
        )
        replies.append(
            {
                "chat": chat_name,
                "sender": sender,
                "source": source,
                "inbound_text": inbound_text,
                "reply_text": reply_text,
                "context_count": len(context),
                "send_result": send_result,
            }
        )
    return replies


def _watch_latest_reply_messages(latest: dict[str, Any] | None) -> list[dict[str, Any]]:
    if latest is None:
        return []
    if not _watch_message_can_reply(latest):
        return []
    return [latest]


def _reply_target_messages(
    messages: list[dict[str, Any]],
    *,
    reply_all: bool,
) -> list[dict[str, Any]]:
    if reply_all:
        return [message for message in messages if _message_can_trigger_reply(message)]
    latest = _latest_activity_message(messages)
    if latest is None or not _message_can_trigger_reply(latest):
        return []
    return [latest]


def _watch_message_can_reply(message: dict[str, Any]) -> bool:
    if message.get("is_timestamp"):
        return False
    if not _message_text(message):
        return False
    if message.get("is_self") is True:
        return False
    return True


def _watch_reply_skip_reason(latest: dict[str, Any]) -> str:
    if latest.get("is_self") is True:
        return "latest_is_self"
    if not _message_text(latest):
        return "empty_latest_message"
    return "no_replyable_message"


def _latest_activity_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("is_timestamp"):
            continue
        if _message_text(message):
            return message
    return None


def _message_can_trigger_reply(message: dict[str, Any]) -> bool:
    if message.get("is_timestamp"):
        return False
    if not _message_text(message):
        return False
    if message.get("is_self") is True:
        return False
    if message.get("timestamp_is_today") is False:
        return False
    return True


def _reply_skip_reason(latest: dict[str, Any]) -> str:
    if latest.get("is_self") is True:
        return "latest_is_self"
    if latest.get("timestamp_is_today") is False:
        return "not_today"
    if not _message_text(latest):
        return "empty_latest_message"
    return "no_replyable_message"


def _refresh_visible_cursor(
    api: ChatAutomationApi,
    cursor_store: MessageCursorStore | None,
    account_id: str,
    chat_name: str,
    real: bool,
    fallback_messages: list[dict[str, Any]],
) -> None:
    if cursor_store is None:
        return
    messages = fallback_messages
    try:
        read_result = api.read_visible_messages(friend=chat_name, dry_run=not real)
        messages = _extract_visible_messages(read_result)
    except Exception:
        pass
    cursor_store.set_message_cursor(
        account_id,
        chat_name,
        _cursor_from_messages(messages),
        "visible",
    )


def _extract_scan_messages(scan_result: dict[str, Any]) -> list[dict[str, Any]]:
    nested_result = scan_result.get("result")
    if isinstance(nested_result, dict):
        messages = nested_result.get("messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]

    messages = scan_result.get("messages")
    if isinstance(messages, list):
        return [item for item in messages if isinstance(item, dict)]

    return []


def _extract_baseline_messages(scan_result: dict[str, Any]) -> list[dict[str, Any]]:
    nested_result = scan_result.get("result")
    if isinstance(nested_result, dict):
        messages = nested_result.get("baseline_messages")
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
    return []


def _extract_visible_messages(read_result: dict[str, Any]) -> list[dict[str, Any]]:
    messages = read_result.get("messages")
    if isinstance(messages, list):
        return [item for item in messages if isinstance(item, dict)]
    return []


def _try_read_visible_messages(
    api: ChatAutomationApi,
    friend: str,
    real: bool,
    separate_window: bool = True,
) -> list[dict[str, Any]]:
    try:
        read_result = api.read_visible_messages(
            friend=friend,
            separate_window=separate_window,
            dry_run=not real,
        )
    except Exception:
        return []
    return _extract_visible_messages(read_result)


def _try_read_current_chat_messages(
    api: ChatAutomationApi,
    real: bool,
) -> list[dict[str, Any]]:
    read_current = getattr(api, "read_current_chat", None)
    if read_current is None:
        return []
    try:
        read_result = read_current(dry_run=not real)
    except Exception:
        return []
    return _extract_visible_messages(read_result)


def _extract_unread_messages(
    check_result: dict[str, Any],
    friend: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    raw_messages = check_result.get("messages_by_chat")
    if not isinstance(raw_messages, dict):
        raw_messages = check_result

    rows: dict[str, list[dict[str, Any]]] = {}
    for chat_name, messages in raw_messages.items():
        chat = str(chat_name)
        if friend and not _chat_matches_filter(chat, friend):
            continue
        if not isinstance(messages, list):
            continue
        rows[chat] = [item for item in messages if isinstance(item, dict)]
    return rows


def _dedupe_chat_names(chat_names: list[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for chat_name in chat_names:
        chat = str(chat_name or "").strip()
        if not chat or chat in seen:
            continue
        seen.add(chat)
        rows.append(chat)
    return rows


def _chat_matches_filter(chat_name: str, friend: str) -> bool:
    expected = friend.strip().lower()
    actual = chat_name.strip().lower()
    if not expected:
        return True
    if actual == expected:
        return True
    return expected in actual or actual in expected


def _message_text(message: dict[str, Any]) -> str:
    value = (
        message.get("text")
        or message.get("message")
        or message.get("content")
        or message.get("\u6d88\u606f\u5185\u5bb9")
    )
    return str(value or "").strip()


def _message_sender(message: dict[str, Any]) -> str | None:
    if message.get("is_self") is True:
        return "\u6211"
    value = (
        message.get("sender")
        or message.get("from")
        or message.get("\u6d88\u606f\u53d1\u9001\u4eba")
    )
    text = str(value or "").strip()
    return text or None


def _display_sender(message: dict[str, Any], chat_name: str) -> str:
    if message.get("is_self") is True:
        return "\u6211"
    sender = _message_sender(message)
    if sender:
        return sender
    if message.get("is_self") is False:
        if message.get("is_group"):
            return "\u5176\u4ed6\u4eba"
        return chat_name
    if message.get("is_group"):
        return "\u672a\u77e5"
    return chat_name or "\u5bf9\u65b9"


def _context_sender_label(message: dict[str, Any], chat_name: str) -> str:
    sender = _message_sender(message)
    normalized_sender = str(sender or "").strip().lower()
    if message.get("is_self") is True or normalized_sender in {"self", "me"} or sender == "\u6211":
        return "\u6211"
    if message.get("is_group"):
        return sender or "\u5176\u4ed6\u4eba"
    return "\u5bf9\u65b9"


def _message_is_self(current: dict[str, Any], latest: dict[str, Any] | None) -> bool | None:
    if latest is not None and latest.get("is_self") is not None:
        return bool(latest.get("is_self"))
    value = current.get("latest_is_self")
    if value is None:
        return None
    return bool(value)


def _message_log_text(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    return _truncate_text(_message_text(message))


def _truncate_text(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _build_context_lines(
    chat_name: str,
    messages: list[dict[str, Any]],
    latest_message: dict[str, Any],
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    latest_fingerprint = _message_fingerprint(latest_message)
    rows: list[str] = []
    for message in messages:
        if message.get("is_timestamp"):
            continue
        text = _message_text(message)
        if not text:
            continue
        if _message_fingerprint(message) == latest_fingerprint:
            continue
        sender = _context_sender_label(message, chat_name)
        rows.append(f"{sender}: {text}")
    return rows[-limit:]


def _cursor_from_messages(
    messages: list[dict[str, Any]],
    previous_cursor: dict[str, Any] | None = None,
    outbound_text: str | None = None,
) -> dict[str, Any]:
    cursor = {
        "tail": [_message_fingerprint(item) for item in messages[-20:]],
        "count": len(messages),
    }
    outbound_texts = _cursor_outbound_texts(previous_cursor)
    outbound = str(outbound_text or "").strip()
    if outbound:
        outbound_texts.append(outbound)
    if outbound_texts:
        cursor["outbound_texts"] = list(dict.fromkeys(outbound_texts))[-20:]
    return cursor


def _append_outbound_texts(cursor: dict[str, Any], texts: list[str]) -> None:
    outbound_texts = _cursor_outbound_texts(cursor)
    outbound_texts.extend(str(text or "").strip() for text in texts if str(text or "").strip())
    if outbound_texts:
        cursor["outbound_texts"] = list(dict.fromkeys(outbound_texts))[-20:]


def _cursor_contains_message(cursor: dict[str, Any] | None, message: dict[str, Any]) -> bool:
    if not cursor:
        return False
    tail = cursor.get("tail")
    if not isinstance(tail, list):
        return False
    return _message_fingerprint(message) in {str(item) for item in tail}


def _cursor_outbound_texts(cursor: dict[str, Any] | None) -> list[str]:
    if not cursor:
        return []
    values = cursor.get("outbound_texts")
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def _mark_recent_outbound_messages(messages: list[dict[str, Any]], outbound_texts: list[str]) -> None:
    if not outbound_texts:
        return
    outbound = set(outbound_texts)
    for message in messages:
        if _message_text(message).strip() in outbound:
            message["is_self"] = True
            message["sender"] = "\u6211"


def _messages_after_cursor(
    messages: list[dict[str, Any]],
    cursor: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], bool]:
    if cursor is None:
        return [], True
    tail = cursor.get("tail")
    if not isinstance(tail, list) or not tail:
        return [], True

    fingerprints = [_message_fingerprint(item) for item in messages]
    start = _find_tail_end_index(fingerprints, [str(item) for item in tail])
    if start is None:
        return [], True
    return messages[start:], False


def _find_tail_end_index(fingerprints: list[str], tail: list[str]) -> int | None:
    max_len = min(len(fingerprints), len(tail))
    for length in range(max_len, 0, -1):
        needle = tail[-length:]
        for start in range(len(fingerprints) - length, -1, -1):
            if fingerprints[start : start + length] == needle:
                return start + length
    return None


def _message_in_list(message: dict[str, Any], messages: list[dict[str, Any]]) -> bool:
    fingerprint = _message_fingerprint(message)
    return any(_message_fingerprint(item) == fingerprint for item in messages)


def _message_fingerprint(message: dict[str, Any]) -> str:
    payload = [
        _message_sender(message) or "",
        _message_text(message),
        str(message.get("class_name") or ""),
        str(message.get("control_type") or ""),
        str(message.get("runtime_id") or ""),
        str(message.get("timestamp_text") or ""),
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _build_unread_reply_prompt(
    chat_name: str,
    sender: str | None,
    inbound_text: str,
) -> str:
    prefix = f"Incoming WeChat message in chat '{chat_name}'"
    if sender:
        return (
            f"{prefix} from '{sender}':\n"
            f"{inbound_text}\n\n"
            "Reply naturally and concisely.\n"
            "Use the attached context, where lines are labeled 我 / 对方 / 群成员, "
            "to distinguish who said what."
        )
    return (
        f"{prefix}:\n"
        f"{inbound_text}\n\n"
        "Reply naturally and concisely.\n"
        "Use the attached context, where lines are labeled 我 / 对方 / 群成员, "
        "to distinguish who said what."
    )
