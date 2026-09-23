from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import structlog

from . import logrole


def _client_version() -> str:
    from . import __version__

    return __version__

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
    "like_moments": "朋友圈点赞",
    "comment_moments": "朋友圈评论",
    "scan_moments": "朋友圈巡看",
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
        # 写日志的是哪个模块。**角色列全靠它**：没显式标 `对端=` 的行，靠模块名认对端
        # （见 `logrole`）。这个 processor 同时认 structlog 原生记录和 stdlib 转进来的
        # 外来记录（后者读 record.name），所以两条路上的行都能定到角色。
        structlog.stdlib.add_logger_name,
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
        record = payload.pop("_record", None)
        payload.pop("_from_structlog", None)
        # 角色（对端）：显式 `对端=` 优先，其次按写日志的模块认。两个键都要从 payload 里
        # 拿掉——它们是「这行日志的元信息」，不该跟着漏进消息尾巴的 kv 串里。
        logger_name = payload.pop("logger", None) or getattr(record, "name", None)
        role = logrole.role_of(logger_name, payload.pop(logrole.ROLE_KEY, None))
        exception = payload.pop("exception", None)
        stack = payload.pop("stack", None)
        message = _render_event(event, payload)
        prefix = f"{timestamp} [{_LEVEL_LABELS.get(level, level)}] [{role}]".strip()
        if exception:
            message = f"{message}\n{exception}"
        if stack:
            message = f"{message}\n{stack}"
        return f"{prefix} {message}"


#: 附件类型码（客户端 `SAVABLE_TYPES` 的口径）→ 日志页上的叫法。
_ATTACHMENT_KIND_TEXT = {"image": "图片", "file": "文件"}


