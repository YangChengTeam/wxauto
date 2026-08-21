from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import structlog


_LEVEL_LABELS = {
    "debug": "调试",
    "info": "信息",
    "warning": "警告",
    "error": "错误",
    "critical": "严重",
}

_TASK_LABELS = {
    "search_contacts": "搜索联系人",
    "get_contact_profile": "读取联系人资料",
    "list_group_members": "读取群成员",
    "add_friend": "添加好友",
    "check_new_friends": "检查好友申请",
    "accept_new_friends": "同意好友申请",
    "send_message": "发送消息",
    "send_files": "发送文件",
    "scan_messages": "监听消息",
    "get_unread_messages": "汇总未读消息",
    "watch_chats": "监听多个会话",
    "read_chat_sessions": "读取会话列表",
    "read_chat_history": "读取聊天记录",
    "read_visible_messages": "读取可见消息",
    "post_moments": "发布朋友圈",
    "read_recent_moments": "读取最近朋友圈",
}

#: 日志目录里留多少天。半个月足够回溯一次「上周三那单怎么回事」，又不会把盘吃掉。
KEEP_DAYS = 30


class DailyFileHandler(logging.Handler):
    """一天一个日志文件：``logs/gateway-2026-08-15.log``。

    **为什么不用 ``TimedRotatingFileHandler``**：它把「当天」那份写在 ``gateway.log`` 里、
    等隔天才改名带上日期——于是目录里最新的一天恰恰是唯一不带日期的那个文件，而人按日期
    找日志时，最想要的就是今天这份（用户 2026-08-15：「里面应该是按日期天的日志」）。
    这里按记录自己的时间戳决定写哪个文件，跨零点自动换，名字从落笔那一刻就是对的。
    """

    def __init__(self, directory: Path, *, prefix: str = "gateway",
                 keep_days: int = KEEP_DAYS, encoding: str = "utf-8") -> None:
        super().__init__()
        self.directory = Path(directory)
        self.prefix = prefix
        self.keep_days = max(1, keep_days)
        self.encoding = encoding
        self._day = ""
        self._stream: Any = None
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, day: str) -> Path:
        return self.directory / f"{self.prefix}-{day}.log"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(record.created))
            if self._stream is None or day != self._day:
                self._open(day)
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:  # noqa: BLE001 - 日志写不出去不能把业务带崩
            self.handleError(record)

    def _open(self, day: str) -> None:
        if self._stream is not None:
            self._stream.close()
        self._stream = self.path_for(day).open("a", encoding=self.encoding, errors="replace")
        self._day = day
        self._prune()

    def _prune(self) -> None:
        """只留最近 ``keep_days`` 天。文件名带 ISO 日期，按名字排就是按时间排。"""
        files = sorted(self.directory.glob(f"{self.prefix}-*.log"))
        for stale in files[:-self.keep_days]:
            try:
                stale.unlink()
            except OSError:
                pass  # 正被别的进程占着就下次再说，删不掉不是错

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """网关与微信任务执行器的中文精简日志。

    ``log_dir`` 给了就一天一个文件写进去（见 ``DailyFileHandler``）。
    """
    _configure_redirected_stream_encoding()
    level_value = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level_value)

    processors = [
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=ChineseLogRenderer(),
        foreign_pre_chain=processors,
    )

    console = logging.StreamHandler()
    console.setLevel(level_value)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir is not None:
        file_handler = DailyFileHandler(log_dir)
        file_handler.setLevel(level_value)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


