"""入站：轮询取消息 → 去重 → 去抖聚合 → 串行派发一轮对话。

**入站只能轮询**（``CLAUDE.md`` §4.3）：``wechat_api`` 的 watch/unread 都是有界时长任务，
跑完就返回，没有「来消息就回调」这回事。所以这里自己转一个常驻循环。

为了让整层在 Linux 上可测，时间从外部注入、``tick()`` 是可单步调用的一趟；
``run()`` 只是「反复 tick + 睡一会」。测试永远不需要真的等去抖窗口过去。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import structlog

from .guard import looks_like_rejection

_log = structlog.get_logger(__name__)

PeerKey = tuple[str, str]  # (account_id, peer_id)

#: 只有专员本人说话时最多留几条当上下文。留着是为了下一轮别重复问他问过的，
#: 但不能无上限攒——他自己聊一下午，缓冲和 prompt 都会被撑爆。
_SELF_CONTEXT_LIMIT = 10


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

    @property
    def key(self) -> PeerKey:
        return (self.account_id, self.peer_id)


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
        lines.append(f"[{who} · {message.at:%H:%M}] {message.text}")
    return "\n".join(lines)


@dataclass
class _Buffer:
    messages: list[InboundMessage] = field(default_factory=list)
    last_at: datetime | None = None


class Debouncer:
    """按联系人缓冲消息，静默满 ``window`` 秒才放行。

    对方连发三条短消息应该合成一轮回复，否则会回三次——像个机器人。
    """

    def __init__(self, window_seconds: float) -> None:
        self._window = window_seconds
        self._buffers: dict[PeerKey, _Buffer] = {}

    def add(self, message: InboundMessage, now: datetime) -> None:
        buffer = self._buffers.setdefault(message.key, _Buffer())
        buffer.messages.append(message)
        buffer.last_at = now

    def readd(self, key: PeerKey, messages: Sequence[InboundMessage], now: datetime) -> None:
        """把没能派发的一批放回缓冲（对方正在被回复时用）。"""
        buffer = self._buffers.setdefault(key, _Buffer())
        buffer.messages = [*messages, *buffer.messages]
        buffer.last_at = now

    def take_due(self, now: datetime) -> list[tuple[PeerKey, list[InboundMessage]]]:
        """取走已经静默够久的批次，并从缓冲里清掉。"""
        due: list[tuple[PeerKey, list[InboundMessage]]] = []
        for key, buffer in list(self._buffers.items()):
            if not buffer.messages or buffer.last_at is None:
                continue
            if (now - buffer.last_at).total_seconds() >= self._window:
                due.append((key, buffer.messages))
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self._source = source
        self._store = store
        self._handler = handler
        self._poll_interval = poll_interval_seconds
        self._debouncer = Debouncer(debounce_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._inflight: dict[PeerKey, asyncio.Task[None]] = {}
        self._stopped = asyncio.Event()

    # ────────────────────────── 单步 ──────────────────────────

    async def tick(self) -> int:
        """跑一趟：取消息 → 去重 → 去抖 → 派发。返回本趟派发的批次数。"""
        now = self._clock()
        await self._collect(now)
        return self._dispatch_due(now)

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

        for message in fresh:
            self._debouncer.add(message, now)

    def _dispatch_due(self, now: datetime) -> int:
        dispatched = 0
        for key, messages in self._debouncer.take_due(now):
            task = self._inflight.get(key)
            if task is not None and not task.done():
                # 这个联系人正在被回复——放回缓冲，等这一轮收口后再合并派发。
                # 同一会话严格串行（AI 端也只允许一轮，抢跑会直接报错）。
                self._debouncer.readd(key, messages, now)
                continue

            if not any(not message.from_self for message in messages):
                # 一批里全是专员本人说的话（含网关自己刚发出去、下一轮被读回来的回复）：
                # **不能拿它触发一轮**，否则就是自己回自己，会一直刷。留在缓冲里，等对方
                # 真回话时并进那一轮的上下文（CLAUDE.md §5.2「人工接管优先」要的就是这个）。
                self._debouncer.readd(key, list(messages)[-_SELF_CONTEXT_LIMIT:], now)
                continue

            account_id, peer_id = key
            transcript = format_transcript(messages)
            self._inflight[key] = asyncio.create_task(
                self._run_handler(account_id, peer_id, transcript, messages)
            )
            dispatched += 1
        return dispatched

    async def _run_handler(
        self,
        account_id: str,
        peer_id: str,
        transcript: str,
        messages: Sequence[InboundMessage],
    ) -> None:
        try:
            await self._handler(account_id, peer_id, transcript, messages)
        except Exception as exc:  # noqa: BLE001 —— 一个联系人处理失败不该拖垮循环
            _log.warning("处理入站消息失败", account=account_id, error=str(exc))

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
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval)
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
