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

from .candidates import CandidateDirectory
from .capabilities import CAPABILITIES_OP, Capability, capabilities_frame
from .commands import (
    COMMAND_EVENT,
    NOTICE_EVENT,
    RESULT_OP,
    STAGE_RECEIVED,
    CommandExecutor,
    CommandResult,
    parse_commands,
)
from .config import GatewaySettings
from .contacts import STATUS_ACCEPTED as CONTACT_ACCEPTED
from .contacts import STATUS_REQUESTED as CONTACT_REQUESTED
from .contacts import ContactDirectory
from .friends import FriendWatcher
from .guard import OutboundGuard, action_limits_from, within_active_hours
from .inbound import FOLLOW_UP_RETRY, InboundLoop, InboundMessage, MessageSource
from .outbound import DeliveryResult, MessageSink, OutboundSender
from .pacing import MIN_READ_INTERVAL_SECONDS, PACING_EVENT, clamp_interval
from .schedule import ScheduleLoop, parse_schedule_at
from .sessions import GatewayStore
from .ticket import TicketProvider
from .watchlist import PUSH_EVENT, Watchlist
from .wsclient import AgentWsClient, AgentWsError, TurnResult, Unauthorized

_log = structlog.get_logger(__name__)

#: 建连/保活的节拍。线上 nginx 对 /agent-ws 配的是 600s 空闲上限，30s 一拍既够续命，
#: 也让「连接已经死了」最多 30s 就被发现（而不是攒到第一条真消息上）。
KEEPALIVE_INTERVAL_SECONDS = 30.0

#: 候选人会话名多久同步一次（api2 §4.2）。它问的是业务后端、不碰微信，不受节拍红线约束；
#: 但也没必要勤——名单变化很慢，而且增量同步只取水位之后的。
CONTACTS_SYNC_INTERVAL_SECONDS = 600.0

#: 调试会话在会话表里占的「对端」。前后各两条下划线，撞不上任何真实 wxid。
#: 存进同一张表是为了让它和真实会话一样**长命**——关掉窗口再打开，上下文还在。
DEBUG_PEER_ID = "__debug__"

