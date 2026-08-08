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
        reused = payload.get("window_reused")
        window_mode = payload.get("window_mode")
        if window_mode == "main":
            reused_text = "，使用微信主窗口会话"
        elif reused is True:
            reused_text = "，已复用现有独立聊天窗口"
        elif reused is False:
            reused_text = "，已新打开独立聊天窗口"
        else:
            reused_text = ""
        return (
            f"读取监听会话「{payload.get('chat') or '-'}」："
            f"方式 {payload.get('message_source') or '-'}，"
            f"新消息 {payload.get('message_count', 0)} 条，"
            f"可回复 {payload.get('replyable_count', 0)} 条，"
            f"最后一条「{payload.get('latest_message') or ''}」{reused_text}"
        )
    if event == "reply.chat_messages.message":
        return (
            f"监听会话「{payload.get('chat') or '-'}」读到第 "
            f"{payload.get('index', '-')}/{payload.get('total', '-')} 条："
            f"{payload.get('sender') or '未知'}：{payload.get('text') or ''}"
            f"（自己发送={_yes_no(payload.get('is_self'))}，"
            f"时间={payload.get('timestamp_text') or '-'}）"
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
    if event == "reply.agent.request":
        return (
            f"Agent 请求「{payload.get('chat') or '-'}」："
            f"来源={payload.get('source') or '-'}，"
            f"发送方={payload.get('sender') or '-'}，"
            f"消息「{payload.get('input_message') or ''}」，"
            f"上下文 {payload.get('context_count', 0)} 条，"
            f"自己消息={_yes_no(payload.get('latest_is_self'))}，"
            f"今天={_yes_no(payload.get('latest_is_today'))}"
        )
        return (
            f"Agent 请求「{payload.get('chat') or '-'}」："
            f"来源={payload.get('source') or '-'}，"
            f"输入「{payload.get('input_message') or ''}」，"
            f"上下文 {payload.get('context_count', 0)} 条"
        )
    if event == "reply.agent.response":
        return (
            f"Agent 响应「{payload.get('chat') or '-'}」："
            f"来源={payload.get('source') or '-'}，"
            f"发送方={payload.get('sender') or '-'}，"
            f"回复「{payload.get('reply_message') or ''}」，"
            f"上下文 {payload.get('context_count', 0)} 条，"
            f"自己消息={_yes_no(payload.get('latest_is_self'))}，"
            f"今天={_yes_no(payload.get('latest_is_today'))}，"
            f"耗时 {payload.get('elapsed_ms', 0)}ms"
        )
        return (
            f"Agent 返回「{payload.get('chat') or '-'}」："
            f"来源={payload.get('source') or '-'}，"
            f"回复「{payload.get('reply_message') or ''}」，"
            f"上下文 {payload.get('context_count', 0)} 条，"
            f"耗时 {payload.get('elapsed_ms', 0)}ms"
        )
    if event == "chat.sessions.read":
        return (
            f"会话摘要读取：可见会话 {payload.get('session_count', 0)} 个，"
            f"当前选中「{payload.get('selected_session') or '-'}」"
        )
    if event == "watch.chats.summary":
        return (
            f"读取聊天「{payload.get('chat') or '-'}」："
            f"可见消息 {payload.get('visible_message_count', 0)} 条，"
            f"本轮候选 {payload.get('message_count', 0)} 条，"
            f"已回复 {payload.get('reply_count', 0)} 条，"
            f"最后一条「{payload.get('latest_message') or ''}」，"
            f"依据={payload.get('latest_message_source') or '-'}，"
            f"自己消息={_yes_no(payload.get('latest_is_self'))}，"
            f"判定来源={payload.get('latest_is_self_source') or '-'}，"
            f"今天={_yes_no(payload.get('latest_is_today'))}，"
            f"时间={payload.get('timestamp_text') or '-'}，"
            f"判定={_decision_reason_label(payload.get('decision_reason'))}，"
            f"开窗={_yes_no(payload.get('fallback_window_opened'))}"
        )
        return (
            f"监听完成「{payload.get('chat') or '-'}」："
            f"可见消息 {payload.get('visible_message_count', 0)} 条，"
            f"监听到新消息 {payload.get('message_count', 0)} 条，"
            f"已回复 {payload.get('reply_count', 0)} 条，"
            f"最后一条「{payload.get('latest_message') or ''}」，"
            f"最新消息依据={payload.get('latest_message_source') or '-'}，"
            f"判定={payload.get('decision_reason') or '-'}"
        )
    if event == "watch.chats.skip_summary":
        return (
            f"跳过聊天「{payload.get('chat') or '-'}」："
            f"{_skip_reason_label(payload.get('reason'))}，"
            f"最后一条「{payload.get('latest_message') or ''}」，"
            f"依据={payload.get('latest_message_source') or '-'}，"
            f"自己消息={_yes_no(payload.get('latest_is_self'))}，"
            f"判定来源={payload.get('latest_is_self_source') or '-'}，"
            f"今天={_yes_no(payload.get('latest_is_today'))}，"
            f"时间={payload.get('timestamp_text') or '-'}"
        )
        return (
            f"监听跳过「{payload.get('chat') or '-'}」："
            f"{_skip_reason_label(payload.get('reason'))}，"
            f"最后一条「{payload.get('latest_message') or ''}」，"
            f"依据={payload.get('latest_message_source') or '-'}，"
            f"自己消息判定={payload.get('latest_is_self_source') or '-'}"
        )
    if event == "watch.chats.start":
        mode = "自动回复" if payload.get("auto_reply") else "只监听"
        return (
            f"打开聊天「{payload.get('chat') or '-'}」，模式：{mode}。"
        )
    if event == "watch.chats.read":
        return (
            f"监听完成「{payload.get('chat') or '-'}」："
            f"可见消息 {payload.get('visible_message_count', 0)} 条，"
            f"监听到新消息 {payload.get('message_count', 0)} 条，"
            f"已回复 {payload.get('reply_count', 0)} 条，"
            f"最后一条「{payload.get('latest_message') or ''}」"
        )
    if event == "watch.chats.skipped":
        return (
            f"消息监听「{payload.get('chat') or '-'}」建立首轮基线："
            f"已读取可见消息 {payload.get('visible_message_count', 0)} 条，本轮不回复旧消息。"
        )
    if event == "watch.chats.message":
        return (
            f"消息监听「{payload.get('chat') or '-'}」本轮读到第 "
            f"{payload.get('index', '-')}/{payload.get('total', '-')} 条："
            f"{payload.get('sender') or '未知'}：{payload.get('text') or ''}"
            f"（自己发送={_yes_no(payload.get('is_self'))}，"
            f"时间={payload.get('timestamp_text') or '-'}）"
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

    # Friend check logs
    if event == "friend.check.start":
        mode = "仅检测" if payload.get("auto_accept") is False else "检测并自动同意"
        return f"开始检测新好友：{mode}，上限 {payload.get('limit', '-')} 人"
    if event == "friend.check.empty":
        return "好友检测完成：没有新的好友申请"
    if event == "friend.check.found":
        samples = payload.get("samples") or []
        sample_text = "、".join(str(s)[:20] for s in samples[:3])
        suffix = f"，示例「{sample_text}」" if sample_text else ""
        auto = "，已自动同意" if payload.get("auto_accept") else ""
        return f"好友检测完成：发现 {payload.get('count', 0)} 位新好友{auto}{suffix}"
    if event == "friend.check.failed":
        return f"好友检测失败：{payload.get('error', '')}"
    if event == "friend.add.start":
        return f"开始添加好友：{payload.get('number', '-')}，招呼「{payload.get('greetings', '')}」"
    if event == "friend.add.done":
        return f"添加好友请求已发送：{payload.get('number', '-')}"
    if event == "friend.add.failed":
        return f"添加好友失败：{payload.get('number', '-')}，{payload.get('error', '')}"

    # Moments logs
    if event == "moments.post.start":
        return (
            f"开始发送朋友圈：文字 {payload.get('text_length', 0)} 字，"
            f"图片/视频 {payload.get('media_count', 0)} 个"
        )
    if event == "moments.post.done":
        return f"朋友圈发送完成：图片/视频 {payload.get('media_count', 0)} 个"
    if event == "moments.post.failed":
        return f"朋友圈发送失败：{payload.get('error', '')}"

    # Diagnostic logs from wechat_api
    if event == "collect_visible_messages.collected":
        return (
            f"消息收集：原始节点 {payload.get('raw_node_count', 0)} 个，"
            f"消息 {payload.get('message_count', 0)} 条"
        )
    if event == "collect_visible_messages.group_detect":
        return f"群聊检测：{payload.get('chat_name', '-')}，is_group={payload.get('is_group', False)}"
    if event == "annotate_group_senders.right_click":
        return (
            f"群聊发送者检测：右键点击文本「{payload.get('text', '')[:30]}」，"
            f"区域={payload.get('rect', '-')}，位置={payload.get('pos', '-')}"
        )
    if event == "annotate_group_senders.select_chatList_failed":
        return f"群聊发送者检测：select_chatList失败：{payload.get('error', '')}"
    if event == "annotate_group_senders.menu_search_error":
        return f"群聊发送者检测：菜单搜索异常：{payload.get('error', '')}"
    if event == "annotate_group_senders.check_mode":
        return f"群聊发送者检测：已在多选模式={payload.get('already_selected', False)}，CheckBox={payload.get('checkbox_count', 0)}"
    if event == "annotate_group_senders.select_result":
        return f"群聊发送者检测：select_chatList={'成功' if payload.get('selected') else '失败'}"
    if event == "annotate_group_senders.texts_with_name":
        samples = payload.get("samples") or []
        sample_text = "、".join(str(s)[:30] for s in samples[:3])
        return f"群聊发送者检测：带名文本 {payload.get('count', 0)} 条，示例「{sample_text}」"
    if event == "annotate_group_senders.texts_without_name":
        samples = payload.get("samples") or []
        sample_text = "、".join(str(s)[:30] for s in samples[:3])
        return f"群聊发送者检测：纯文本 {payload.get('count', 0)} 条，示例「{sample_text}」"
    if event == "annotate_group_senders.sender_map":
        return f"群聊发送者检测：提取到 {payload.get('map_size', 0)} 个发送者"
    if event == "annotate_group_senders.applied":
        return f"群聊发送者检测：已应用 {payload.get('applied_count', 0)} 条消息的发送者"
    if event == "annotate_group_senders.failed":
        return f"群聊发送者检测失败：{payload.get('error', '')}"
    if event == "annotate_group_senders.no_chat_list":
        return "群聊发送者检测：未找到 chat_list 控件"
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

    if event == "uia.operation_probe":
        state = "可用" if payload.get("uia_exposed") else "不可用"
        return f"动作前 UIA 探测：{state}（{payload.get('reason') or '-'}）"

    if event == "worker.started":
        return f"服务已启动，轮询间隔 {payload.get('poll_interval', '-')} 秒。"
    if event == "worker.stopped":
        return "服务已停止。"
    if event == "task.duplicate_skipped":
        active_id = str(payload.get("active_task_id") or "")[:8] or "-"
        return f"已有待执行「{task_type}」任务，未重复入队：#{active_id}"
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
        "post_moments": "朋友圈发送任务",
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
    chat_summaries = _summarize_chat_summaries(result.get("chat_summaries"))
    if chat_summaries:
        parts.append(chat_summaries)
    return " ".join(parts)


def _summarize_chat_summaries(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    rows: list[str] = []
    for item in value[:4]:
        if not isinstance(item, dict):
            continue
        chat = str(item.get("chat") or "-")
        latest = _truncate_text(str(item.get("latest_message") or ""), 36)
        visible = item.get("visible_message_count", 0)
        replies = item.get("reply_count", 0)
        if latest:
            rows.append(f"{chat}：可见 {visible} 条，回复 {replies} 条，最后「{latest}」")
        else:
            rows.append(f"{chat}：可见 {visible} 条，回复 {replies} 条")
    return "；".join(rows)


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}…"


def _yes_no(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "未知"


def _skip_reason_label(value: Any) -> str:
    if str(value or "") == "latest_is_self_preview":
        return "会话列表最后一条是自己发送的"
    labels = {
        "friend_filter": "不符合目标会话过滤",
        "not_new_after_cursor": "游标之后没有新消息",
        "no_visible_message": "当前窗口没有可见消息",
        "not_today": "最后一条不是今天的消息",
        "latest_is_self": "最后一条是自己发送的",
        "empty_latest_message": "最后一条内容为空",
        "empty_agent_reply": "Agent 返回空回复",
        "already_processed_current_chat": "当前会话已处理过",
        "unknown_sender": "无法确认最后一条消息是谁发送的",
        "no_replyable_message": "没有可回复的消息",
    }
    return labels.get(str(value or ""), str(value or "未说明原因"))

def _decision_reason_label(value: Any) -> str:
    labels = {
        "replied": "已回复",
        "auto_reply_disabled": "仅监听未自动回复",
        "no_visible_message": "当前窗口没有可见消息",
        "not_today": "最后一条不是今天的消息",
        "latest_is_self_preview": "会话列表最后一条是自己发送的",
        "latest_is_self": "最后一条是自己发送的",
        "empty_latest_message": "最后一条消息为空",
        "empty_agent_reply": "Agent 返回空回复",
        "no_replyable_message": "没有可回复消息",
    }
    return labels.get(str(value or ""), str(value or "未说明"))


def _format_timestamp(value: str) -> str:
    if not value:
        return datetime.now().strftime("%H:%M:%S")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return parsed.astimezone().strftime("%H:%M:%S")