def _kb(value: object) -> str:
    """字节数 → 「123 KB」/「1.5 MB」，看一眼就知道大不大。"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "-"
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{max(n + 1023, 1024) // 1024} KB"


def _attempt_tail(payload: dict[str, Any]) -> str:
    """一轮失败时的共同尾巴：第几次、什么原因、等了多久、接下来会怎样。

    第 1 次和第 2 次的**后果不一样**，看的人必须分得出来：第 1 次还会重连再试，
    第 2 次就是这一轮到此为止、消息留到下一轮重投。
    """
    attempt = int(payload.get("第几次") or 1)
    after = "重连后再试一次" if attempt == 1 else "两次都没跑通，这批消息留到下一轮重投"
    return (
        f"原因：{payload.get('原因') or '-'}"
        f"（第 {attempt} 次，已用 {payload.get('已用秒', 0)} 秒，{after}）"
    )


def _peer(payload: dict[str, Any]) -> str:
    """这行日志说的是哪个会话。``会话`` 是网关业务层的惯用键，``peer`` 是少数几处的老写法。"""
    return str(payload.get("会话") or payload.get("peer") or "-")


def _render_gateway_line(event: str, payload: dict[str, Any]) -> str | None:
    """网关业务层（入站轮询 → 派轮 → 出站）的日志。匹配不上返回 ``None``。

    这一层和 ``wechat_api`` 层不一样：``event`` 本身就已经是一句完整中文，缺的只是
    ``会话="张三"，reason="转人工：…"`` 那截 kv 尾巴。而「为什么没回这个人」整条链路
    恰恰全靠这几行看——它是唯一一段落到通用兜底、在实时日志里显示成 JSON 键值对的
    （用户 2026-08-29：不发的原因也该让 Windows 端看得明白）。

    ★ 这里的键就是 `_log.info(...)` 的第一个参数。改那边的措辞要同步改这里；
      对不上时落回通用兜底，和加这段之前一模一样——不会更糟，只是又变回 kv 串。
    """
    peer = _peer(payload)

    # ── 读：这一趟到底读到了什么 ────────────────────────────────
    if event == "读完这个会话":
        fresh = int(payload.get("新消息") or 0)
        latest = payload.get("最近一条") or "（空）"
        head = f"读完会话「{peer}」：读到 {payload.get('读到', 0)} 条"
        body = "没有新消息（游标之后一条都没有，所以这一轮不会回）" if fresh == 0 else f"其中 {fresh} 条是新的"
        return f"{head}，{body}，最近一条：{latest}"
    if event == "新消息":
        who = payload.get("谁") or "-"
        when = payload.get("时间") or "-"
        return f"新消息：「{peer}」{who} {when}：{payload.get('内容') or ''}"
    if event == "第一次读这个会话，只接尾部今天、对方发的那几条，更早的算历史不回溯":
        return (
            f"第一次读会话「{peer}」：读到 {payload.get('已读到', 0)} 条，"
            f"只处理尾部今天、对方发的 {payload.get('要处理', 0)} 条，更早的算历史不回溯"
        )
    if event == "这些会话正在人工接管，这轮不读":
        return f"有 {payload.get('数量', 0)} 个会话正在人工接管，这轮连读都不读（招聘老师本人在跟他聊）"
    if event == "名单较长，本轮只读其中一段":
        return (
            f"监听名单共 {payload.get('名单', 0)} 个会话，本轮只读其中 {payload.get('本轮', 0)} 个"
            f"（轮着来，免得一次开太多聊天窗口）"
        )
    if event == "名单里的这个会话读不到，先跳过":
        return (
            f"名单里的会话「{peer}」读不到，先跳过：{payload.get('原因') or '-'}"
            f"（名字要和微信里显示的一字不差）"
        )
    if event == "名单里的这个会话又能读了":
        return f"名单里的会话「{peer}」又能读了"
    if event == "入站取消息失败":
        return f"这一趟读消息失败：{payload.get('error') or '-'}（下一趟接着读，消息不会丢）"

    # ── 派轮：读到了新消息，但不一定会触发一轮 ──────────────────
    if event == "派发一轮给 AI 端":
        return f"派发一轮给 AI 端：「{peer}」，本轮 {payload.get('条数', 0)} 条消息"
    if event == "这几条全是自己说的，不触发回复，等对方开口":
        return (
            f"「{peer}」这 {payload.get('条数', 0)} 条全是自己说的（含刚发出去又被读回来的），"
            f"不触发回复，等对方开口"
        )
    if event == "上一轮还没跑完，这几条先等着":
        return f"「{peer}」上一轮还没跑完，这 {payload.get('条数', 0)} 条先等着，等收口后并进下一轮"

    # ── 和 AI 端的连接：断没断、什么时候换的票 ──────────────────
    if event == "已连接 AI 端":
        return "已连上 AI 端"
    if event == "已为该账号建立 AI 端连接":
        return "这个微信账号和 AI 端的连接已建立"
    if event == "读循环结束":
        # ★ 这条就是「和 AI 端断了」。断开时在跑的那一轮会当场失败，消息留着重投。
        return f"和 AI 端的连接断了：{payload.get('error') or '（对方关闭）'}（在跑的那一轮作废，消息留着重投）"
    if event == "AI 端连接不可用":
        return f"连不上 AI 端：{payload.get('error') or '-'}（这期间收到的消息都会攒着，通了再回）"
    if event == "已换取短票":
        return "已向业务后端换到新票，可以连 AI 端了"
    if event == "已读取本地联调票":
        return "用的是本地联调票（不是从业务后端换的）"
    if event == "换票请求失败":
        return f"换票失败，连不了 AI 端：{payload.get('error') or '-'}"
    if event == "旧会话接不回，改为新建":
        return f"「{peer}」原来那条会话接不回来了，改为新建一条（这个人之前聊的上下文会丢）"

    # ── 一轮进行中：AI 端在干什么（这一段是实时的，一轮几十秒都有下文）──
    if event == "喂给 AI 端":
        known = "对面已归位到候选人" if payload.get("认得这个人") else "还不知道对面是哪个候选人"
        again = f"，第 {payload.get('第几次')} 次尝试" if int(payload.get("第几次") or 1) > 1 else ""
        files = [
            f"{item.get('文件') or '-'}（{_ATTACHMENT_KIND_TEXT.get(item.get('类型'), '文件')}，{_kb(item.get('字节'))}）"
            for item in (payload.get("附件清单") or [])
            if isinstance(item, dict)
        ]
        with_files = f"，随行附件 {len(files)} 份：{'、'.join(files)}" if files else ""
        return (
            f"喂给 AI 端：「{peer}」，本轮 {payload.get('本轮', 0)} 条 + "
            f"历史 {payload.get('历史行', 0)} 行，{known}{again}{with_files}"
        )
    if event == "这一轮有附件没能带上":
        items = [
            f"{item.get('文件') or '-'}（{item.get('原因') or '-'}）"
            for item in (payload.get("清单") or [])
            if isinstance(item, dict)
        ]
        return (
            f"「{peer}」这一轮有 {payload.get('个数', len(items))} 份附件没能带上："
            f"{'、'.join(items) or '-'}（正文里已如实告诉 AI 端，不会上传）"
        )
    if event == "AI 端阶段":
        return f"AI 端：{payload.get('阶段') or '-'}"
    if event == "AI 端还在跑这一轮":
        return (
            f"AI 端还在跑这一轮：「{peer}」，已 {payload.get('已用秒', 0)} 秒"
            f"（超过 {payload.get('上限秒', 0)} 秒算超时，会重连再试一次）"
        )
    if event == "AI 端这一轮跑完了":
        got = "拿到卡片" if payload.get("有卡片") else "但没有卡片（出站那步会说明为什么不回）"
        return f"AI 端这一轮跑完了：「{peer}」，用时 {payload.get('已用秒', 0)} 秒，{got}"
    if event == "AI 端认领了材料":
        items = []
        for item in payload.get("清单") or []:
            if not isinstance(item, dict):
                continue
            upload = (
                f"有授权原话，会上传到后端，识别到 {item.get('字段数', 0)} 个字段"
                if item.get("有授权原话")
                else "没有授权原话，不会上传，要传得专员自己传"
            )
            items.append(f"{item.get('文件') or '-'} → {item.get('类型') or '-'}（{upload}）")
        missed = payload.get("未认领") or []
        tail = f"；未认领：{'、'.join(str(n) for n in missed)}（这些不会上传）" if missed else ""
        return f"AI 端认领了「{peer}」发来的材料：{'、'.join(items) or '-'}{tail}"
    if event == "AI 端没认领这一轮的附件":
        sent = "、".join(str(n) for n in (payload.get("送去") or [])) or "-"
        return (
            f"AI 端没认领「{peer}」这一轮的附件（{sent}）：不会上传到后端"
            f"（多半是没认出是简历 / 身份证 / 银行卡，或图太糊；要传得专员自己传）"
        )
    if event == "AI 端返回错误":
        return (
            f"AI 端返回错误：「{peer}」，错误码 {payload.get('code') or '-'}，"
            f"原因：{payload.get('原因') or '-'}（用时 {payload.get('已用秒', 0)} 秒；"
            f"这一轮没有结论，消息留着下一轮重投）"
        )
    if event == "这一轮没跑通":
        return f"这一轮没跑通：「{peer}」，{_attempt_tail(payload)}"
    if event == "AI 端不认这条会话了，清掉重来":
        return f"AI 端不认「{peer}」这条会话了，已清掉重建，{_attempt_tail(payload)}"

    # ── 出站：AI 给了结果，但不一定发得出去 ────────────────────
    if event == "本轮不回复":
        return f"本轮不回复「{peer}」，原因：{payload.get('reason') or '-'}"
    if event == "出站被守门拦下":
        return f"出站被守门拦下：「{peer}」，原因：{payload.get('reason') or '-'}"
    if event == "已投递回复":
        return f"已投递回复：「{peer}」，共 {payload.get('segments', 0)} 条消息"
    if event == "按 AI 端提案拉黑":
        return "按 AI 端提案拉黑该联系人（AI 判定对方明确拒绝）"
    if event == "状态回写跳过：本机不知道这个会话对应哪个候选人":
        return f"状态回写跳过：本机不知道会话「{peer}」对应哪个候选人（等他的会话名归位后才写得回去）"

    # ── 欠账：没结论的那几批 ──────────────────────────────────
    if event == "这一轮没把话说出去，本批留在待处理队列等重投":
        counted = "计一次失败" if payload.get("计入失败") else "不计失败（这类拦截会自己过期）"
        return (
            f"这一轮没把话说出去，本批留在待处理队列等重投：{payload.get('原因') or '-'}"
            f"，{counted}，已重试 {payload.get('已重试', 0)} 次"
        )
    if event == "从待处理队列补投":
        return f"从待处理队列补投 {payload.get('批次', 0)} 批"

    return None


def _render_event(event: str, payload: dict[str, Any]) -> str:
    task_type = _TASK_LABELS.get(str(payload.get("task_type") or ""), "未知任务")
    task_id = _short_id(payload.get("task_id") or payload.get("active_task_id"))

    # 网关业务层先认一遍：那一层的事件名是整句中文，走通用兜底会显示成 kv 串。
    gateway_line = _render_gateway_line(event, payload)
    if gateway_line is not None:
        return gateway_line

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
        # 版本号印在句子里：用户贴回来的就是这一行，「装的是不是最新版」不用再问（2026-09-18）。
        return f"微信任务执行失败：{task_type} #{task_id}，原因：{payload.get('error') or '-'}（客户端 v{_client_version()}）"
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
    if event == "friend.add.already":
        return f"这个号早就是好友了，不用再加：{payload.get('number')}"
    if event == "friend.add.done":
        who = payload.get("session_name") or payload.get("name")
        suffix = f"（{who}）" if who else ""
        if payload.get("already_friend"):
            return f"这个号早就是好友了：{payload.get('number')}{suffix}"
        # 只是「申请发出去了」，不是「已经是好友了」——对方点同意才算数。
        return f"好友申请已提交：{payload.get('number')}{suffix}"
    if event == "friend.add.profile_missing":
        if payload.get("rendered"):
            return f"名片读到了但加不了：{payload.get('number')}（既没有「添加到通讯录」也没有「发消息」）"
        return f"等不到这个号的名片：{payload.get('number')}（可能号码不对，也可能微信版式变了）"
    if event == "friend.add.remark_edit_missing":
        return f"申请窗里找不到「修改备注」栏，这次不设备注：{payload.get('number')}"
    if event == "friend.add.fallback":
        # reason 必须打出来：2026-09-22 客户机上加好友连号都没输进去，日志里却只有
        # 一句「改走备用流程」，到底卡在「面板打不开」还是「搜索框用不了」全靠猜。
        reason = payload.get("reason")
        tail = f"，原因：{reason}" if reason else ""
        return f"添加好友改走备用流程：{payload.get('number')}（这次读不到对方昵称）{tail}"
    if event == "friend.panel.entry_missing":
        return f"快捷操作菜单里没有「{payload.get('label')}」：{payload.get('seen') or '-'}"
    if event == "friend.add.failed":
        return f"添加好友失败：{payload.get('number')}，原因：{payload.get('error') or '-'}"
    if event == "friend.check.start":
        mode = "并同意" if payload.get("verify") else "仅检查"
        return f"开始检查好友申请：{mode}，最多 {payload.get('limit')} 条"
    if event == "friend.check.empty":
        return "好友申请检查完成：没有新的申请"
    if event == "friend.check.found":
        mode = f"，这一趟通过了 {payload.get('accepted', 0)} 个" if payload.get("auto_accept") else ""
        return f"好友申请检查完成：「新的朋友」里有 {payload.get('count', 0)} 条{mode}"
    if event == "friend.check.no_section":
        return "好友申请检查：通讯录里没找到「新的朋友」分区（微信版式变了？）"
    if event == "friend.check.accepted":
        via = payload.get("via")
        return f"已通过好友申请：{payload.get('name') or '-'}" + (f"（{via}）" if via else "")
    if event == "friend.check.verify_button_missing":
        return (
            f"这条申请右侧没认出「通过」按钮，跳过：{payload.get('entry') or '-'}"
            f"（面板上的按钮：{payload.get('buttons') or '-'}）"
        )
    if event == "friend.check.confirm_fallback_button":
        return (
            f"确认框里没有认得出的「确定」，点了最后一颗不是「取消」的按钮「{payload.get('picked') or '-'}」"
            f"（框上的按钮：{payload.get('buttons') or '-'}）"
        )
    if event == "friend.check.verify_reclicked":
        return f"点了「通过」等了 {payload.get('waited') or '-'} 秒什么都没出现、按钮还在，再点一次"
    if event == "friend.check.confirm_missing":
        return (
            f"[v{_client_version()}] 点了「{payload.get('button') or '通过'}」但没等到确认框，这一趟停手：{payload.get('entry') or '-'}，"
            f"原因：{payload.get('error') or '-'}"
            f"（此刻桌面顶层窗口：{payload.get('windows') or '-'}；含不可见的微信窗口：{payload.get('hidden') or '-'}；"
            f"详情面板按钮：{payload.get('buttons') or '-'}）"
        )
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
    if event == "moments.post.layout_sniffed_docked":
        return f"朋友圈开出来的是停靠进主窗口的新版式（微信版本 {payload.get('version') or '未读到'}），改走新版流程"
    if event == "moments.post.docked.published":
        closed = "已收起" if payload.get("panel_closed") else "20 秒内没收起，是否发出去请到朋友圈核对"
        return f"新版朋友圈已点「发表」，编辑面板{closed}"
    if event == "moments.post.docked.screenshot_failed":
        return f"截「发表」格失败，改按比例点相机格：{payload.get('error') or '-'}"
    if event == "moments.post.docked.tile_fallback":
        return f"截图里没认出相机格，按比例点 ({payload.get('x')}, {payload.get('y')})"
    if event == "moments.post.docked.close_failed":
        return f"发完关朋友圈面板没成（不影响结果）：{payload.get('error') or '-'}"
    if event == "moments.like.start":
        return f"开始给朋友圈点赞：只点今天的，最多 {payload.get('limit', 0)} 条"
    if event == "moments.like.done":
        return (
            f"朋友圈点赞完成：点了 {payload.get('liked', 0)} 条，"
            f"看过 {payload.get('scanned', 0)} 条，已点过 {payload.get('already_liked', 0)} 条"
        )
    if event == "moments.like.failed":
        return f"朋友圈点赞失败：{payload.get('error') or '-'}"
    if event == "moments.comment.start":
        return f"开始给朋友圈评论：要评 {payload.get('count', 0)} 条，只评今天的"
    if event == "moments.comment.done":
        return (
            f"朋友圈评论完成：评了 {payload.get('commented', 0)} 条，"
            f"看过 {payload.get('scanned', 0)} 条，没认出 {payload.get('missed', 0)} 条"
        )
    if event == "moments.comment.failed":
        return f"朋友圈评论失败：{payload.get('error') or '-'}"
    if event == "moments.scan.start":
        return (
            f"开始巡看朋友圈：只看新动态，最多收 {payload.get('limit', 0)} 条，"
            f"顺手点赞 {payload.get('like_limit', 0)} 个"
        )
    if event == "moments.scan.done":
        return (
            f"朋友圈巡看完成：新动态 {payload.get('new', 0)} 条，点了 {payload.get('liked', 0)} 个赞，"
            f"已点过 {payload.get('already_liked', 0)} 个"
        )
    if event == "moments.scan.failed":
        return f"朋友圈巡看失败：{payload.get('error') or '-'}"
    if event == "moments.poll.done":
        return (
            f"朋友圈巡看一趟：新动态 {payload.get('new', 0)} 条，点了 {payload.get('liked', 0)} 个赞，"
            f"已点过 {payload.get('already_liked', 0)} 个"
        )
    if event == "moments.poll.failed":
        return f"朋友圈巡看这趟没成：{payload.get('error') or '-'}"
    if event == "moments.poll.skip":
        return f"这趟朋友圈巡看跳过：{payload.get('reason') or '-'}"
    if event == "moments.poll.comment.failed":
        return f"交给劳小智写评论没成：{payload.get('error') or '-'}"
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
    if event == "chat.attachment.save.start":
        return f"开始另存对方发来的附件：「{payload.get('friend') or '-'}」，气泡：{payload.get('text') or '-'}"
    if event == "chat.attachment.save.done":
        return (
            f"附件已另存：「{payload.get('friend') or '-'}」，"
            f"{payload.get('name') or '-'}（{payload.get('bytes', 0)} 字节）"
        )
    if event == "chat.voice.row":
        state = "已转成文字" if payload.get("ready") else "还没转成文字"
        return f"读到一条语音（{state}）：{payload.get('raw') or '（名称为空）'}"
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
    if event == "gateway.backlog.changed":
        # 「积压」= 微信里的消息已经读进来了、但这一轮没回出去（多半是 AI 端没给结果）。
        # 消息不会丢也不会重复回：它们记在待处理队列里，下一轮自动重投。
        # 写全一点是有意的——状态栏那格原来只放得下「欠 N 批」，没人看得懂（用户 2026-08-21）。
        now = int(payload.get("backlog") or 0)
        before = int(payload.get("previous") or 0)
        if now == 0:
            return (
                f"积压清空了：先前那 {before} 批读到但没回出去的微信消息，"
                f"现在都已经回出去了"
            )
        if now > before:
            return (
                f"有 {now} 批微信消息已经读进来、但还没回出去"
                f"（AI 端这几轮没给出结果，已记在待处理队列里，下一轮自动重投）。"
                f"消息不会丢也不会重复回，读新消息照常进行；"
                f"要是一直不降，多半是 AI 端出了问题，往上翻找带 [警告] 的那几行"
            )
        return (
            f"待回的积压从 {before} 批降到 {now} 批（补投成功 {before - now} 批），"
            f"剩下这些还在等 AI 端给结果"
        )
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
    if event == "wechat.edit_not_enabled":
        # 「第二次就好了」那件事的第一现场：切完聊天窗口，输入框还没变成可操作。
        return (
            f"微信输入框还没准备好（刚切过去，还不能打字），等一下重来一次：这次要做的是"
            f"{'发文件' if payload.get('op') == 'send_files' else '发消息'}"
        )
    if event == "wechat.edit_missing":
        # 「非正常好友,无法发送消息」的第一现场：切过去 0.1 秒内没找到输入框，好友其实是真的。
        return (
            f"微信没找到「{payload.get('friend') or '-'}」的聊天输入框（微信报「非正常好友」，"
            f"多半是聊天被弹成独立窗口或界面还没切好），等一下重来一次：这次要做的是"
            f"{'发文件' if payload.get('op') == 'send_files' else '发消息'}"
        )
    if event == "wechat.separate_window_closed_before_retry":
        return f"重来前先把「{payload.get('friend') or '-'}」被弹出去的独立聊天窗口关掉"
    if event == "wechat.edit_retry":
        waited = "输入框已经可用了" if payload.get("等到可用") else "没等到它变可用，仍然再试一次"
        return f"重来一次：{waited}"
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
        # 租约续不上 = 这条任务可能已经被另一个执行器接走。执行器**不会**中途停手
        # （微信操作做到一半没法安全打断），但做完后的结果会因为归属校验写不进库。
        return (
            f"任务续租失败，做完后结果可能记不上：任务 #{_short_id(payload.get('task_id'))}"
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