#: 这轮告诉 AI 端「我在跟谁说话」用的名字（078 拿它当关系记忆的键）。
#: 明写成调试，免得调试时说的话被记成某个真实联系人的关系记忆。
DEBUG_PEER_LABEL = "调试会话"

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
        contacts: ContactDirectory | None = None,
        candidates: CandidateDirectory | None = None,
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
        self._contacts = contacts
        self._candidates = candidates
        self._capabilities = capabilities
        # account_id → (报给了哪条连接, 报的是什么)。**连接也要记**，见 _report_capabilities。
        self._reported_capabilities: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._command_tasks: set[asyncio.Task[None]] = set()
        self._debug_session_id: str | None = None
        self._command_listener: Callable[[dict[str, Any]], None] | None = None
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
            candidates=candidates,
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
            active_poll_interval_seconds=clamp_interval(
                settings.active_poll_interval_seconds,
                MIN_READ_INTERVAL_SECONDS,
                what="聊着时的读消息间隔",
            ),
            hot_window_seconds=settings.hot_window_seconds,
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
        if ev == PACING_EVENT:
            self._apply_pacing(data)
            return
        if ev == "card.update":
            self._on_card_update(data)
            return
        if ev == NOTICE_EVENT:
            self._on_notice(data)
            return
        _log.debug("收到未处理的推送事件", ev=ev)

    def _on_notice(self, data: dict[str, Any]) -> None:
        """AI 端说「这一轮发生了什么」（``wechat.notice``）——**只写日志，不做任何事**。

        为什么需要它：这台电脑上原本只在「有指令下来」时才有动静。后端按计划触发的一轮，
        如果 AI 判断这次不用发消息，本机日志里一个字都没有 —— 拿着电脑的人分不出
        「AI 没收到」「AI 觉得不用做」「投递丢了」哪一种。这条把那段空白填上。

        ★ 它不带用户原话（那可能是简历正文或联系方式），只带调度事实。
        """
        text = str(data.get("text") or "").strip()
        if not text:
            return
        _log.info("AI 端消息", 内容=text, 来源=str(data.get("source") or "") or None)

    def _on_card_update(self, data: dict[str, Any]) -> None:
        """指令回执（`kind=wechat_command_result`）。转给听众，并留一条日志。

        日志这条是**新加的**：在此之前，一条指令有没有真下发下来、失败在哪一步，
        本机日志里一个字都没有——AI 端知道（它那边记了 `no wechat gateway online`），
        而拿着这台电脑的人什么都看不到。
        """
        card = data.get("card")
        body = card.get("body") if isinstance(card, dict) else None
        if not isinstance(body, dict) or body.get("kind") != "wechat_command_result":
            return
        status = str(body.get("status") or "")
        reason = str(body.get("reason") or "")
        if status in {"rejected", "failed", "unsupported", "no_gateway"}:
            _log.warning("指令没能执行", op=body.get("op"), status=status, reason=reason)
        else:
            _log.info("指令回执", op=body.get("op"), status=status)
        listener = self._command_listener
        if listener is None:
            return
        try:
            listener(dict(body))
        except Exception as exc:  # noqa: BLE001 - 界面出错不该掀翻读循环
            _log.warning("指令回执回调异常", error=str(exc))

    def _apply_pacing(self, data: dict[str, Any]) -> None:
        """AI 端下发节拍（``wechat.pacing``）：让它按业务状态调快慢。

        典型用法：这轮回完、正等对方回话 → 下发快档；聊完/转人工 → 回到慢档。
        **下限仍在网关这边把住**（``InboundLoop.set_pace`` 的 ``floor``）——节拍是防风控的
        红线，不能由对端说了算；下发方只能在允许的范围内表达「盯紧点/放松点」。
        """
        def number(key: str) -> float | None:
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) and value >= 0 else None

        self.loop.set_pace(
            idle_seconds=number("idle_seconds"),
            active_seconds=number("active_seconds"),
            hot_window_seconds=number("hot_window_seconds"),
            floor=MIN_READ_INTERVAL_SECONDS,
        )

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
        # 先把会话名报给后端，再回执给 AI 端：报失败只记账（ContactDirectory 内部吞掉），
        # 顺序上放前面只是为了让「加好友成功」和「后端知道这个备注名」尽量同时发生。
        try:
            await self._report_contact(command, result)
        except Exception as exc:  # noqa: BLE001 —— 会话名上报永远不该影响回执
            _log.warning("会话名上报异常", error=str(exc))
        payload = (
            result.to_frame_data()
            if result is not None
            else {"command_id": command_id, "status": "failed", "reason": "网关内部错误"}
        )
        await report(str(payload.pop("status")), **{k: v for k, v in payload.items()
                                                    if k != "command_id"})

    async def _sync_contacts(self) -> None:
        """把本员工的候选人会话名映射拉回本机（api2 §4.2）。**启动时一次，之后周期性**。

        **增量优先**：本机记的水位之后的才要。第一次装机没有水位，那次是全量。

        为什么不是只在启动时拉一次：这台机器可能一开就是好几天，而名单在企业端随时会变
        （HR 在网页上改了备注、别的设备报了新映射）。只拉一次的话，这中间加的好友照样会被
        起第二个备注名——正是这份同步要防的事。
        """
        if self._contacts is None or not self._contacts.configured:
            return
        while True:
            await self._sync_contacts_once()
            await asyncio.sleep(CONTACTS_SYNC_INTERVAL_SECONDS)

    async def _sync_contacts_once(self) -> None:
        """同步一轮。失败只记账——同步不上不该把网关带停（ContactDirectory 自己不抛）。"""
        if self._contacts is None:
            return
        for account_id in self._accounts:
            since = self._store.contacts_synced_until(account_id)
            rows = await self._contacts.pull(updated_since=since)
            if rows:
                saved = self._store.save_contacts(account_id, rows)
                _log.info("候选人会话名已同步", account=account_id, saved=saved, incremental=bool(since))

    async def _report_contact(self, command: dict[str, Any], result: CommandResult | None) -> None:
        """加好友这条链走到一步就报一次会话名（api2 §4.1）。

        触发的是 `commands.py` 算出来的 `friend_state`——**它只在真动了微信时才有值**
        （dry-run 不报，否则后端会以为好友已经加上了）。`number`/`remark` 取原指令的参数：
        备注名就是我们真在微信里设的那个，不是猜的。
        """
        if self._contacts is None or result is None:
            return
        state = str((result.result or {}).get("friend_state") or "")
        if not state:
            return
        args = (command or {}).get("args") or {}
        command_id = str(command.get("command_id") or "")

        if state == CONTACT_ACCEPTED and not args.get("number"):
            # 我方通过了对方的申请（`accept_new_friends`）：手上只有对方显示名，没有号，
            # 而 `number` 是上报必填。名字若能在本机映射里对上就补出来，对不上就如实不报——
            # **不编一个号**，后端拿它归位候选人，编的会归到别人头上。
            await self._report_accepted_by_name(result, command_id)
            return

        number, remark = str(args.get("number") or ""), str(args.get("remark") or "")
        if state == CONTACT_REQUESTED and number and remark:
            # 先落本机再上报：上报可能失败，而「以后要认出对方通过了」这件事不能跟着失败。
            self._store.note_friend_request(self._account(), number, remark)
        await self._contacts.report(
            number=number, remark=remark, status=state, command_id=command_id,
        )

    def _account(self) -> str:
        """指令归哪个号。现在一条连接一个号（`CommandExecutor` 也是取第一个）。"""
        return self._accounts[0] if self._accounts else ""

    async def _report_accepted_by_name(self, result: CommandResult, command_id: str) -> None:
        """我方通过的那几位，凡是本机认得出号的都报一条 `accepted`。"""
        account_id = self._account()
        items = (result.result or {}).get("items") or []
        for item in items if isinstance(items, list) else []:
            name = str(item or "").strip()
            if not name:
                continue
            number = self._store.contact_number_for(account_id, name)
            if not number:
                _log.info("通过了好友申请但本机不认识这个名字，不上报")  # 名字不进日志
                continue
            await self._contacts.report(
                number=number, remark=name, status=CONTACT_ACCEPTED, command_id=command_id,
            )
            self._store.mark_contact_status(account_id, number, CONTACT_ACCEPTED)

    async def _note_friend_accepted(self, account_id: str, peer_id: str) -> None:
        """收到这个人的消息 = 他通过了我们的申请（api2 §4.1「检测到对方通过」）。

        为什么用「来消息」当信号：这是**免费且确定**的一条——微信里能收到私聊消息就说明
        已经是好友了。另一条路是去查好友列表，那要真动微信、要占节拍配额，为一个能白捡的
        事实不值得（[[wechat-rate-control]]）。只在还挂着 `requested` 时报，报完落地，
        所以同一个人只会报一次。
        """
        if self._contacts is None:
            return
        number = self._store.pending_friend_request(account_id, peer_id)
        if not number:
            return
        await self._contacts.report(
            number=number, remark=peer_id, status=CONTACT_ACCEPTED,
        )
        self._store.mark_contact_status(account_id, number, CONTACT_ACCEPTED)
        _log.info("对方通过了好友申请，已上报", account=account_id)

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

        - **跑完了但一条都没发出去**（AI 端返回错误、配额用完、不在时间窗）→ 不抛异常，
          在 `DeliveryResult.follow_up` 上标出来，同样**不销账**。原来这类只要不抛异常
          就算成功，候选人的消息被永久丢弃、积压指示灯还归零（审计 P0-4）。
        """
        # 收到他的消息就说明好友已经加上了——趁这一刻上报（api2 §4.1「检测到对方通过」）。
        # 放在对话之前：这个事实此刻就成立了，对话失败重投时它已经报过、不会重复报。
        await self._note_friend_accepted(account_id, peer_id)
        card = await self._run_dialog_turn(account_id, peer_id, transcript, messages)
        if card is None:
            # AI 端明确报错：fallback 卡里没有 reply_segments，发不出任何东西。
            # 这一轮**没有结论**，留着下一趟重投（计一次失败，有上限）。
            return DeliveryResult(
                skipped_reason="AI 端这一轮没有给出结果", follow_up=FOLLOW_UP_RETRY
            )
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
                    refs=self._peer_refs(account_id, peer_id),
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
        self, client: AgentWsClient, account_id: str, peer_id: str, task_type: str | None = None
    ) -> str:
        """取该联系人的长命会话；没有就建，有就在本连接上接回来一次。

        ``task_type`` 只在**新建**时用得上（接回来的会话是什么类型就是什么类型），
        默认走入站回复那一种；调试会话传自己的（见 ``debug_turn``）。"""
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
            session_id = await client.start_session(task_type or self._settings.task_type)
            self._store.put_session(account_id, peer_id, session_id)
            self._pool.mark_resumed(account_id, session_id)
        return session_id

    async def debug_turn(
        self,
        text: str,
        *,
        on_delta: Callable[[str], None] | None = None,
        on_stage: Callable[[dict[str, Any]], None] | None = None,
    ) -> TurnResult:
        """调试会话：把一句人话发给 AI 端，**不经过微信、也不往微信发东西**。

        这是「调试会话」那一页的后端。走的路和线上完全一样，只是入口不同：

            界面输入框 → 本方法 → 常驻那条 WS → AI 端 wechat_ops → 指令卡
                       → AI 端按 tenant+user 找回**同一条**连接下发 wechat.command
                       → `_on_push` → `_dispatch_commands` → 队列串行执行

        最后那半截一个字都没改：指令怎么下来、怎么守门、怎么回执，和线上一模一样。
        所以这一页测出来的东西是**真的**——它不是一条给调试用的旁路。

        ★ 为什么不复用 `handle_turn`：那条路末尾是 `self._sender.deliver(...)`，
        会把回复真的发到某个微信联系人那儿去。调试会话没有「对方」，产出只回界面。

        断线只重试一次，和对话轮同一个规矩：重试不了就把错误如实交给界面，
        别把「AI 端连不上」显示成「AI 端没话说」。
        """
        if not self._accounts:
            raise AgentWsError("这台机器上没有可用的微信账号")
        account_id = self._accounts[0]
        for attempt in (1, 2):
            try:
                client = await self._pool.acquire(account_id)
                session_id = await self._ensure_session(
                    client, account_id, DEBUG_PEER_ID, self._settings.debug_task_type
                )
                self._debug_session_id = session_id
                return await client.send_message(
                    session_id,
                    text,
                    extra={"wechat": {"peer": DEBUG_PEER_LABEL, "funnel": "debug"}},
                    on_delta=on_delta,
                    on_stage=on_stage,
                )
            except Unauthorized:
                self._store.drop_session(account_id, DEBUG_PEER_ID)
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise
            except AgentWsError:
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise
        raise AgentWsError("调试会话未能完成")  # 走不到；让类型检查看得出这里一定有产出

    async def interrupt_debug(self) -> None:
        """打断调试会话正在跑的那一轮（界面上的「停止」）。

        打断只是让 AI 端别再往下算了；**已经下发出去的指令不会被撤回**——那些已经在
        本机队列里排着，撤回它们要另一套（也更危险）的机制。所以界面上这个按钮说的是
        「停止」，不是「撤销」。
        """
        session_id = self._debug_session_id
        if not session_id or not self._accounts:
            return
        client = await self._pool.acquire(self._accounts[0])
        await client.interrupt(session_id)

    async def sync_watched(self) -> dict[str, Any]:
        """请 AI 端把名单重发一份（配置中心里点的「同步名单」）。

        新名单从 ``wechat.watchlist`` 推回来、走 `_on_push` 落地，和平时那条路一模一样；
        这个协程的返回值只是回执（``{watching, blocked}``），给界面照实话报数用。
        """
        if not self._accounts:
            raise AgentWsError("这台机器上没有可用的微信账号")
        client = await self._pool.acquire(self._accounts[0])
        return await client.sync_watchlist()

    async def remove_watched(self, name: str) -> dict[str, Any]:
        """把一个会话移出自动回复名单（配置中心里点的那个删除）。

        走常驻那条 WS，删在 AI 端；删完 AI 端会立刻把新名单推回来，本机名单跟着变
        （`_on_push` → `Watchlist.apply_push`），界面不用自己去改那份列表。
        """
        if not self._accounts:
            raise AgentWsError("这台机器上没有可用的微信账号")
        client = await self._pool.acquire(self._accounts[0])
        return await client.remove_watchlist([name])

    def set_command_listener(
        self, listener: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """登记「指令回执来了」的听众（调试会话那一页用）。

        ★ 回执**不回到那一轮里**（066：一轮早就 done 了，指令还在跑）。它以
        `card.update` 单独推回来，此前无人认领 → 只记一条 debug 日志就丢了。
        这正是「界面说已下发、然后再无下文」的由来：连**没下发成功**的那条理由
        （「微信端没有在线的网关」）也一起被吞了。
        """
        self._command_listener = listener

    def _peer_refs(self, account_id: str, peer_id: str) -> list[dict[str, Any]]:
        """这一轮对面是哪个候选人（047 的 `@` 引用协议）。

        `worker_id` 是上报会话名时后端归位后回给我们的，本机一直存着却从没用过——
        AI 端因此在入站对话里完全不知道对面是谁，一切「按人回写状态 / 按人预约面试」
        都无从谈起。这里把它按既有的 refs 协议带上去，不另造一套。

        反查不到就不带（`matched: false` 是后端明说允许的常态，不是错），
        AI 端那边 refs 为空照常跑——**绝不编一个 ID 顶上**。
        """
        worker_id = self._store.contact_worker_id(account_id, peer_id)
        return [{"type": "worker", "id": worker_id}] if worker_id else []

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

    def in_quiet_hours(self) -> bool:
        """现在在不在工作时间窗之外。只读，给桌面端状态栏用。"""
        return self.loop.in_quiet_window()

    def seconds_to_next_read(self) -> float | None:
        """离下一趟读消息还有几秒（None = 现在不会读）。只读，给桌面端状态栏用。"""
        return self.loop.seconds_to_next_poll()

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
        # 会话名同步放**后台**跑：它是「换机之后别把同一个人叫成两个名字」的补课，
        # 不该挡住网关起来（后端慢或没登录时，同步失败只记账，见 ContactDirectory）。
        side_tasks.append(asyncio.create_task(self._sync_contacts(), name="gw-contacts"))
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
        try:
            client = await self._pool.acquire(account_id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("能力上报失败", error=str(exc))
            return
        # ★ 「变了才报」里的**变**必须同时看连接：AI 端的网关册子是按**连接**记的
        # （连接一关就注销），而自报能力表正是这台机器入册的唯一途径。断线重连后是一条
        # 新连接、册子里没有它——此时若因为「内容一样」就不报，这台机器再也不入册，
        # 之后每条微信指令都死在 no gateway，而两边日志都显示「已连接」。
        if self._reported_capabilities.get(account_id) == (client, frame["capabilities"]):
            return
        try:
            await client.notify(CAPABILITIES_OP, frame)
        except Exception as exc:  # noqa: BLE001
            _log.warning("能力上报失败", error=str(exc))
            return
        self._reported_capabilities[account_id] = (client, frame["capabilities"])
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
