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
from typing import Protocol

import structlog

from .guard import looks_like_rejection
from .pacing import jittered

_log = structlog.get_logger(__name__)

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
    )


class MessageSource(Protocol):
    """入站消息来源。生产实现投递队列任务读微信；测试用假实现。"""

    async def fetch(self, account_id: str) -> Sequence[InboundMessage]: ...


TurnHandler = Callable[[str, str, str, Sequence[InboundMessage]], Awaitable[None]]
"""(account_id, peer_id, transcript, messages) -> None"""


def format_transcript(messages: Iterable[InboundMessage]) -> str:
    """拼成 AI 端约定的入站上下文格式（064 design §2）。

    说话人只有两类，且**不暴露 wxid**——AI 端不需要知道对方的微信标识。
    """
    lines = []
    for message in messages:
        who = "专员本人" if message.from_self else "对方"
        when = message.at_text or f"{message.at:%H:%M}"
        lines.append(f"[{who} · {when}] {message.text}")
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

    @property
    def pending_peers(self) -> int:
        return sum(1 for b in self._buffers.values() if b.messages)


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
        is_active: Callable[[datetime], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self._source = source
        self._store = store
        self._handler = handler
        self._poll_interval = poll_interval_seconds
        self._debouncer = Debouncer(debounce_seconds)
        self._is_active = is_active or (lambda _now: True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng
        self._sleeping = False
        self._inflight: dict[PeerKey, asyncio.Task[None]] = {}
        self._pending_loaded: set[str] = set()
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

        # ★ 确定性拒绝闸（第一道，不依赖模型）：对方说了「别联系我」类的话 →
        #   立即拉黑，且**不派发任何一轮**。连告别语都不发——对方要的是别再被联系，
        #   再回一句同样是一次联系（需求文档 §7 的字面要求）。
        for message in fresh:
            if not message.from_self and looks_like_rejection(message.text):
                self._store.blacklist(account_id, peer_id, reason="入站消息命中拒绝表达")
                _log.info("命中拒绝表达，已拉黑并停止自动化", account=account_id)
                return

        # ★ 落盘再入缓冲（070）：游标在读到消息那一刻就推进了，从这里到「AI 端跑完一轮」
        # 之间断掉任何一环，这批消息就同时满足「游标翻过去了」和「谁也没处理」。
        #
        # 但**只有欠着对方一条回复的批次才记账**：一批全是专员自己说的话（含网关刚发出去、
        # 下一轮被读回来的那几条）根本不会触发任何一轮（见 `_dispatch_due`），记进待处理表
        # 就成了一笔永远销不掉的账——状态栏一直橙着「已连接 · 欠 N 批」，看的人以为消息卡住了
        # （2026-08-14 真机：专员自己在群里说了句话，欠 1 批挂了一下午）。这几条仍留在内存
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

    def _dispatch_due(self, now: datetime) -> int:
        dispatched = 0
        for key, messages, batch_ids in self._debouncer.take_due(now):
            task = self._inflight.get(key)
            if task is not None and not task.done():
                # 这个联系人正在被回复——放回缓冲，等这一轮收口后再合并派发。
                # 同一会话严格串行（AI 端也只允许一轮，抢跑会直接报错）。
                self._debouncer.readd(key, messages, now, batch_ids)
                continue

            if not any(not message.from_self for message in messages):
                # 一批里全是专员本人说的话（含网关自己刚发出去、下一轮被读回来的回复）：
                # **不能拿它触发一轮**，否则就是自己回自己，会一直刷。留在缓冲里，等对方
                # 真回话时并进那一轮的上下文（CLAUDE.md §5.2「人工接管优先」要的就是这个）。
                self._debouncer.readd(key, list(messages)[-_SELF_CONTEXT_LIMIT:], now, batch_ids)
                continue

            account_id, peer_id = key
            transcript = format_transcript(messages)
            self._inflight[key] = asyncio.create_task(
                self._run_handler(account_id, peer_id, transcript, messages, batch_ids)
            )
            dispatched += 1
        return dispatched

    async def _run_handler(
        self,
        account_id: str,
        peer_id: str,
        transcript: str,
        messages: Sequence[InboundMessage],
        batch_ids: set[str] | None = None,
    ) -> None:
        try:
            await self._handler(account_id, peer_id, transcript, messages)
        except Exception as exc:  # noqa: BLE001 —— 一个联系人处理失败不该拖垮循环
            # ★ 失败**不销账**：批次留在待处理表里，下一趟 tick 会重投（070）。
            # 原来这里只记一条 warning 就把消息吞了——AI 端一断线，候选人就永远等不到回复。
            attempts = 0
            for batch_id in batch_ids or ():
                attempts = max(attempts, self._store.bump_pending_attempts(batch_id))
            self._pending_loaded.difference_update(batch_ids or ())
            _log.warning(
                "处理入站消息失败，已留在待处理队列等下一趟重投",
                account=account_id,
                error=str(exc),
                已重试=attempts,
            )
            return
        # 成功才销账。
        for batch_id in batch_ids or ():
            self._store.drop_pending_inbound(batch_id)
            self._pending_loaded.discard(batch_id)

    # ────────────────────────── 欠账重投（070）──────────────────────────

    def _reload_pending(self, now: datetime) -> int:
        """把待处理表里还没送出去的批次装回缓冲。返回装载了几批。

        进程重启、AI 端断线恢复之后靠这一步把欠的账补上。幂等由 ``gateway_seen_messages``
        保证——重投的还是同一批 msg_id，不会二次触发别的规则。
        """
        cutoff = now - timedelta(hours=PENDING_MAX_AGE_HOURS)
        for row in self._store.purge_pending_inbound_before(cutoff):
            self._pending_loaded.discard(row["batch_id"])
            _log.warning(
                "待处理消息超过保鲜期，已丢弃（这时候再回过去反而更奇怪）",
                account=row["account_id"],
                条数=len(row["payload"]),
                收到于=row["created_at"],
            )

        loaded = 0
        for row in self._store.list_pending_inbound():
            batch_id = row["batch_id"]
            if batch_id in self._pending_loaded or self._debouncer.has_batch(batch_id):
                continue
            if row["attempts"] >= PENDING_MAX_ATTEMPTS:
                self._store.drop_pending_inbound(batch_id)
                _log.warning(
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
            _log.info("从待处理队列补投", 批次=loaded)
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

    async def run(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            await self.tick()
            # 抖动而不是定时：每 60 秒整点动一次微信，是「这不是人在用」最明显的指纹。
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=jittered(self._poll_interval, rng=self._rng)
                )
            except TimeoutError:
                continue
        await self.drain()

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
