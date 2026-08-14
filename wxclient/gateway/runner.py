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
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

import structlog

from .capabilities import CAPABILITIES_OP, Capability, capabilities_frame
from .commands import (
    COMMAND_EVENT,
    RESULT_OP,
    STAGE_RECEIVED,
    CommandExecutor,
    parse_commands,
)
from .config import GatewaySettings
from .friends import FriendWatcher
from .guard import OutboundGuard, action_limits_from, within_active_hours
from .inbound import InboundLoop, InboundMessage, MessageSource
from .outbound import DeliveryResult, MessageSink, OutboundSender
from .pacing import MIN_READ_INTERVAL_SECONDS, clamp_interval
from .schedule import ScheduleLoop, parse_schedule_at
from .sessions import GatewayStore
from .ticket import TicketProvider
from .watchlist import PUSH_EVENT, Watchlist
from .wsclient import AgentWsClient, AgentWsError, Unauthorized

_log = structlog.get_logger(__name__)

#: 建连/保活的节拍。线上 nginx 对 /agent-ws 配的是 600s 空闲上限，30s 一拍既够续命，
#: 也让「连接已经死了」最多 30s 就被发现（而不是攒到第一条真消息上）。
KEEPALIVE_INTERVAL_SECONDS = 30.0

#: 退出时最多等在途的一轮收口多久。超了就放弃那一轮——丢一轮远好过界面卡住不动。
SHUTDOWN_DRAIN_SECONDS = 3.0


