from __future__ import annotations

import json
from typing import Any, Protocol

import structlog

from wxclient.agents import ChatAgent

DEFAULT_CONTEXT_MESSAGE_LIMIT = 12
logger = structlog.get_logger(__name__)


class ChatAutomationApi(Protocol):
    def check_unread_messages(self, dry_run: bool | None = None) -> dict[str, Any]:
        ...

    def scan_messages(
        self,
        friend: str,
        duration: str = "30s",
        select_sender: bool = False,
        save_file: bool = False,
        save_media: bool = False,
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
) -> dict[str, Any]:
    source_result = _read_source_messages(
        api=api,
        friend=friend,
        duration=duration,
        real=real,
        select_sender=select_sender,
        source=source,
    )
    messages = source_result["messages"]
    context_messages = source_result["context_messages"]
    replyable_messages = [message for message in messages if message.get("is_self") is not True]
    logger.info(
        "reply.chat_messages.read",
        chat=friend,
        message_source=source,
        message_count=len(messages),
        replyable_count=len(replyable_messages),
        latest_message=_message_log_text(messages[-1] if messages else None),
    )
    if messages and not replyable_messages:
        logger.info(
            "reply.chat_messages.skipped",
            chat=friend,
            reason="latest_is_self",
            latest_message=_message_log_text(messages[-1]),
        )
    target_messages = replyable_messages if reply_all else replyable_messages[-1:]
    replies: list[dict[str, Any]] = []

    for message in target_messages:
        inbound_text = str(message.get("text") or "").strip()
        if not inbound_text:
            continue
        context = _build_context_lines(
            chat_name=friend,
            messages=context_messages,
            latest_message=message,
            limit=context_message_limit,
        )
        reply_text = agent.reply(inbound_text, context=context).strip()
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
) -> dict[str, Any]:
    targets = _dedupe_chat_names(friends)
    if not targets:
        raise ValueError("至少需要一个监听对象")

    chat_results: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    message_count = 0

    for chat_name in targets:
        logger.info(
            "watch.chats.start",
            chat=chat_name,
            duration=duration,
            auto_reply=auto_reply,
        )
        try:
            if auto_reply:
                result = reply_to_chat_messages(
                    api=api,
                    agent=agent,
                    friend=chat_name,
                    duration=duration,
                    real=real,
                    reply_all=reply_all,
                    select_sender=select_sender,
                    source="new",
                    context_message_limit=context_message_limit,
                )
                scan_messages = _extract_scan_messages(result.get("source_result", {}))
                chat_replies = [
                    item for item in result.get("replies", []) if isinstance(item, dict)
                ]
                replies.extend(chat_replies)
                chat_results.append(result)
            else:
                scan_result = api.scan_messages(
                    friend=chat_name,
                    duration=duration,
                    select_sender=select_sender,
                    dry_run=not real,
                )
                scan_messages = _extract_scan_messages(scan_result)
                result = {
                    "action": "watch_chat",
                    "friend": chat_name,
                    "duration": duration,
                    "dry_run": not real,
                    "auto_reply": False,
                    "new_message_count": len(scan_messages),
                    "reply_count": 0,
                    "source_result": scan_result,
                }
                chat_results.append(result)

            message_count += len(scan_messages)
            logger.info(
                "watch.chats.read",
                chat=chat_name,
                message_count=len(scan_messages),
                reply_count=int(result.get("reply_count") or 0),
                latest_message=_message_log_text(scan_messages[-1] if scan_messages else None),
            )
            for reply in result.get("replies", []) if isinstance(result, dict) else []:
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
        "select_sender": select_sender,
        "chat_count": len(targets),
        "message_count": message_count,
        "new_message_count": message_count,
        "reply_count": len(replies),
        "error_count": len(errors),
        "friends": targets,
        "replies": replies,
        "errors": errors,
        "results": chat_results,
    }


def _read_source_messages(
    api: ChatAutomationApi,
    friend: str,
    duration: str,
    real: bool,
    select_sender: bool,
    source: str,
) -> dict[str, Any]:
    if source == "new":
        scan_result = api.scan_messages(
            friend=friend,
            duration=duration,
            select_sender=select_sender,
            dry_run=not real,
        )
        return {
            "messages": _extract_scan_messages(scan_result),
            "context_messages": _try_read_visible_messages(api, friend, real),
            "raw_result": scan_result,
        }

    if source == "visible":
        read_result = api.read_visible_messages(friend=friend, dry_run=not real)
        visible_messages = _extract_visible_messages(read_result)
        return {
            "messages": visible_messages,
            "context_messages": visible_messages,
            "raw_result": read_result,
        }

    raise ValueError(f"Unsupported reply source: {source}")


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
    current_results = _reply_to_current_chat_latest(
        api=api,
        agent=agent,
        cursor_store=cursor_store,
        account_id=account_id,
        real=real,
        friend=friend,
        context_message_limit=context_message_limit,
    )
    replies: list[dict[str, Any]] = list(current_results["replies"])
    processed_chats: set[str] = set(current_results["processed_chats"])

    check_result = api.check_unread_messages(dry_run=not real)
    unread_messages = _extract_unread_messages(check_result, friend=friend)
    logger.info(
        "reply.unread_messages.summary",
        chat_count=len(unread_messages),
        message_count=sum(len(messages) for messages in unread_messages.values()),
    )

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

    visible_results = _reply_to_visible_watch_chats(
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
    replies.extend(visible_results["replies"])

    return {
        "action": "reply_unread",
        "dry_run": not real,
        "friend_filter": friend,
        "chat_count": len(unread_messages),
        "message_count": sum(len(messages) for messages in unread_messages.values()),
        "matched_chat_count": len(unread_messages),
        "current_chat": current_results["chat"],
        "current_chat_checked": current_results["checked"],
        "current_chat_replied": current_results["replied"],
        "current_chat_latest_is_today": current_results["latest_is_today"],
        "visible_chat_count": visible_results["visible_chat_count"],
        "visible_message_count": visible_results["visible_message_count"],
        "cursor_seed_count": visible_results["cursor_seed_count"],
        "processed_chats": sorted(processed_chats | set(visible_results["processed_chats"])),
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
    sender = _display_sender(latest, chat_name)
    prompt = _build_unread_reply_prompt(
        chat_name=chat_name,
        sender=sender,
        inbound_text=inbound_text,
    )
    reply_text = agent.reply(prompt, context=context).strip()
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
    target_messages = messages if reply_all else messages[-1:]
    for message in target_messages:
        inbound_text = _message_text(message)
        if not inbound_text:
            continue
        sender = _message_sender(message)
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
        reply_text = agent.reply(prompt, context=context).strip()
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


def _extract_visible_messages(read_result: dict[str, Any]) -> list[dict[str, Any]]:
    messages = read_result.get("messages")
    if isinstance(messages, list):
        return [item for item in messages if isinstance(item, dict)]
    return []


def _try_read_visible_messages(
    api: ChatAutomationApi,
    friend: str,
    real: bool,
) -> list[dict[str, Any]]:
    try:
        read_result = api.read_visible_messages(friend=friend, dry_run=not real)
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
        return chat_name
    return chat_name or "\u5bf9\u65b9"


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
        sender = _display_sender(message, chat_name)
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
    if sender:
        return (
            f"Incoming WeChat message in chat '{chat_name}' from '{sender}':\n"
            f"{inbound_text}\n\nReply naturally and concisely."
        )
    return (
        f"Incoming WeChat message in chat '{chat_name}':\n"
        f"{inbound_text}\n\nReply naturally and concisely."
    )
