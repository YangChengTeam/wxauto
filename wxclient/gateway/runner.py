"""编排：把协议层（换票 + WS）与业务层（入站 + 出站）接成一条回路。

一条入站消息的完整旅程：

    轮询取到 → 去重 → 去抖聚合 → [本文件] 取连接 → 建/接会话 → 发一轮 → 收卡
             → 守门 → 逐段投递 → 队列串行发出

连接策略（``CLAUDE.md`` §4.1）：**一个招聘专员一条 WS 连接**，因为 AI 端一条连接
绑定单一身份。连接常驻，票只在建连那一刻用一次。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

import structlog

from .config import GatewaySettings
from .guard import OutboundGuard
from .inbound import InboundLoop, InboundMessage, MessageSource
from .outbound import DeliveryResult, MessageSink, OutboundSender
from .sessions import GatewayStore
from .ticket import TicketProvider
from .wsclient import AgentWsClient, AgentWsError, Unauthorized

_log = structlog.get_logger(__name__)


class ConnectionPool:
    """一专员一连接。断线后重连——**重连必须现换一张新票**。"""

    def __init__(
        self,
        *,
        ws_url: str,
        ticket_provider: TicketProvider,
        turn_timeout_seconds: float = 180.0,
        client_factory: Callable[..., AgentWsClient] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._tickets = ticket_provider
        self._turn_timeout = turn_timeout_seconds
        self._factory = client_factory or AgentWsClient
        self._clients: dict[str, AgentWsClient] = {}
        self._resumed: dict[str, set[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    async def acquire(self, account_id: str) -> AgentWsClient:
        """取该专员的连接；没有或已断则现换票重连。"""
        async with self._lock(account_id):
            client = self._clients.get(account_id)
            if client is not None and client.connected:
                return client

            # 票 60s 即过期，所以每次建连都现换——绝不缓存复用。
            ticket = await self._tickets.mint(account_id)
            client = self._factory(self._ws_url, turn_timeout_seconds=self._turn_timeout)
            await client.connect(ticket)
            self._clients[account_id] = client
            self._resumed[account_id] = set()
            _log.info("已为该账号建立 AI 端连接", account=account_id)
            return client

    def connected_count(self) -> int:
        """当前有几条 AI 端连接是活的。只读，给状态展示用（可跨线程读）。"""
        return sum(1 for client in list(self._clients.values()) if client.connected)

    async def invalidate(self, account_id: str) -> None:
        """丢弃当前连接，下次 acquire 会重新换票建连。"""
        client = self._clients.pop(account_id, None)
        self._resumed.pop(account_id, None)
        if client is not None:
            await client.close()

    def mark_resumed(self, account_id: str, session_id: str) -> None:
        self._resumed.setdefault(account_id, set()).add(session_id)

    def needs_resume(self, account_id: str, session_id: str) -> bool:
        """本连接上还没接过这个会话 → 需要 resume（顺带触发 AI 端重投影）。"""
        return session_id not in self._resumed.get(account_id, set())

    async def close_all(self) -> None:
        for account_id in list(self._clients):
            await self.invalidate(account_id)


class GatewayRunner:
    """网关主体。"""

    def __init__(
        self,
        *,
        settings: GatewaySettings,
        ticket_provider: TicketProvider,
        store: GatewayStore,
        source: MessageSource,
        sink: MessageSink,
        accounts: Sequence[str],
        client_factory: Callable[..., AgentWsClient] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pool = ConnectionPool(
            ws_url=settings.ai_ws_url,
            ticket_provider=ticket_provider,
            turn_timeout_seconds=settings.turn_timeout_seconds,
            client_factory=client_factory,
        )
        self._sender = OutboundSender(
            sink=sink,
            guard=OutboundGuard(
                store,
                max_sends_per_hour=settings.max_sends_per_hour,
                active_hours=settings.active_hours,
            ),
            store=store,
            clock=self._clock,
        )
        self.loop = InboundLoop(
            accounts=accounts,
            source=source,
            store=store,
            handler=self.handle_turn,
            debounce_seconds=settings.debounce_seconds,
            poll_interval_seconds=settings.poll_interval_seconds,
            clock=self._clock,
        )

    # ────────────────────────── 一轮 ──────────────────────────

    async def handle_turn(
        self,
        account_id: str,
        peer_id: str,
        transcript: str,
        messages: Sequence[InboundMessage],
    ) -> DeliveryResult:
        """入站一批 → 一轮对话 → 出站。异常向上抛给 InboundLoop 记录，不吞。"""
        card = await self._run_dialog_turn(account_id, peer_id, transcript, messages)
        return await self._sender.deliver(account_id, peer_id, card)

    async def _run_dialog_turn(
        self,
        account_id: str,
        peer_id: str,
        transcript: str,
        messages: Sequence[InboundMessage],
    ) -> dict[str, Any] | None:
        """跑一轮，返回卡片。断线只重试一次——重试不了就这一轮作罢，下轮再说。"""
        for attempt in (1, 2):
            try:
                client = await self._pool.acquire(account_id)
                session_id = await self._ensure_session(client, account_id, peer_id)
                result = await client.send_message(
                    session_id,
                    transcript,
                    extra={"wechat": self._turn_meta(messages)},
                )
            except Unauthorized:
                # 票据/归属出问题：会话多半已经不属于我们了，清掉重来。
                self._store.drop_session(account_id, peer_id)
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise
                continue
            except AgentWsError:
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise
                continue

            if not result.ok:
                # AI 端明确报错：卡片是 fallback，没有 reply_segments，出站自然不会发东西。
                _log.warning(
                    "AI 端返回错误", account=account_id, code=(result.error or {}).get("code")
                )
                return None
            return result.card
        return None

    async def _ensure_session(
        self, client: AgentWsClient, account_id: str, peer_id: str
    ) -> str:
        """取该联系人的长命会话；没有就建，有就在本连接上接回来一次。"""
        session_id = self._store.get_session(account_id, peer_id)
        if session_id and self._pool.needs_resume(account_id, session_id):
            try:
                await client.resume_session(session_id)
            except Unauthorized:
                # AI 端说这个会话接不回来（被驱逐/不归我们）→ 丢掉重建，别卡死这个联系人。
                _log.info("旧会话接不回，改为新建", account=account_id)
                self._store.drop_session(account_id, peer_id)
                session_id = None
            else:
                self._pool.mark_resumed(account_id, session_id)

        if not session_id:
            session_id = await client.start_session(self._settings.task_type)
            self._store.put_session(account_id, peer_id, session_id)
            self._pool.mark_resumed(account_id, session_id)
        return session_id

    @staticmethod
    def _turn_meta(messages: Sequence[InboundMessage]) -> dict[str, Any]:
        """给 AI 端的这一轮元信息（064 design §2）。"""
        return {
            "funnel": "inbound",
            "operator_intervened": any(m.from_self for m in messages),
        }

    # ────────────────────────── 生命周期 ──────────────────────────

    async def tick(self) -> int:
        return await self.loop.tick()

    async def drain(self) -> None:
        await self.loop.drain()

    def connected_count(self) -> int:
        """活着的 AI 端连接数。只读，给桌面端状态栏用。"""
        return self._pool.connected_count()

    async def run(self) -> None:
        await self.loop.run()

    async def stop(self) -> None:
        self.loop.stop()
        await self.loop.drain()
        await self._pool.close_all()
