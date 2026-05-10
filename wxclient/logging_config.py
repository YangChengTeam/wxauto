from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    _clear_handlers()
    level_name = level.upper()
    level_value = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level_value)

    shared_processors = [
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer = ChineseLogRenderer()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level_value)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            foreign_pre_chain=shared_processors,
        )
    )
    root.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level_value)
        file_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=renderer,
                foreign_pre_chain=shared_processors,
            )
        )
        root.addHandler(file_handler)


def _clear_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


class ChineseLogRenderer:
    def __call__(
        self,
        logger: logging.Logger,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> str:
        timestamp = _format_timestamp(str(event_dict.get("timestamp") or ""))
        event = str(event_dict.get("event") or "")
        message = _render_event(event, event_dict)
        exception = event_dict.get("exception")
        if exception:
            message = f"{message}\n{exception}"
        return f"{timestamp}  {message}"


def _render_event(event: str, payload: dict[str, Any]) -> str:
    task_id = str(payload.get("task_id") or "")
    short_id = task_id[:8] if task_id else "-"
    task_type = _task_type_label(str(payload.get("task_type") or ""))
    attempts = payload.get("attempts")

    if event == "reply.current_chat.read":
        chat = payload.get("chat") or "未选中会话"
        latest = payload.get("latest_message") or "无"
        return (
            f"读取当前会话「{chat}」：可见消息 {payload.get('message_count', 0)} 条，"
            f"最后一条「{latest}」，自己发送={_yes_no(payload.get('latest_is_self'))}，"
            f"今天={_yes_no(payload.get('latest_is_today'))}，时间={payload.get('timestamp_text') or '-'}"
        )
    if event == "reply.current_chat.read_failed":
        return f"读取当前会话失败：{payload.get('error', '')}"
    if event == "reply.current_chat.skipped":
        chat = payload.get("chat") or "-"
        latest = payload.get("latest_message") or ""
        suffix = f"，最后一条「{latest}」" if latest else ""
        return f"跳过当前会话「{chat}」：{_skip_reason_label(payload.get('reason'))}{suffix}"
    if event == "reply.current_chat.replied":
        return (
            f"已回复当前会话「{payload.get('chat') or '-'}」："
            f"收到「{payload.get('inbound_message') or ''}」，"
            f"回复「{payload.get('reply_message') or ''}」，"
            f"上下文 {payload.get('context_count', 0)} 条"
        )
    if event == "reply.chat_messages.read":
        return (
            f"读取监听会话「{payload.get('chat') or '-'}」："
            f"方式 {payload.get('message_source') or '-'}，"
            f"新消息 {payload.get('message_count', 0)} 条，"
            f"可回复 {payload.get('replyable_count', 0)} 条，"
            f"最后一条「{payload.get('latest_message') or ''}」"
        )
    if event == "reply.chat_messages.skipped":
        return (
            f"跳过监听会话「{payload.get('chat') or '-'}」："
            f"{_skip_reason_label(payload.get('reason'))}"
        )
    if event == "reply.chat_messages.replied":
        return (
            f"已回复监听会话「{payload.get('chat') or '-'}」："
            f"收到「{payload.get('inbound_message') or ''}」，"
            f"回复「{payload.get('reply_message') or ''}」，"
            f"上下文 {payload.get('context_count', 0)} 条"
        )
    if event == "watch.chats.start":
        mode = "自动回复" if payload.get("auto_reply") else "只监听"
        return (
            f"开始监听「{payload.get('chat') or '-'}」，"
            f"时长 {payload.get('duration') or '-'}，模式：{mode}。"
        )
    if event == "watch.chats.read":
        return (
            f"监听完成「{payload.get('chat') or '-'}」："
            f"读取新消息 {payload.get('message_count', 0)} 条，"
            f"已回复 {payload.get('reply_count', 0)} 条，"
            f"最后一条「{payload.get('latest_message') or ''}」"
        )
    if event == "watch.chats.replied":
        return (
            f"消息监听已发送回复「{payload.get('chat') or '-'}」："
            f"收到「{payload.get('inbound_message') or ''}」，"
            f"回复「{payload.get('reply_message') or ''}」，"
            f"上下文 {payload.get('context_count', 0)} 条"
        )
    if event == "watch.chats.failed":
        return f"监听「{payload.get('chat') or '-'}」失败：{payload.get('error', '')}"
    if event == "reply.unread_messages.summary":
        return (
            f"未读检测完成：会话 {payload.get('chat_count', 0)} 个，"
            f"消息 {payload.get('message_count', 0)} 条"
        )
    if event == "reply.unread_messages.read":
        return (
            f"读取未读会话「{payload.get('chat') or '-'}」："
            f"{payload.get('message_count', 0)} 条，最后一条「{payload.get('latest_message') or ''}」"
        )
    if event == "reply.unread_messages.skipped":
        return (
            f"跳过未读会话「{payload.get('chat') or '-'}」："
            f"{_skip_reason_label(payload.get('reason'))}"
        )

    if event == "worker.started":
        return f"服务已启动，轮询间隔 {payload.get('poll_interval', '-')} 秒。"
    if event == "worker.stopped":
        return "服务已停止。"
    if event == "task.running":
        return f"开始执行「{task_type}」#{short_id}，第 {attempts} 次尝试。"
    if event == "task.succeeded":
        detail = _summarize_result(payload.get("result"))
        suffix = f" {detail}" if detail else ""
        return f"完成「{task_type}」#{short_id}。{suffix}"
    if event == "task.blocked":
        return f"「{task_type}」#{short_id} 被阻塞：{payload.get('error', '')}"
    if event == "task.failed":
        return f"「{task_type}」#{short_id} 执行失败：{payload.get('error', '')}"
    return event or "日志已写入。"


def _task_type_label(task_type: str) -> str:
    labels = {
        "add_friend": "添加好友任务",
        "check_new_friends": "好友检测任务",
        "accept_new_friends": "自动同意好友任务",
        "send_message": "发送消息任务",
        "scan_messages": "聊天监听任务",
        "read_visible_messages": "读取可见消息任务",
        "check_unread_messages": "未读检测任务",
        "reply_chat": "聊天回复任务",
        "reply_unread": "未读回复任务",
        "watch_chats": "消息监听任务",
    }
    return labels.get(task_type, task_type or "任务")


def _summarize_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    if result.get("dry_run") is True:
        parts.append("模拟执行。")
    labels = {
        "reply_count": "已回复",
        "chat_count": "涉及会话",
        "message_count": "读取消息",
        "new_message_count": "发现新消息",
        "message_source": "读取方式",
        "friend_filter": "会话过滤",
        "error_count": "失败会话",
    }
    for key, label in labels.items():
        if key in result and result[key] is not None:
            parts.append(f"{label} {result[key]}。")
    return " ".join(parts)


def _yes_no(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def _skip_reason_label(value: Any) -> str:
    labels = {
        "friend_filter": "不符合目标会话过滤",
        "not_new_after_cursor": "游标之后没有新消息",
        "not_today": "最后一条不是今天的消息",
        "latest_is_self": "最后一条是自己发送的",
        "empty_latest_message": "最后一条内容为空",
        "empty_agent_reply": "Agent 返回空回复",
        "already_processed_current_chat": "当前会话已处理过",
        "unknown_sender": "无法确认最后一条消息是谁发送的",
    }
    return labels.get(str(value or ""), str(value or "未说明原因"))


def _format_timestamp(value: str) -> str:
    if not value:
        return datetime.now().strftime("%H:%M:%S")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return parsed.astimezone().strftime("%H:%M:%S")