class ConnectionPool:
    """一专员一连接。断线后重连——**重连必须现换一张新票**。"""

    def __init__(
        self,
        *,
        ws_url: str,
        ticket_provider: TicketProvider,
        turn_timeout_seconds: float = 180.0,
        client_factory: Callable[..., AgentWsClient] | None = None,
        on_push: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._tickets = ticket_provider
        self._turn_timeout = turn_timeout_seconds
        self._factory = client_factory or AgentWsClient
        self._on_push = on_push
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
            # 推送回调要带上是**哪个账号**收到的：连接是一账号一条，事件本身不带账号。
            handler = self._on_push
            client = self._factory(
                self._ws_url,
                turn_timeout_seconds=self._turn_timeout,
                on_push=(lambda ev, data, a=account_id: handler(a, ev, data))
                if handler is not None else None,
            )
            await client.connect(ticket)
            self._clients[account_id] = client
            self._resumed[account_id] = set()
            _log.info("已为该账号建立 AI 端连接", account=account_id)
            return client

    def connected_count(self) -> int:
        """当前有几条 AI 端连接是活的。只读，给状态展示用（可跨线程读）。"""
        return sum(1 for client in list(self._clients.values()) if client.connected)

    async def heartbeat(self, account_id: str) -> None:
        """没连就连上，连着就 ping 一下确认还活着。失败会抛，由调用方记账。"""
        client = self._clients.get(account_id)
        if client is None or not client.connected:
            await self.acquire(account_id)
            return
        try:
            await client.ping()
        except Exception:
            # ping 都不通说明这条连接已经废了，丢掉，下一轮 heartbeat 会重新换票建连。
            await self.invalidate(account_id)
            raise

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
        watchlist: Watchlist | None = None,
        friend_watcher: FriendWatcher | None = None,
        command_executor: CommandExecutor | None = None,
        client_factory: Callable[..., AgentWsClient] | None = None,
        clock: Callable[[], datetime] | None = None,
        capabilities: Callable[[], dict[str, Capability]] | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._accounts = tuple(accounts)
        self._connect_error: str | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._watchlist = watchlist
        self._friends = friend_watcher
        self._commands = command_executor
        self._capabilities = capabilities
        self._reported_capabilities: dict[str, dict[str, Any]] | None = None
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._pool = ConnectionPool(
            ws_url=settings.ai_ws_url,
            ticket_provider=ticket_provider,
            turn_timeout_seconds=settings.turn_timeout_seconds,
            client_factory=client_factory,
            on_push=self._on_push,
        )
        self._sender = OutboundSender(
            sink=sink,
            guard=OutboundGuard(
                store,
                max_sends_per_hour=settings.max_sends_per_hour,
                active_hours=settings.active_hours,
                action_limits=action_limits_from(settings),
            ),
            store=store,
            clock=self._clock,
        )
        # 077：定时指令自己一条循环。它压根不动微信（只翻自己的表），所以节拍跟
        # 读消息/查好友无关；到点之后的执行仍走 _run_command，幂等与守门一并复用。
        self.schedule = ScheduleLoop(
            accounts=list(accounts),
            store=store,
            run_command=self._run_command,
            is_active=self.is_active,
            clock=self._clock,
        )
        self.loop = InboundLoop(
            accounts=accounts,
            source=source,
            store=store,
            handler=self.handle_turn,
            debounce_seconds=settings.debounce_seconds,
            poll_interval_seconds=clamp_interval(
                settings.poll_interval_seconds, MIN_READ_INTERVAL_SECONDS, what="读消息间隔"
            ),
            is_active=self.is_active,
            clock=self._clock,
        )

    def is_active(self, now: datetime) -> bool:
        """现在允不允许动微信。读消息、查好友、发送共用同一个「工作时间窗」。"""
        return within_active_hours(now, self._settings.active_hours)

    def _on_push(self, account_id: str, ev: str, data: dict[str, Any]) -> None:
        """AI 端主动推来的事件（不属于任何一轮对话）：更新监听列表、下发能力指令。

        收到未知事件只记一条 debug，绝不因此断连——AI 端将来加别的推送，旧网关必须还能照跑。
        """
        if ev == PUSH_EVENT and self._watchlist is not None:
            self._watchlist.apply_push(data)
            return
        if ev == COMMAND_EVENT:
            self._dispatch_commands(account_id, data)
            return
        _log.debug("收到未处理的推送事件", ev=ev)

    def _dispatch_commands(self, account_id: str, data: dict[str, Any]) -> None:
        """指令**不在读循环里执行**：一条指令要动微信、可能跑十几秒，卡在读循环里
        期间连 ping 都收不到，整条连接会被判死。所以只登记任务，交给事件循环去跑。
        """
        if self._commands is None:
            _log.warning("收到微信指令但没有执行器，已忽略")
            return
        for command in parse_commands(data):
            if self._defer_if_scheduled(account_id, command):
                continue
            task = asyncio.create_task(self._run_command(account_id, command))
            self._command_tasks.add(task)
            task.add_done_callback(self._command_tasks.discard)

    def _defer_if_scheduled(self, account_id: str, command: dict[str, Any]) -> bool:
        """带 ``schedule_at`` 且时点在将来 → 落表等着，返回 True（本轮不执行）。

        077：原来 `schedule_at` 只是透传到网关就没人管了，「明天上午九点提醒他」
        会被**当场**发出去——比不做还糟。

        解析不了的时点不当定时处理，照常立即执行：宁可早发，也不要因为一个格式问题
        把用户交代的事悄悄咽掉。
        """
        due = parse_schedule_at(command.get("schedule_at"))
        now = self._clock()
        if due is None or due <= now:
            return False
        command_id = str(command.get("command_id") or "")
        if not command_id:
            return False
        self._store.put_scheduled_command(account_id, command_id, command, due, at=now)
        _log.info("指令已排期", op=command.get("op"), 到点=due.isoformat())
        # 报一声「排上了」——不报的话卡上停在「已下发」，而它其实一小时后才动
        # （073 的阶段回执，`scheduled` 是中间态，AI 端收到只刷卡不销账）。
        task = asyncio.create_task(
            self._report(account_id, command_id, "scheduled", scheduled_for=due.isoformat())
        )
        self._command_tasks.add(task)
        task.add_done_callback(self._command_tasks.discard)
        return True

    async def _report(self, account_id: str, command_id: str, status: str, **extra: Any) -> None:
        """把一个阶段报回 AI 端。报不出去只记账——回执发不掉不该影响已做完的动作。"""
        try:
            client = await self._pool.acquire(account_id)
            await client.notify(RESULT_OP, {"command_id": command_id, "status": status, **extra})
        except Exception as exc:  # noqa: BLE001
            _log.warning("指令回执发送失败", error=str(exc), status=status)

    async def _run_command(self, account_id: str, command: dict[str, Any]) -> None:
        assert self._commands is not None
        command_id = str(command.get("command_id") or "")

        async def report(status: str, **extra: Any) -> None:
            await self._report(account_id, command_id, status, **extra)

        # 073：先报「收到了」。一条指令要动微信、可能跑十几秒，中间一点信号都没有的话，
        # 卡上就停在「已下发」——分不清在跑还是丢了（需求文档 §12.4 要 接收/开始/成功/失败）。
        await report(STAGE_RECEIVED)
        try:
            result = await self._commands.execute(command, on_stage=report)
        except Exception as exc:  # noqa: BLE001 —— 执行器炸了也要给上游一个交代
            _log.warning("指令执行器异常", error=str(exc))
            result = None
        payload = (
            result.to_frame_data()
            if result is not None
            else {"command_id": command_id, "status": "failed", "reason": "网关内部错误"}
        )
        await report(str(payload.pop("status")), **{k: v for k, v in payload.items()
                                                    if k != "command_id"})

    # ────────────────────────── 一轮 ──────────────────────────

    async def handle_turn(
        self,
        account_id: str,
        peer_id: str,
        transcript: str,
        messages: Sequence[InboundMessage],
    ) -> DeliveryResult:
        """入站一批 → 一轮对话 → 出站。

        ★ 异常处理**分两段**（070 + 需求文档 §12.4）：

        - **对话轮失败**（AI 端断线、票据过期、超时）→ 向上抛。这时一条微信都还没发出去，
          `InboundLoop` 会把这批消息留在待处理队列里，恢复之后原样重投。

        - **投递阶段失败**（已经开始往微信发了）→ **只记账，不上抛**。这里重投的代价是
          「已经发出去的那几段再发一遍」——候选人收到重复消息，且撤不回来。
          需求文档 §12.4 的原话是「结果未知先查询或转人工，不能盲目重发」，
          所以这一批就此销账，宁可少一条回复，也不赌那一下。
        """
        card = await self._run_dialog_turn(account_id, peer_id, transcript, messages)
        try:
            return await self._sender.deliver(account_id, peer_id, card)
        except Exception as exc:  # noqa: BLE001 —— 见上：投递阶段不允许触发重投
            _log.warning(
                "投递回复时出错，本批不再重投（可能已经发出去一部分，重投会重复骚扰对方）",
                account=account_id,
                error=str(exc),
            )
            return DeliveryResult(skipped_reason=f"投递失败：{exc}")

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
                    extra={"wechat": self._turn_meta(peer_id, messages)},
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
    def _turn_meta(peer_id: str, messages: Sequence[InboundMessage]) -> dict[str, Any]:
        """给 AI 端的这一轮元信息（064 design §2）。

        `peer` 是这个会话在微信里显示的名字。AI 端拿它当关系记忆的键（078）——
        它那边只有 session_id，从 session_id 反推不出「这是在跟谁说话」。
        """
        return {
            "peer": peer_id,
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

    def watched_peers(self) -> list[str]:
        """当前在盯哪些会话。只读，给桌面端状态栏用（可跨线程读）。"""
        return self._watchlist.watched() if self._watchlist is not None else []

    def scheduled_count(self) -> int:
        """还排着几条定时指令（077）。只读，给状态栏用。"""
        return self._store.count_scheduled_commands()

    def pending_backlog(self) -> int:
        """入站待处理队列里还欠着几批（070）。只读，给状态栏用。

        平时恒为 0。持续 > 0 说明 AI 端那头不通、消息正在攒着——这必须让专员看见，
        否则「微信显示已读、可是没人回」只会被当成功能坏了。
        """
        return self.loop.pending_batches

    @property
    def connect_error(self) -> str | None:
        """上一次建连/保活的失败原因；连上了就是 None。只读，给状态栏用。"""
        return self._connect_error

    async def ensure_connections(self) -> None:
        """建连 + 保活。异常只记账不上抛——连不上 AI 端不该把入站轮询也带停。

        为什么不等第一条入站消息再连（原来的懒建连）：
        ① 界面得能立刻说清「通没通」，不然只能显示「连接中」干等；
        ② 票据/鉴权坏了要在**启动时**就炸出来，而不是等第一个真候选人来了才发现；
        ③ 反代有空闲上限（线上 600s），本来就需要一个周期性的 ping 来续命兼探活。
        """
        for account_id in self._accounts:
            try:
                await self._pool.heartbeat(account_id)
            except Exception as exc:  # noqa: BLE001 - 什么原因都要记下来给界面看
                self._connect_error = f"{type(exc).__name__}: {exc}"
                _log.warning("AI 端连接不可用", account=account_id, error=self._connect_error)
            else:
                self._connect_error = None
                await self._report_capabilities(account_id)

    async def run(self) -> None:
        # 四条常驻循环各走各的节拍：保活 30s、读消息 ~1min、查好友申请 ~15min、
        # 定时指令 30s（它不动微信，只翻自己的表）。
        # 谁也不等谁——把它们串成一个循环就等于让最慢的那件事决定所有事的频率。
        side_tasks = [
            asyncio.create_task(self._keepalive_loop(), name="gw-keepalive"),
            asyncio.create_task(self.schedule.run(), name="gw-schedule"),
        ]
        if self._friends is not None:
            side_tasks.append(asyncio.create_task(self._friends.run(), name="gw-friends"))
        try:
            await self.loop.run()
        finally:
            for task in side_tasks:
                task.cancel()
            for task in side_tasks:
                with suppress(asyncio.CancelledError):
                    await task

    async def _report_capabilities(self, account_id: str) -> None:
        """把「这台机器现在能做哪几件事」报给 AI 端（074）。**变了才报**。

        为什么挂在保活上而不是只在建连时报一次：微信随时可能被关掉、掉线、缩回托盘，
        能力表跟着变。只报一次的话，AI 端手里那份很快就是假的——而假的能力表比没有更糟，
        它会让 AI 理直气壮地承诺做不到的事。
        """
        if self._capabilities is None:
            return
        try:
            frame = capabilities_frame(self._capabilities())
        except Exception as exc:  # noqa: BLE001 —— 探测失败不该把保活带停
            _log.warning("能力探测失败", error=str(exc))
            return
        if frame["capabilities"] == self._reported_capabilities:
            return
        try:
            client = await self._pool.acquire(account_id)
            await client.notify(CAPABILITIES_OP, frame)
        except Exception as exc:  # noqa: BLE001
            _log.warning("能力上报失败", error=str(exc))
            return
        self._reported_capabilities = frame["capabilities"]
        unavailable = [
            cap["label"] for cap in frame["capabilities"].values() if not cap["available"]
        ]
        _log.info("已上报本机能力", 做不了=unavailable or "无")

    async def _keepalive_loop(self) -> None:
        while True:
            await self.ensure_connections()
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)

    async def stop(self) -> None:
        self.loop.stop()
        self.schedule.stop()
        if self._friends is not None:
            self._friends.stop()
        # 收口给个上限：正常情况下几十毫秒就完了，但如果正卡在一轮对话里，drain 会一直等到
        # turn_timeout（默认 180s）——人已经点了退出，不能让他对着一个不动的窗口干等。
        try:
            await asyncio.wait_for(self.loop.drain(), timeout=SHUTDOWN_DRAIN_SECONDS)
        except TimeoutError:
            abandoned = self.loop.cancel_inflight()
            _log.warning("退出时还有未收口的轮次，已放弃", turns=abandoned)
        await self._pool.close_all()
