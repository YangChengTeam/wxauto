"""入站：轮询取消息 → 去重 → 去抖聚合 → 串行派发一轮对话。

**入站只能轮询**（``CLAUDE.md`` §4.3）：``wechat_api`` 的 watch/unread 都是有界时长任务，
跑完就返回，没有「来消息就回调」这回事。所以这里自己转一个常驻循环。

为了让整层在 Linux 上可测，时间从外部注入、``tick()`` 是可单步调用的一趟；
``run()`` 只是「反复 tick + 睡一会」。测试永远不需要真的等去抖窗口过去。
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import structlog

from .. import logrole
from .guard import looks_like_rejection
from .pacing import jittered

_log = structlog.get_logger(__name__)

#: 跟 AI 端打交道的那几行（那条 WS：派轮 / 卡片 / 指令 / 回执 / 名单 / 能力）。
_ai = _log.bind(**{logrole.ROLE_KEY: logrole.AI})
#: 跟业务后端打交道的那几行（Client Token 走的 HTTP）。
_backend = _log.bind(**{logrole.ROLE_KEY: logrole.BACKEND})
#: 这台电脑自己的事，不涉及任何对端。
_local = _log.bind(**{logrole.ROLE_KEY: logrole.LOCAL})

PeerKey = tuple[str, str]  # (account_id, peer_id)

#: 只有专员本人说话时最多留几条当上下文。留着是为了下一轮别重复问他问过的，
#: 但不能无上限攒——他自己聊一下午，缓冲和 prompt 都会被撑爆。
_SELF_CONTEXT_LIMIT = 10

#: 待处理批次最多重投几次。AI 端长时间不可用时不该无限重试同一批，但也不能几分钟就放弃：
#: 轮询间隔是分钟级，20 次差不多覆盖一两个小时的中断。
PENDING_MAX_ATTEMPTS = 20

#: 待处理批次的保鲜期。队列是**短期**的（需求文档 §2.4）——隔了一天再回过去更奇怪。
#: 取 24h 是为了覆盖「晚上快下班时收到、失败了、第二天进工作时间窗才补上」这条真实路径。
PENDING_MAX_AGE_HOURS = 24


@dataclass(frozen=True)
class InboundMessage:
    """一条入站微信消息。"""

    account_id: str
    peer_id: str
    msg_id: str
    text: str
    at: datetime
    from_self: bool = False
    """True = 这条是**招聘专员本人手打发出去的**（人工接管）。必须一起喂给 AI，
    否则它会重复问专员已经问过的问题（``CLAUDE.md`` §5.2）。"""
    peer_name: str = ""
    at_text: str = ""
    """微信自己标的时间（「18:28」「昨天 20:11」），来自聊天记录里的时间分隔行。

    有它就用它：``at`` 是**网关读到这条的时刻**，不是对方发出的时刻。改成直接读会话
    之后（不再依赖未读），读到的很可能是几分钟前甚至昨天的消息——把读取时刻当发送时刻
    喂给模型，它就会对着一条昨晚的消息说「你刚才说」。"""
    message_type: str = "text"
    """``text`` / ``voice``。语音是微信自动转文字后读到的（``text`` 已是转写正文），
    要让模型知道这是听写来的（可能有错字），上报后端时 ``messageType`` 也照实填。"""

    @property
    def key(self) -> PeerKey:
        return (self.account_id, self.peer_id)


def message_to_row(message: InboundMessage) -> dict:
    """落盘用的最小形态（``account_id``/``peer_id`` 存在行上，不重复写进 payload）。"""
    return {
        "msg_id": message.msg_id,
        "text": message.text,
        "at": message.at.isoformat(),
        "from_self": message.from_self,
        "peer_name": message.peer_name,
        "at_text": message.at_text,
        "message_type": message.message_type,
    }


def message_from_row(account_id: str, peer_id: str, row: dict) -> InboundMessage:
    at_raw = str(row.get("at") or "")
    try:
        at = datetime.fromisoformat(at_raw)
    except ValueError:
        at = datetime.now(timezone.utc)
    return InboundMessage(
        account_id=account_id,
        peer_id=peer_id,
        msg_id=str(row.get("msg_id") or ""),
        text=str(row.get("text") or ""),
        at=at,
        from_self=bool(row.get("from_self")),
        peer_name=str(row.get("peer_name") or peer_id),
        at_text=str(row.get("at_text") or ""),
        message_type=str(row.get("message_type") or "text"),
    )


class MessageSource(Protocol):
    """入站消息来源。生产实现投递队列任务读微信；测试用假实现。"""

    async def fetch(self, account_id: str) -> Sequence[InboundMessage]: ...


#: 一批入站消息跑完之后的去向。`TurnHandler` 的返回值上带 `follow_up` 就按它办，
#: 不带（返回 None / 老实现）= 有结论了，销账。
#:
#: ★ 为什么要分这两档：原来只要不抛异常就当成功销账，于是「AI 端报错」和「额度用完被
#: 守门拦下」这两种**一条都没发出去**的情况，候选人的消息被永久丢弃，而唯一的积压指示灯
#: 同时归零——现场看不出丢过东西（2026-08-18 审计 P0-4，两条路都实跑复现过）。
FOLLOW_UP_RETRY = "retry"
"""没跑出结论（AI 端报错、重试后仍连不上）——留着重投，**且计一次失败**（有上限，会放弃）。"""
FOLLOW_UP_DEFER = "defer"
"""被规则挡下（额度用完、不在时间窗）——留着重投，**不计失败**：这类拦截一定会自己过期。"""

HISTORY_MAX_AGE_DAYS = 14
"""聊天记录留多久（喂给 AI 端当背景用的，不是聊天归档）。