class ChineseLogRenderer:
    def __call__(
        self,
        logger: logging.Logger,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> str:
        payload = dict(event_dict)
        timestamp = str(payload.pop("timestamp", ""))
        level = str(payload.pop("level", method_name or "info")).lower()
        event = str(payload.pop("event", ""))
        payload.pop("_record", None)
        payload.pop("_from_structlog", None)
        exception = payload.pop("exception", None)
        stack = payload.pop("stack", None)
        message = _render_event(event, payload)
        prefix = f"{timestamp} [{_LEVEL_LABELS.get(level, level)}]".strip()
        if exception:
            message = f"{message}\n{exception}"
        if stack:
            message = f"{message}\n{stack}"
        return f"{prefix} {message}"


def _render_event(event: str, payload: dict[str, Any]) -> str:
    task_type = _TASK_LABELS.get(str(payload.get("task_type") or ""), "未知任务")
    task_id = _short_id(payload.get("task_id") or payload.get("active_task_id"))

    if event == "worker.started":
        return f"微信任务执行器已启动，轮询间隔 {payload.get('poll_interval')} 秒"
    if event == "worker.stopped":
        return "微信任务执行器已停止"
    if event == "task.running":
        return f"微信任务开始执行：{task_type} #{task_id}"
    if event == "task.succeeded":
        return f"微信任务执行成功：{task_type} #{task_id}"
    if event == "task.blocked":
        return f"微信任务被阻止：{task_type} #{task_id}，原因：{payload.get('error') or '-'}"
    if event == "task.failed":
        return f"微信任务执行失败：{task_type} #{task_id}，原因：{payload.get('error') or '-'}"
    if event == "task.duplicate_skipped":
        return f"跳过重复待执行任务：{task_type}，已有任务 #{task_id}"

    if event == "contact.search.start":
        return (
            f"开始搜索联系人：{payload.get('query') or '-'}，"
            f"最多 {payload.get('limit', 0)} 条"
        )
    if event == "contact.search.done":
        return (
            f"联系人搜索完成：匹配 {payload.get('match_count', 0)} 条，"
            f"精确匹配 {payload.get('exact_count', 0)} 条"
        )
    if event == "contact.profile.start":
        return f"开始读取联系人资料：{payload.get('friend') or '-'}"
    if event == "contact.profile.done":
        return f"联系人资料读取完成：{payload.get('friend') or '-'}"
    if event == "group.members.start":
        return f"开始读取群成员：{payload.get('group') or '-'}"
    if event == "group.members.done":
        return (
            f"群成员读取完成：{payload.get('group') or '-'}，"
            f"共 {payload.get('member_count', 0)} 人"
        )
    if event == "friend.add.start":
        return f"开始添加好友：{payload.get('number')}"
    if event == "friend.add.done":
        # 只是「申请发出去了」，不是「已经是好友了」——对方点同意才算数。
        who = payload.get("session_name") or payload.get("name")
        return f"好友申请已提交：{payload.get('number')}" + (f"（{who}）" if who else "")
    if event == "friend.add.fallback":
        return f"添加好友改走备用流程：{payload.get('number')}（这次读不到对方昵称）"
    if event == "friend.add.failed":
        return f"添加好友失败：{payload.get('number')}，原因：{payload.get('error') or '-'}"
    if event == "friend.check.start":
        mode = "并同意" if payload.get("verify") else "仅检查"
        return f"开始检查好友申请：{mode}，最多 {payload.get('limit')} 条"
    if event == "friend.check.empty":
        return "好友申请检查完成：没有新的申请"
    if event == "friend.check.found":
        mode = "，已执行同意" if payload.get("auto_accept") else ""
        return f"好友申请检查完成：发现 {payload.get('count', 0)} 条{mode}"
    if event == "friend.check.failed":
        return f"好友申请检查失败：{payload.get('error') or '-'}"

    if event == "moments.post.start":
        return (
            f"开始发布朋友圈：文字 {payload.get('text_length', 0)} 字，"
            f"媒体 {payload.get('media_count', 0)} 个"
        )
    if event == "moments.post.done":
        return f"朋友圈发布成功：媒体 {payload.get('media_count', 0)} 个"
    if event == "moments.post.failed":
        return f"朋友圈发布失败：{payload.get('error') or '-'}"
    if event == "moments.post.media.missing":
        return f"朋友圈媒体文件不存在：{_json(payload.get('paths'))}"
    if event == "chat.sessions.read":
        return (
            f"会话列表读取完成：共 {payload.get('session_count', 0)} 个，"
            f"当前会话：{payload.get('selected_session') or '-'}"
        )
    if event == "messages.unread.read":
        return (
            f"未读消息汇总完成：{payload.get('session_count', 0)} 个会话，"
            f"共 {payload.get('total_unread', 0)} 条"
        )
    if event == "chat.history.start":
        return (
            f"开始读取聊天记录：{payload.get('friend') or '-'}，"
            f"最多 {payload.get('limit', 0)} 条"
        )
    if event == "chat.history.main_window_fallback":
        return (
            f"主窗口里读不了「{payload.get('friend') or '-'}」，改用独立聊天窗口："
            f"{payload.get('error') or '-'}"
        )
    if event == "chat.history.done":
        return (
            f"聊天记录读取完成：{payload.get('friend') or '-'}，"
            f"共 {payload.get('message_count', 0)} 条"
        )
    if event == "files.send.start":
        return (
            f"开始发送文件给「{payload.get('friend') or '-'}」："
            f"共 {payload.get('file_count', 0)} 个"
        )
    if event == "files.send.done":
        return (
            f"文件发送完成：「{payload.get('friend') or '-'}」，"
            f"共 {payload.get('file_count', 0)} 个"
        )
    if event == "uia.operation_probe":
        status = "可用" if payload.get("uia_exposed") else "不可用"
        return f"微信 UIA 探测完成：{status}，原因：{payload.get('reason') or '-'}"
    if event == "uia.close.no_window_pattern":
        # 这一条是「没做什么」的留痕：pywinauto 关不掉这个控件，原本会按 ESC 兜底，
        # 而 ESC 在微信 4.x 里就是把微信收进托盘。宁可让面板留在屏幕上。
        return "有个面板关不掉，按原样留着了（没按 ESC——那会把微信收进托盘）"
    if event == "uia.operation_probe.after_restore":
        status = "可用" if payload.get("uia_exposed") else "仍不可用"
        return (
            f"微信被最小化了，已把窗口拉起来重新探测：{status}"
            + (f"，原因：{payload.get('reason')}" if payload.get("reason") else "")
        )

    # ── 发送后的落地核对（真机上「以为发了其实没发」全靠这几条留痕）──────
    if event == "message.send.stuck_in_input":
        return (
            f"消息卡在输入框里没发出去：「{payload.get('friend') or '-'}」，"
            f"输入框里还剩 {payload.get('left', 0)} 个字，对方没有收到"
        )
    if event == "message.send.input_state":
        return f"发送前后核对输入框：还剩 {payload.get('text', 0)} 个字"
    if event == "message.send.input_probe_failed":
        return f"读不到输入框，这次发没发出去判定不了：{payload.get('error') or '-'}"
    if event == "message.send.retry_failed":
        return f"补按发送键失败，按「没发出去」处理：{payload.get('error') or '-'}"
    if event == "files.send.input_state":
        return (
            f"发送前后核对输入框：还剩文字 {payload.get('text', 0)} 个字、"
            f"附件 {payload.get('children', 0)} 个"
        )
    if event == "files.send.input_probe_failed":
        return f"读不到输入框，这次发没发出去判定不了：{payload.get('error') or '-'}"
    if event == "files.send.retry_send":
        return f"文件还在输入框里，第 {payload.get('attempt', 0)} 次补按发送键：{payload.get('key') or '-'}"
    if event == "files.send.retry_failed":
        return f"补按发送键失败，按「没发出去」处理：{payload.get('error') or '-'}"

    if event == "window.restored":
        forced = "（强制）" if payload.get("forced") else ""
        return f"已把微信窗口拉回前台{forced}"
    if event == "window.restore_failed":
        return f"拉起微信窗口失败：{payload.get('error') or '-'}"

    if event == "task.yielded":
        return f"微信任务让出这一轮，稍后接着做：{task_type} #{task_id}"
    if event == "worker.lease_lost":
        # 租约丢了 = 这条任务可能已经被另一个执行器接走，**再往下写就是两个人同时操作微信**。
        return (
            f"执行器租约丢失，已停手：任务 #{_short_id(payload.get('task_id'))}"
            f"（执行器租约 {'在' if payload.get('worker_lease') else '丢'}、"
            f"任务租约 {'在' if payload.get('task_lease') else '丢'}）"
        )
    if event == "wechat.driver.unknown":
        return f"忽略未知微信驱动：{payload.get('driver') or '-'}"
    if event == "collect_visible_messages.collected":
        return (
            f"可见消息采集完成：会话「{payload.get('chat_name') or '-'}」，"
            f"消息 {payload.get('message_count', 0)} 条"
        )
    if event == "collect_visible_messages.group_detect":
        kind = "群聊" if payload.get("is_group") else "单聊"
        return f"会话类型识别完成：「{payload.get('chat_name') or '-'}」为{kind}"
    extras = _format_extras(payload)
    return f"{event}{extras}" if event else extras.lstrip("，")


def _format_extras(payload: dict[str, Any]) -> str:
    ignored = {"logger", "positional_args"}
    values = [f"{key}={_json(value)}" for key, value in payload.items() if key not in ignored]
    return f"，{'，'.join(values)}" if values else ""


def _short_id(value: Any) -> str:
    text = str(value or "-")
    return text[:8]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _configure_redirected_stream_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure) and not stream.isatty():
            reconfigure(encoding="utf-8", errors="replace")