这份记录里是候选人对话原文——真实 PII 落在专员自己的电脑上。留够判「这一步走完没有」
就行，一台跑几个月的机器不该把它攒成一个越来越大的本地文件。
"""

TurnHandler = Callable[[str, str, str, Sequence[InboundMessage]], Awaitable[Any]]
"""(account_id, peer_id, transcript, messages) -> 结果对象（可带 `follow_up`）或 None"""


def format_transcript(messages: Iterable[InboundMessage]) -> str:
    """拼成 AI 端约定的入站上下文格式（064 design §2）。

    说话人只有两类，且**不暴露 wxid**——AI 端不需要知道对方的微信标识。
    """
    lines = []
    for message in messages:
        who = "专员本人" if message.from_self else "对方"
        when = message.at_text or f"{message.at:%H:%M}"
        lines.append(f"[{who} · {when}] {voice_marked(message.message_type, message.text)}")
    return "\n".join(lines)


def voice_marked(message_type: str, text: str) -> str:
    """语音消息在上下文里标出来：正文是微信听写的，模型要知道可能有错字、别逐字较真。"""
    if message_type == "voice" and text:
        return f"（语音转文字）{text}"
    return text


def format_history(rows: Iterable[dict]) -> str:
    """把存下来的聊天记录拼成与 `format_transcript` **一模一样**的行格式。

    两边必须逐字同形：AI 端那头是同一个解析器（`context.mjs` 的 `parseTranscript`），
    格式一分岔，历史里的话就会被当成「对方刚说的」。
    """
    lines = []
    for row in rows:
        who = "专员本人" if row.get("from_self") else "对方"
        when = str(row.get("at_text") or "")
        if not when:
            at = str(row.get("at") or "")
            when = at[11:16] if len(at) >= 16 else at
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{who} · {when}] {voice_marked(str(row.get('message_type') or ''), text)}")
    return "\n".join(lines)


@dataclass
class _Buffer:
    messages: list[InboundMessage] = field(default_factory=list)
    last_at: datetime | None = None
    batch_ids: set[str] = field(default_factory=set)
    """这些消息落盘时用的批次号（070）。派发成功后按它销账，失败按它记重试。

    一次去抖可能横跨好几趟轮询，于是一个缓冲里会攒着多个批次——所以是集合不是单值。
    """


class Debouncer:
    """按联系人缓冲消息，静默满 ``window`` 秒才放行。

    对方连发三条短消息应该合成一轮回复，否则会回三次——像个机器人。
    """

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._buffers: dict[PeerKey, _Buffer] = {}

    def add(self, message: InboundMessage, now: datetime, batch_id: str | None = None) -> None:
        buffer = self._buffers.setdefault(message.key, _Buffer())
        buffer.messages.append(message)
        buffer.last_at = now
        if batch_id:
            buffer.batch_ids.add(batch_id)

    def readd(
        self,
        key: PeerKey,
        messages: Sequence[InboundMessage],
        now: datetime,
        batch_ids: Iterable[str] = (),
    ) -> None:
        """把没能派发的一批放回缓冲（对方正在被回复时用）。

        ★ 批次号必须一起放回来：不放回去就等于「这批还没处理，但账已经销了」，
        下一轮成功时找不到该删哪一行，队列里会留下永远清不掉的残渣。
        """
        buffer = self._buffers.setdefault(key, _Buffer())
        buffer.messages = [*messages, *buffer.messages]
        buffer.last_at = now
        buffer.batch_ids.update(batch_ids)

    def has_batch(self, batch_id: str) -> bool:
        """这个批次是不是已经在缓冲里了（重投时用来防重复装载）。"""
        return any(batch_id in buffer.batch_ids for buffer in self._buffers.values())

    def take_due(self, now: datetime) -> list[tuple[PeerKey, list[InboundMessage], set[str]]]:
        """取走已经静默够久的批次，并从缓冲里清掉。"""
        due: list[tuple[PeerKey, list[InboundMessage], set[str]]] = []
        for key, buffer in list(self._buffers.items()):
            if not buffer.messages or buffer.last_at is None:
                continue
            if (now - buffer.last_at).total_seconds() >= self._window:
                due.append((key, buffer.messages, set(buffer.batch_ids)))
                del self._buffers[key]
        return due


class InboundLoop:
    """入站主循环。"""

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        source: MessageSource,
        store,  # GatewayStore（鸭子类型，避免循环导入）
        handler: TurnHandler,
        debounce_seconds: float = 3.0,
        poll_interval_seconds: float = 5.0,
        active_poll_interval_seconds: float | None = None,
        hot_window_seconds: float = 600.0,
        is_active: Callable[[datetime], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        on_peer_messages: Callable[[str, str, Sequence[InboundMessage]], None] | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self._source = source
        self._store = store
        self._handler = handler
        self._on_peer_messages = on_peer_messages
        """「刚读到对方几条新消息」的旁听者（业务后端的消息事件上报）。

        **只是旁听**：它不参与去抖、不决定派不派轮，抛异常也只记账。入站循环的职责
        没变，这里只是把「对方开口了」这个事实原样交出去一次。"""
        self._poll_interval = poll_interval_seconds
        self._active_interval = (
            poll_interval_seconds if active_poll_interval_seconds is None
            else min(active_poll_interval_seconds, poll_interval_seconds)
        )
        self._hot_window = max(0.0, hot_window_seconds)
        self._last_activity: datetime | None = None
        self._debouncer = Debouncer(debounce_seconds)
        self._is_active = is_active or (lambda _now: True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng
        self._sleeping = False
        self._next_poll_at: datetime | None = None  # 下一趟读消息的时刻，给界面倒计时用
        self._inflight: dict[PeerKey, asyncio.Task[None]] = {}
        self._pending_loaded: set[str] = set()
        self._inflight_batches: set[str] = set()
        """正在被某一轮处理的批次。**必须单独记**：它此刻既不在缓冲里、也还没销账，
        `_reload_pending` 看不见它就会把同一批再装载一次 → 候选人收到两条一样的回复
        （2026-08-18 审计 P0-8）。"""
        self._quiet_notice: dict[PeerKey, tuple[str, int]] = {}
        self._stopped = asyncio.Event()

    # ────────────────────────── 单步 ──────────────────────────

    async def tick(self) -> int:
        """跑一趟：取消息 → 去重 → 去抖 → 派发。返回本趟派发的批次数。

        非工作时段**一个微信动作都不做**（连读都不读）。原来只在发送那一步拦，可入站轮询
        是 24 小时不停的——凌晨三点还在一秒不差地翻聊天列表，正是风控要找的那种账号
        （2026-08-12 真机自动退登事故）。夜里攒下的消息不会丢：微信里还是未读，
        第二天进工作时段第一趟就读到。
        """
        now = self._clock()
        if not self._is_active(now):
            if not self._sleeping:
                self._sleeping = True
                _log.info("不在工作时间窗，暂停轮询微信")
            return 0
        if self._sleeping:
            self._sleeping = False
            _log.info("进入工作时间窗，恢复轮询微信")
        # 补投欠账要排在读新消息**之前**：先来的先回，别让重试一直被新消息插队。
        self._reload_pending(now)
        await self._collect(now)
        # ★ 派发要用**读完之后**的时钟，不能复用上面那个 now（2026-08-14 真机实测）：
        # 消息是在 `_collect` 里按 `now` 入的缓冲，而去抖要求「静默满 debounce 秒」。
        # 拿同一个 now 去问「够久了吗」，答案恒为 0 秒 —— 本趟收到的消息**必然**判不够，
        # 只能推到下一趟。轮询间隔 240s，于是每条回复白等一个完整周期（真机：21:25:39
        # 收到，21:26:43 和 21:27:58 两趟都没派发）。而 `_collect` 自己就要跑 5~15 秒
        # （逐个会话开窗读记录），读完再看表，去抖窗口通常已经自然满足。
        #
        # 这**不是**把节拍调快：轮询间隔、发送频次上限、工作时间窗一个都没动，
        # 微信动作总量不变，只是不再凭空空转一趟。
        return self._dispatch_due(self._clock())

    async def _collect(self, now: datetime) -> None:
        for account_id in self._accounts:
            try:
                messages = await self._source.fetch(account_id)
            except Exception as exc:  # noqa: BLE001 —— 一个账号取失败不该拖垮整个循环
                _log.warning("入站取消息失败", account=account_id, error=str(exc))
                continue

            by_peer: dict[str, list[InboundMessage]] = {}
            for message in messages:
                by_peer.setdefault(message.peer_id, []).append(message)

            for peer_id, peer_messages in by_peer.items():
                self._ingest(account_id, peer_id, peer_messages, now)

    def _ingest(
        self,
        account_id: str,
        peer_id: str,
        messages: Sequence[InboundMessage],
        now: datetime,
    ) -> None:
        # 黑名单的人连收都不收——不处理、不留缓冲。
        if self._store.is_blacklisted(account_id, peer_id):
            self._store.mark_seen(account_id, peer_id, [m.msg_id for m in messages])
            return

        # 幂等：RPA 重复推同一条消息不能触发第二轮。
        unseen_ids = set(self._store.filter_unseen(account_id, peer_id, [m.msg_id for m in messages]))
        fresh = [m for m in messages if m.msg_id in unseen_ids]
        if not fresh:
            return
        self._store.mark_seen(account_id, peer_id, [m.msg_id for m in fresh])

        # ★ 看到的都记下来（双方的话都记）。AI 端每一轮只收得到「这一趟攒下的那几条」，
        # 而它要判的是「这一步走完没有」——对方上周答应过来看看、专员前天已经问过工种，
        # 都在更早的轮次里。记在这里，下一轮当历史一并送上去（见 `GatewayRunner._history_for`）。
        # 放在拒绝闸**之前**：那条「别再联系我了」本身也是这段对话的一部分，记下来才对得上。
        self._store.record_messages(account_id, peer_id, [message_to_row(m) for m in fresh])

        # 对方真说话了 = 这个会话正热着 → 接下来几分钟读得勤一点（见 `_interval_for`）。
        if any(not message.from_self for message in fresh):
            self._last_activity = now

        # ★ 对方真开口了 → 交给旁听者上报（业务后端据此取消待跟进）。
        #   **必须排在拒绝闸之前**：「别再联系我了」也是一次回复，而那正是最不该再追一条的
        #   时刻——放在 `return` 后面，这条事件永远发不出去，后端照旧四小时后追过去。
        self._note_peer_messages(account_id, peer_id, [m for m in fresh if not m.from_self])

        # ★ 确定性拒绝闸（第一道，不依赖模型）：对方说了「别联系我」类的话 →
        #   立即拉黑，且**不派发任何一轮**。连告别语都不发——对方要的是别再被联系，
        #   再回一句同样是一次联系（需求文档 §7 的字面要求）。
        for message in fresh:
            if not message.from_self and looks_like_rejection(message.text):
                self._store.blacklist(account_id, peer_id, reason="入站消息命中拒绝表达")
                _local.info("命中拒绝表达，已拉黑并停止自动化", account=account_id)
                return

        # ★ 落盘再入缓冲（070）：游标在读到消息那一刻就推进了，从这里到「AI 端跑完一轮」
        # 之间断掉任何一环，这批消息就同时满足「游标翻过去了」和「谁也没处理」。
        #
        # 但**只有欠着对方一条回复的批次才记账**：一批全是专员自己说的话（含网关刚发出去、
        # 下一轮被读回来的那几条）根本不会触发任何一轮（见 `_dispatch_due`），记进待处理表
        # 就成了一笔永远销不掉的账——实时日志里那句「有 N 批已经读进来、但还没回出去」
        # 会一直挂着不降，看的人以为消息卡住了（2026-08-14 真机：专员自己在群里说了句话，
        # 挂了一下午；那会儿它显示在状态栏上，2026-08-21 搬进日志了）。这几条仍留在内存
        # 缓冲里当上下文，等对方真回话时并进那一轮；进程中途重启最多丢掉这点上下文，
        # 而「欠对方的回复不能丢」这条不变量一点没松。
        owes_reply = any(not message.from_self for message in fresh)
        batch_id = f"pi_{uuid.uuid4().hex}" if owes_reply else None
        if batch_id:
            self._store.put_pending_inbound(
                account_id, peer_id, batch_id, [message_to_row(m) for m in fresh], at=now
            )
        for message in fresh:
            self._debouncer.add(message, now, batch_id)

    def _note_peer_messages(
        self, account_id: str, peer_id: str, messages: Sequence[InboundMessage]
    ) -> None:
        """把「对方发来这几条」交给旁听者。**出了错只记账**——读循环不能被它带停。"""
        if not messages or self._on_peer_messages is None:
            return
        try:
            self._on_peer_messages(account_id, peer_id, messages)
        except Exception as exc:  # noqa: BLE001 —— 旁听者出错不该影响收消息
            _backend.warning("入站消息事件登记失败", error=str(exc))

    def _dispatch_due(self, now: datetime) -> int:
        dispatched = 0
        for key, messages, batch_ids in self._debouncer.take_due(now):
            task = self._inflight.get(key)
            if task is not None and not task.done():
                # 这个联系人正在被回复——放回缓冲，等这一轮收口后再合并派发。
                # 同一会话严格串行（AI 端也只允许一轮，抢跑会直接报错）。
                self._debouncer.readd(key, messages, now, batch_ids)
                self._say_once(key, "上一轮还没跑完，这几条先等着", len(messages))
                continue

            if not any(not message.from_self for message in messages):
                # 一批里全是专员本人说的话（含网关自己刚发出去、下一轮被读回来的回复）：
                # **不能拿它触发一轮**，否则就是自己回自己，会一直刷。留在缓冲里，等对方
                # 真回话时并进那一轮的上下文（CLAUDE.md §5.2「人工接管优先」要的就是这个）。
                self._debouncer.readd(key, list(messages)[-_SELF_CONTEXT_LIMIT:], now, batch_ids)
                self._say_once(key, "这几条全是自己说的，不触发回复，等对方开口", len(messages))
                continue

            account_id, peer_id = key
            transcript = format_transcript(messages)
            _ai.info("派发一轮给 AI 端", 会话=peer_id, 条数=len(messages))
            self._quiet_notice.pop(key, None)
            self._inflight_batches.update(batch_ids or ())
            self._inflight[key] = asyncio.create_task(
                self._run_handler(account_id, peer_id, transcript, messages, batch_ids)
            )
            dispatched += 1
        return dispatched

    def _say_once(self, key: PeerKey, why: str, count: int) -> None:
        """「这批为什么没派发」说一次就够。

        缓冲里的消息每趟 tick 都会重新过一遍判定，逐趟都喊等于把实时日志刷满，
        真正有用的那条反而被埋掉。内容变了（又攒了一条）才再说一次。
        """
        if self._quiet_notice.get(key) == (why, count):
            return
        self._quiet_notice[key] = (why, count)
        # 这条说的是「这批为什么不派轮」——本机的判定，不是微信那边发生了什么
        _local.info(why, 会话=key[1], 条数=count)

    async def _run_handler(
        self,
        account_id: str,
        peer_id: str,
        transcript: str,
        messages: Sequence[InboundMessage],
        batch_ids: set[str] | None = None,
    ) -> None:
        try:
            try:
                outcome = await self._handler(account_id, peer_id, transcript, messages)
            except Exception as exc:  # noqa: BLE001 —— 一个联系人处理失败不该拖垮循环
                # ★ 失败**不销账**：批次留在待处理表里，下一趟 tick 会重投（070）。
                # 原来这里只记一条 warning 就把消息吞了——AI 端一断线，候选人就永远等不到回复。
                self._keep_pending(account_id, batch_ids, FOLLOW_UP_RETRY, str(exc))
                return
            # 没抛异常 ≠ 把话说出去了。一条都没发出去的两种情况（AI 端报错 / 被守门拦下）
            # 由 handler 在返回值上标出来——**它们同样不能销账**（审计 P0-4）。
            follow_up = str(getattr(outcome, "follow_up", "") or "")
            if follow_up:
                self._keep_pending(account_id, batch_ids, follow_up,
                                   str(getattr(outcome, "skipped_reason", "")))
                return
            # 有结论了（发出去了，或明确决定不回这一轮）才销账。
            for batch_id in batch_ids or ():
                self._store.drop_pending_inbound(batch_id)
                self._pending_loaded.discard(batch_id)
        finally:
            self._inflight_batches.difference_update(batch_ids or ())

    def _keep_pending(
        self, account_id: str, batch_ids: set[str] | None, follow_up: str, why: str
    ) -> None:
        """这批没有结论：留在待处理表里等重投。

        `retry` 计一次失败（有上限，最终会放弃并留一条警告）；`defer` 不计——
        额度用完、不在时间窗这类拦截一定会自己过期，把它算成失败等于用重试次数
        去消耗一段本来就该等的时间，等额度回来时批次已经被放弃了。
        """
        attempts = 0
        if follow_up == FOLLOW_UP_RETRY:
            for batch_id in batch_ids or ():
                attempts = max(attempts, self._store.bump_pending_attempts(batch_id))
        self._pending_loaded.difference_update(batch_ids or ())
        _ai.warning(
            "这一轮没把话说出去，本批留在待处理队列等重投",
            account=account_id,
            原因=why or follow_up,
            计入失败=(follow_up == FOLLOW_UP_RETRY),
            已重试=attempts,
        )

    # ────────────────────────── 欠账重投（070）──────────────────────────

    def _reload_pending(self, now: datetime) -> int:
        """把待处理表里还没送出去的批次装回缓冲。返回装载了几批。

        进程重启、AI 端断线恢复之后靠这一步把欠的账补上。幂等由 ``gateway_seen_messages``
        保证——重投的还是同一批 msg_id，不会二次触发别的规则。
        """
        # 顺手清掉过保鲜期的聊天记录（真实 PII，不留成越来越大的本地归档）。
        dropped = self._store.purge_messages_before(now - timedelta(days=HISTORY_MAX_AGE_DAYS))
        if dropped:
            _local.info("清掉过期聊天记录", 条数=dropped, 保留天数=HISTORY_MAX_AGE_DAYS)

        cutoff = now - timedelta(hours=PENDING_MAX_AGE_HOURS)
        for row in self._store.purge_pending_inbound_before(cutoff):
            self._pending_loaded.discard(row["batch_id"])
            _local.warning(
                "待处理消息超过保鲜期，已丢弃（这时候再回过去反而更奇怪）",
                account=row["account_id"],
                条数=len(row["payload"]),
                收到于=row["created_at"],
            )

        loaded = 0
        for row in self._store.list_pending_inbound():
            batch_id = row["batch_id"]
            if (batch_id in self._pending_loaded
                    or batch_id in self._inflight_batches      # 正在跑：再装一次就是回两遍
                    or self._debouncer.has_batch(batch_id)):
                continue
            if row["attempts"] >= PENDING_MAX_ATTEMPTS:
                self._store.drop_pending_inbound(batch_id)
                _local.warning(
                    "待处理消息重投多次仍失败，已放弃",
                    account=row["account_id"],
                    条数=len(row["payload"]),
                    已重试=row["attempts"],
                )
                continue
            account_id, peer_id = row["account_id"], row["peer_id"]
            messages = [message_from_row(account_id, peer_id, item) for item in row["payload"]]
            if not messages:
                self._store.drop_pending_inbound(batch_id)
                continue
            # last_at 用 messages 的原始时间：重投的批次早就静默够久了，不该再等一个去抖窗口。
            for message in messages:
                self._debouncer.add(message, messages[-1].at, batch_id)
            self._pending_loaded.add(batch_id)
            loaded += 1
        if loaded:
            _local.info("从待处理队列补投", 批次=loaded)
        return loaded

    @property
    def pending_batches(self) -> int:
        """待处理表里还欠着几批。只读，给状态栏/诊断用。"""
        return self._store.count_pending_inbound()

    async def drain(self) -> None:
        """等所有在途的一轮收口（测试与优雅退出用）。"""
        while True:
            pending = [t for t in self._inflight.values() if not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()

    # ────────────────────────── 常驻 ──────────────────────────

    def _interval_for(self, now: datetime) -> float:
        """这一觉睡多久：**聊着的时候勤一点，没人说话就慢下来。**

        原来是一个固定值（线上 300 秒）：对方问一句要等最多 5 分钟才被看见，回得像台机器；
        而没人说话时又照样每 5 分钟翻一次微信，白动。人的节奏本来就是这样——正在聊就
        盯着，聊完就放下。

        ⚠️ **快也只快到下限为止**：`_active_interval` 在装配处已经被 `clamp_interval` 抬到
        `MIN_READ_INTERVAL_SECONDS`（30 秒）以上，抖动、工作时间窗、发送频次上限一个没动。
        这不是把节拍整体调快，是「热的时候借用已经允许的那一档」。
        """
        if self._last_activity is None or self._hot_window <= 0:
            return self._poll_interval
        if (now - self._last_activity).total_seconds() <= self._hot_window:
            return self._active_interval
        return self._poll_interval

    def set_pace(
        self,
        *,
        idle_seconds: float | None = None,
        active_seconds: float | None = None,
        hot_window_seconds: float | None = None,
        floor: float,
    ) -> dict[str, float]:
        """运行期改节拍（AI 端下发用）。返回改完之后的三个值。

        **下限由这里把住，不信下发方**：AI 端一句「盯紧点」不该能把读消息间隔压到 5 秒。
        低于 `floor` 一律抬到 `floor`，热窗最长 1 小时——热窗无限长等于永远按快档跑。
        """
        if idle_seconds is not None:
            self._poll_interval = max(float(idle_seconds), floor)
        if active_seconds is not None:
            self._active_interval = max(float(active_seconds), floor)
        if hot_window_seconds is not None:
            self._hot_window = max(0.0, min(float(hot_window_seconds), 3600.0))
        # 快档不该比慢档还慢——两个值一起下发时顺手保证这条。
        self._active_interval = min(self._active_interval, self._poll_interval)
        pace = {
            "idle_seconds": self._poll_interval,
            "active_seconds": self._active_interval,
            "hot_window_seconds": self._hot_window,
        }
        _ai.info("节拍已更新（AI 端下发）", **pace)
        return pace

    async def run(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            await self.tick()
            # 抖动而不是定时：每 60 秒整点动一次微信，是「这不是人在用」最明显的指纹。
            delay = jittered(self._interval_for(self._clock()), rng=self._rng)
            # 记下这一觉睡到几点：界面拿它做「下次读消息」的倒计时。**记的是真实那一觉**
            # （含抖动、含快慢档），不是配置里那个数——照配置算的倒计时会一直和现实差几十秒。
            self._next_poll_at = self._clock() + timedelta(seconds=delay)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
            except TimeoutError:
                continue
        self._next_poll_at = None
        await self.drain()

    def in_quiet_window(self) -> bool:
        """现在是不是在工作时间窗之外（窗外一个微信动作都不做）。只读，可跨线程读。"""
        return not self._is_active(self._clock())

    def seconds_to_next_poll(self) -> float | None:
        """离下一趟读消息还有几秒。``None`` = 现在压根不会读（没在跑 / 不在工作时间窗）。

        只读，可跨线程读——界面每秒问一次。
        """
        if self._next_poll_at is None:
            return None
        now = self._clock()
        if not self._is_active(now):
            return None  # 窗外一个微信动作都不做，倒计时到点了也不会读，别给个会骗人的数
        return max(0.0, (self._next_poll_at - now).total_seconds())

    def stop(self) -> None:
        self._stopped.set()

    def cancel_inflight(self) -> int:
        """放弃还没收口的轮次，返回放弃了几轮。

        只在退出时用：等一轮跑完最长要等到 turn_timeout（默认 180s），而人已经点了退出。
        丢掉的那一轮不是灾难——对方的消息没标已读，下次起来照样会被重新读到。
        """
        pending = [task for task in self._inflight.values() if not task.done()]
        for task in pending:
            task.cancel()
        self._inflight.clear()
        return len(pending)
