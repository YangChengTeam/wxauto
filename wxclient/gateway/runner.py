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
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from .autofriend import AutoFriendWatcher
from .candidate_mode import CandidateModeClient
from .candidates import CandidateDirectory
from .capabilities import CAPABILITIES_OP, Capability, capabilities_frame
from .commands import (
    COMMAND_EVENT,
    NOTICE_EVENT,
    RESULT_OP,
    STAGE_RECEIVED,
    TERMINAL_STATUSES,
    CommandExecutor,
    CommandResult,
    parse_commands,
)
from .config import GatewaySettings
from .contacts import STATUS_ACCEPTED as CONTACT_ACCEPTED
from .contacts import STATUS_REQUESTED as CONTACT_REQUESTED
from .contacts import ContactDirectory
from .friendcheck import PendingFriendWatcher
from .friends import FriendWatcher
from .guard import OutboundGuard, action_limits_from, within_active_hours
from .inbound import (
    FOLLOW_UP_RETRY,
    InboundLoop,
    InboundMessage,
    MessageSource,
    format_history,
)
from .message_events import DIRECTION_INBOUND, MessageEventReporter, local_message_id
from .outbound import DeliveryResult, MessageSink, OutboundSender
from .pacing import MIN_READ_INTERVAL_SECONDS, PACING_EVENT, clamp_interval
from .schedule import ScheduleLoop, parse_schedule_at
from .sessions import CANDIDATE_MODES, GatewayStore
from .strangers import StrangerWatcher
from .ticket import TicketProvider
from .watchlist import PUSH_EVENT, Watchlist
from .wsclient import AgentWsClient, AgentWsError, TurnResult, Unauthorized

_log = structlog.get_logger(__name__)

#: 建连/保活的节拍。线上 nginx 对 /agent-ws 配的是 600s 空闲上限，30s 一拍既够续命，
#: 也让「连接已经死了」最多 30s 就被发现（而不是攒到第一条真消息上）。
KEEPALIVE_INTERVAL_SECONDS = 30.0

#: 候选人会话名多久同步一次（api2 §4.2）。它问的是业务后端、不碰微信，不受节拍红线约束。
#: ★ 从 600s 收到 120s：这条同步现在**同时是监听名单的来源**（spec 085，用户 2026-08-20
#: 「就用 /client/contacts，不要新增一个接口」）。HR 在工作台圈定一个人之后，隔多久分身
#: 才认得他，就等于这个数——十分钟太久，那期间他先开口就没人接。增量同步只取水位之后的，
#: 勤一点也不贵。
CONTACTS_SYNC_INTERVAL_SECONDS = 120.0

#: 消息事件发件箱多久清一次（业务后端据这些事件排「未回复跟进」）。
#: 它只问业务后端、不碰微信，不受节拍红线约束。20s 是「候选人回话到后端取消待跟进」
#: 的最大延迟——跟进本身是小时级的，再勤没有意义；再慢则会在临界点上多追一条。
MESSAGE_EVENT_FLUSH_INTERVAL_SECONDS = 20.0

#: 回执发件箱多久补发一次。它只发一帧 WS、不碰微信，不受节拍红线约束。
#: 20s = 连接恢复到那条计划项被销账的最大延迟；业务后端的等待窗是 180s，够补回去。
RECEIPT_FLUSH_INTERVAL_SECONDS = 20.0

#: 一轮最多补几条。压着几百条时不要在一轮里全打出去，把读循环挤住。
RECEIPT_FLUSH_BATCH = 20

#: 回执压多久就不再补。★ 必须跟 AI 端在途表的存活期对齐（`wechatChannel.ts` 的
#: `PENDING_TTL_MS` = 24h）：那边已经忘了这条指令，再补只会落在「unknown command」上。
RECEIPT_MAX_AGE_HOURS = 24

#: 退出前最多等发件箱清多久。专员晚上关掉程序时，白天最后那条「候选人回话了」如果还压在
#: 本机，后端就会照旧追过去——所以宁可多等这两秒。清不完也没丢：发件箱是落盘的。
MESSAGE_EVENT_DRAIN_SECONDS = 2.0

#: 每轮往上带多少条历史。够 AI 端判「这一步走完没有」，又不至于把每轮的输入撑成一倍——
#: 这段文本每轮都要重新发一遍，是按 token 计费的。
HISTORY_TURNS = 30

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
        stranger_watcher: StrangerWatcher | None = None,
        command_executor: CommandExecutor | None = None,
        contacts: ContactDirectory | None = None,
        candidates: CandidateDirectory | None = None,
        message_events: MessageEventReporter | None = None,
        client_factory: Callable[..., AgentWsClient] | None = None,
        clock: Callable[[], datetime] | None = None,
        capabilities: Callable[[], dict[str, Capability]] | None = None,
        candidate_mode: CandidateModeClient | None = None,
        auto_friend: AutoFriendWatcher | None = None,
        friend_checker: PendingFriendWatcher | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._accounts = tuple(accounts)
        self._connect_error: str | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._watchlist = watchlist
        self._friends = friend_watcher
        self._strangers = stranger_watcher
        self._commands = command_executor
        self._auto_friend = auto_friend
        self._friend_checker = friend_checker
        self._contacts = contacts
        self._candidates = candidates
        self._message_events = message_events
        self._capabilities = capabilities
        # 091：候选人级接管同步业务后端的那条路。没配（没登录/没接口地址）也照常跑——
        # 本机那道闸不依赖它，同步不上去只是「外面还不知道」。
        self._candidate_mode = candidate_mode
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
            message_events=message_events,
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
            on_peer_messages=self._note_inbound_messages,
        )
        # 092：名单里连续几轮读不到的那个会话，交给 AutoFriendWatcher 去判「是不是压根还不是好友」。
        # 到这一步才装：它要用 `CommandExecutor` 和 `_report_contact`，两个都比 source 晚。
        if auto_friend is not None and hasattr(source, "set_unreachable_handler"):
            auto_friend.on_added = self._report_contact
            source.set_unreachable_handler(auto_friend.on_unreachable)
        # 093：反过来的那半边——已申请、还没通过的人，隔一阵复查一次是不是通过了。
        # 092 管「在名单里却不是好友」，093 管「是好友了却不在名单里」。
        if friend_checker is not None:
            friend_checker.on_accepted = self._on_friend_accepted

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
        commands = parse_commands(data)
        # 先判帧读不读得懂，再判做不做得了：读不懂时 `commands` 是空的，那一列表拿去回拒
        # 等于一条都不回——而帧本身可能带着 `command_id`，回得了话。
        if not commands:
            # 帧里既没有 commands 数组、op 又是空的。带了 command_id 就还回得了话；
            # 连 command_id 都没有才是真的无从回起（那条 AI 端也挂不上在途表）。
            _log.warning("收到读不懂的微信指令帧")
            self._reject_commands(account_id, [data], "这条指令读不懂：缺少 op")
            return
        if self._commands is None:
            # ★ 不能只写日志就完事：AI 端那条指令会一直挂在在途表里等回执，业务后端
            #   超时重投又被幂等挡掉，这条计划项**永远发不出去且被记成已处理**。
            #   做不了就明说做不了（074：「做不了」和「不让做」给用户的话完全两样）。
            _log.warning("收到微信指令但没有执行器，已回拒", 条数=len(commands))
            self._reject_commands(account_id, commands, "这台电脑上的网关没有配指令执行器")
            return
        for command in commands:
            if self._defer_if_scheduled(account_id, command):
                continue
            task = asyncio.create_task(self._run_command(account_id, command))
            self._command_tasks.add(task)
            task.add_done_callback(self._command_tasks.discard)

    def _reject_commands(
        self, account_id: str, commands: Sequence[dict[str, Any]], reason: str
    ) -> None:
        """这几条一条都做不了 → 逐条回一个 `rejected`，别让 AI 端干等到超时。"""
        for command in commands:
            command_id = str((command or {}).get("command_id") or "").strip()
            if not command_id:
                continue
            task = asyncio.create_task(
                self._report(account_id, command_id, "rejected", reason=reason)
            )
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
        """把一个阶段报回 AI 端。报不出去不影响已做完的动作，但**终态会落盘等补发**。

        ★ 终态（done/failed/rejected）和中间态（received/started/scheduled）待遇不同：
          终态是 AI 端「这条到底发出去没有」的唯一凭据（`docs/17 §5`），丢一条 = 那条计划项
          在 AI 端永远挂在在途表里，业务后端超时重投又被幂等挡掉，**永远发不出去且被记成
          已处理**。中间态只是刷卡片，隔了几分钟才补上去反而是噪音——发不掉就算了。
        """
        payload = {"command_id": command_id, "status": status, **extra}
        terminal = status in TERMINAL_STATUSES and bool(command_id)
        if terminal:
            # 先写盘再发：发到一半进程被杀，重启后这条还在。
            # ★ 落盘时间用 runner 自己的钟（`_clock`），别让 store 去取真实时间：
            #   过期判定（24h）用的就是这个钟，两边各取各的，时间线就对不上了。
            #   生产上两者本来是同一个值，所以这不改行为——但测试注入时钟时，
            #   「入库用真实 now、过期用注入 now」会让同一条用例今天绿明天红
            #   （2026-08-28 撞到：入库时刻反而晚于「25 小时后」，于是过期的那条清不掉）。
            self._store.queue_receipt(account_id, command_id, payload, at=self._clock())
        try:
            client = await self._pool.acquire(account_id)
            await client.notify(RESULT_OP, payload)
        except Exception as exc:  # noqa: BLE001
            _log.warning("指令回执发送失败", error=str(exc), status=status, 待补发=terminal)
            return
        if terminal:
            self._store.drop_receipt(account_id, command_id)

    async def _flush_receipts(self) -> None:
        """把没送到的终态回执补发出去。**启动时先跑一轮**（上次进程是被杀掉的那种情况）。"""
        while True:
            await self._flush_receipts_once()
            await asyncio.sleep(RECEIPT_FLUSH_INTERVAL_SECONDS)

    async def _flush_receipts_once(self) -> None:
        """补发一轮。发不掉就留着下一轮再来——这条链断了不该把网关带停。"""
        stale = self._store.purge_receipts_before(
            self._clock() - timedelta(hours=RECEIPT_MAX_AGE_HOURS)
        )
        for row in stale:
            # 丢必须留痕：AI 端那边已经忘了这条指令（在途表 24h TTL），补也补不上了。
            _log.warning("回执过期未送达，已丢弃", command_id=row["command_id"],
                         attempts=row["attempts"], created_at=row["created_at"])
        for row in self._store.list_receipts(limit=RECEIPT_FLUSH_BATCH):
            account_id, command_id = row["account_id"], row["command_id"]
            try:
                client = await self._pool.acquire(account_id)
                await client.notify(RESULT_OP, row["payload"])
            except Exception as exc:  # noqa: BLE001
                attempts = self._store.bump_receipt_attempts(account_id, command_id)
                _log.info("回执补发没成功，留着下一轮", command_id=command_id,
                          attempts=attempts, error=str(exc)[:100])
                # 连接这会儿不通，同一轮里后面几条也是白试。
                return
            self._store.drop_receipt(account_id, command_id)
            _log.info("回执已补发", command_id=command_id, attempts=row["attempts"])

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

    # ──────────────── 微信消息事件（未回复跟进的事实来源）────────────────

    def _note_inbound_messages(
        self, account_id: str, peer_id: str, messages: Sequence[InboundMessage]
    ) -> None:
        """候选人发来消息 → 登记 inbound 事件，业务后端据此**取消**待执行的未回复跟进。

        由 `InboundLoop` 在读到新消息那一刻回调（同步、只写本机发件箱，不发网络请求）——
        真正的上报在 `_flush_message_events` 那条循环里。

        **候选人 ID 由本机反查**（081 裁决 2：手机号↔会话名的关联全程不经 AI 端）；
        反查不到就如实跳过（契约：匹配不到 candidateId 的入站消息不要上报）。
        """
        if self._message_events is None:
            return
        candidate_id = self._store.contact_worker_id(account_id, peer_id)
        if not candidate_id:
            _log.info("这个会话还没归位到候选人，入站消息不上报", account=account_id)
            return
        for message in messages:
            self._message_events.record(
                account_id,
                candidate_id=candidate_id,
                direction=DIRECTION_INBOUND,
                # 微信 UI 读不到稳定的消息 ID（见 `wechat_io` 模块说明），拿本机那个
                # 单调序号派生一个稳定 ID；哈希掉是因为它里面带着对方的备注名。
                message_id=local_message_id(account_id, message.msg_id),
                # `at` 是**读到**这条的时刻。微信自己标的「18:28」没有日期，还原不成可信的
                # 时间点，所以宁可用读到的时刻，也不去猜一个更好看的（契约要真实发生时间，
                # 这是本机能给出的最接近的真话）。
                occurred_at=message.at,
                content=message.text,
                source="inbound_poll",
            )

    async def _flush_message_events(self) -> None:
        """把消息事件发件箱清空。失败只记账（`MessageEventReporter` 自己不抛）。"""
        if self._message_events is None:
            return
        while True:
            await self._message_events.flush()
            await asyncio.sleep(MESSAGE_EVENT_FLUSH_INTERVAL_SECONDS)

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
            # 090：刚才还不认识的人，后端已经给他建好档、会话名也归位了 → 他不再是「陌生人」。
            # 不摘掉的话，这个人会同时出现在两份来源里，而临时准入到期时会被当成过期清掉，
            # 名单反而少了一个正经候选人。
            settled = [name for name in self._store.stranger_names(account_id)
                       if name in self._store.known_session_names(account_id)]
            if settled:
                self._store.drop_strangers(account_id, settled)
                _log.info("陌生人已归位成候选人，转入正式名单", account=account_id, 数量=len(settled))
        self._apply_derived_watchlist()

    def _apply_derived_watchlist(self) -> int:
        """从本机会话名映射派生监听名单（spec 085，用户 2026-08-20 定的口径）。返回名单长度。

        ★ **空的一律不接管**。这不是洁癖：名单是 fail-closed 白名单，空名单意味着一个会话
        都不碰；而 `apply_backend` 一旦被调用就会把 AI 端那条推送永久挡掉（`_backend_seen`）。
        现网 `gateway_contacts` 眼下还是 0 行——真拿一个空名单去接管，等来的就是整台机器全哑，
        而且退不回去。所以「派生出来是空的」按「这轮没拿到」处理，沿用现有那份。
        """
        if self._watchlist is None:
            return 0
        names: list[str] = []
        seen: set[str] = set()
        for account_id in self._accounts:
            # 090：临时准入的陌生会话并进来。**并集不是替换**——他们是「还没归位的人」，
            # 归位之后 `gateway_contacts` 里就有了，这份临时准入过期自然消失。
            for name in (*self._store.watched_session_names(account_id), *self._store.stranger_names(account_id)):
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        if not names:
            return 0
        self._watchlist.apply_backend({"only": names})
        return len(names)

    async def _report_contact(self, command: dict[str, Any], result: CommandResult | None) -> None:
        """加好友这条链走到一步就报一次会话名（api2 §4.1）。

        触发的是 `commands.py` 算出来的 `friend_state`——**它只在真动了微信时才有值**
        （dry-run 不报，否则后端会以为好友已经加上了）。`number`/`remark` 取原指令的参数：
        备注名就是我们真在微信里设的那个，不是猜的。

        ★ 顺带把**昵称**（`profile_name`）报上去：名片这一趟本来就读到了，而候选人档案里的
          `wechatName` 一直是空的——这条上报是它唯一的来源（api2 §4.1）。读不准就不带，
          后端只更新带上来的字段。
        """
        if self._contacts is None or result is None:
            return
        payload = result.result or {}
        state = str(payload.get("friend_state") or "")
        if not state:
            return
        args = (command or {}).get("args") or {}
        command_id = str(command.get("command_id") or "")
        profile_name = str(payload.get("profile_name") or "")

        if state == CONTACT_ACCEPTED and not args.get("number"):
            # 我方通过了对方的申请（`accept_new_friends`）：手上只有对方显示名，没有号，
            # 而 `number` 是上报必填。名字若能在本机映射里对上就补出来，对不上就如实不报——
            # **不编一个号**，后端拿它归位候选人，编的会归到别人头上。
            await self._report_accepted_by_name(result, command_id)
            return

        number = str(args.get("number") or "")
        # ★ 备注名一律以**回执里的** `session_name` 为准，原指令的 `args` 只当兜底。
        #   有两条路会让两者不一样，而它们都只写在回执里：
        #   ① `already_friend`：这一轮没点添加、也没设过备注，微信里他现在叫什么才是实情。
        #      报我们提议的那个 remark = 给后端一个微信里根本不存在的会话名，
        #      后端照着它派下一条指令，逐字寻址随即找不到窗口。
        #   ② `commands.py` 按**后端已知的会话名**补过备注（api2 §4.2：换机不给同一个人
        #      起第二个名字）。那次补写进的是给微信的 `payload`，**原指令的 args 仍是空的**。
        #   2026-08-27 真机就栽在②上：AI 端下发的指令没带 remark，本机补上了、微信里
        #   备注也设对了，可上报读的是 args → 判成「缺 remark」整条跳过，
        #   于是后端始终不知道这个人在微信里叫什么，名单里也就永远没有他。
        remark = str(payload.get("session_name") or "") or str(args.get("remark") or "")
        if state == CONTACT_REQUESTED and number and remark:
            # 先落本机再上报：上报可能失败，而「以后要认出对方通过了」这件事不能跟着失败。
            self._store.note_friend_request(self._account(), number, remark)
        await self._contacts.report(
            number=number, remark=remark, status=state,
            profile_name=profile_name, command_id=command_id,
        )
        if state == CONTACT_ACCEPTED and number:
            # 本机也记一格，和另外两条 accepted 路径同口径（否则同一个人下次还会被当成待通过）。
            self._store.mark_contact_status(self._account(), number, CONTACT_ACCEPTED)

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

    async def _on_friend_accepted(self, account_id: str, session_name: str) -> None:
        """复查确认对方通过了（093）→ 走**既有**那条上报路，并让他立刻进监听名单。

        这里一个字的上报逻辑都不重写：`_note_friend_accepted` 本来就是干这个的
        （查号 → 报 `accepted` → 本机记一格），只是原来唯一的触发点是「收到他的消息」，
        而没进名单的人根本不会被读到消息——那段代码等的调用永远不来。复查补的就是这个调用。

        报完立刻重算名单：不重算的话，他要等下一次 `_sync_contacts`（默认几分钟一轮）
        才进得来，中间他说的话仍然没人读。
        """
        await self._note_friend_accepted(account_id, session_name)
        # 上报口没配（没登录/没接业务端）时上面那段是整段跳过的，本机那一格也就没记。
        # 不兜这一下，复查每轮都会重新确认同一个人、白动一次微信搜索，而且后面排队的谁也轮不上。
        # 服务端恢复之后同步名单会用它那份覆盖回 `requested`，再复查一次就正常报出去——能自愈。
        number = self._store.pending_friend_request(account_id, session_name)
        if number:
            self._store.mark_contact_status(account_id, number, CONTACT_ACCEPTED)
        self._apply_derived_watchlist()

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
                    extra={
                        "wechat": self._turn_meta(peer_id, messages),
                        "history": self._history_for(account_id, peer_id, messages),
                    },
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
        """现在就去取一份最新名单（配置中心里点的「同步名单」）。

        ★ spec 085：名单权威在**业务后端**，来源就是会话名同步那条既有的路
        （`GET /client/contacts`，用户 2026-08-20：不新增接口）。所以这个按钮 =
        「立刻跑一轮会话名同步，再照它重算名单」，返回 ``{watching, blocked}`` 给界面报数。

        **并存期的回退**（design §6）：派生出来是空的（后端还没数据 / 没登录 / 这一下没拉到）时，
        照旧请 AI 端重推那条老路。移交途中不能有「点了同步却什么也不会发生」的窗口——
        那正是这个按钮存在的理由（专员说不准屏幕上这份新不新）。
        """
        if self._contacts is not None and self._contacts.configured:
            await self._sync_contacts_once()
            if self._watchlist is not None and self._watchlist.watched():
                return {
                    "watching": len(self._watchlist.watched()),
                    "blocked": len(self._watchlist.scope.never),
                }
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

    def _history_for(
        self, account_id: str, peer_id: str, messages: Sequence[InboundMessage]
    ) -> str:
        """这一轮**之前**这段对话说过什么（AI 端据此判「这一步走完没有」）。

        ★ 与本轮的 ``transcript`` **分成两条**送上去：AI 端有一堆确定性判定（拒绝识别、
        来意、是不是只有表情）只该看这一轮。历史并进本轮，三天前那句「我暂时不找了」
        会每轮重新收口一次。

        本轮那几条要摘掉——它们已经在 ``transcript`` 里，再当历史送一遍，同一句话会出现两次。
        只有这台机器**亲眼读到过**的才在里面：微信里更早的聊天我们没读过，也不去翻。
        """
        rows = self._store.recent_messages(
            account_id, peer_id, limit=HISTORY_TURNS, exclude=[m.msg_id for m in messages]
        )
        return format_history(rows)

    def _is_stranger(self, peer_id: str) -> bool:
        """这个会话现在还挂在「临时准入」里吗（090）。"""
        return any(peer_id in set(self._store.stranger_names(account)) for account in self._accounts)

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

    def _turn_meta(self, peer_id: str, messages: Sequence[InboundMessage]) -> dict[str, Any]:
        """给 AI 端的这一轮元信息（064 design §2）。

        `peer` 是这个会话在微信里显示的名字。AI 端拿它当关系记忆的键（078）——
        它那边只有 session_id，从 session_id 反推不出「这是在跟谁说话」。

        `watching` 是本机当前在管的会话名（spec 085）。为什么由**网关**带上来而不是让
        AI 端自己查：名单权威移到业务后端之后，AI 端手上那份就是副本，而模型要用它做的事
        （专员说「给 aa 发条消息」时对上是哪个 aa）必须和**真正执行的那一方**看到的一致。
        带上来就只有一份名单，不会出现两处各说各话。
        """
        meta: dict[str, Any] = {
            "peer": peer_id,
            "funnel": "inbound",
            "operator_intervened": any(m.from_self for m in messages),
        }
        # 090：这个会话是**临时准入的陌生人**（我们不认识他，他主动找上来的）。
        # AI 端据此收紧第一轮：判不出对方在求职就一条都不发（spec 090 / v1 §9.5）。
        # ★ 必须由这一层说，不能让 AI 端拿「refs 是空的」去推：入站 refs 为空是常态
        #   （会话名还没归位到候选人，后端明说允许），拿它当判据会让名单里的正常联系人
        #   一起哑掉。
        if self._is_stranger(peer_id):
            meta["first_contact"] = True
        watching = self.watched_peers()
        if watching:
            meta["watching"] = watching
        return meta

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

    def candidate_modes(self) -> dict[str, Any]:
        """这台机器上所有有记录的候选人档位，按会话名索引。只读，给桌面端画名单用。"""
        if not self._accounts:
            return {}
        return self._store.candidate_modes(self._accounts[0])

    def candidate_mode(self, session_name: str) -> Any:
        """某个候选人现在由谁跟。只读（可跨线程读）。

        ★ 从没设过的那一行 `known=False`：界面上「不知道当前档」不能画成「确定是托管」。
        """
        account = self._accounts[0] if self._accounts else ""
        return self._store.candidate_mode(account, session_name)

    async def set_candidate_mode(
        self, session_name: str, mode: str, *, reason: str = "",
    ) -> dict[str, Any]:
        """招聘老师在名单里按了某个人后面那个小按钮（091）。**先落本机，再谈同步**。

        顺序就是权威的划分：接管的意思是「现在立刻别再发给他了」，它必须在断网、业务端 500、
        接口还没上线的时候照样成立。所以这里落完库就已经生效了——那道闸在 `guard.py`，纯本地。

        ★ **同步失败不回滚**（`candidate_mode.py` 那条铁律），唯独 `409` 例外：那是业务端说
          「你手上那份过期了」，本机把这一行标成欠账，等下一次同步名单把真值带回来。

        ★ **两段**：`action=request`（业务端收下）→ 本机已生效 → `action=report`（真应用了）。
          少了第二段，业务端那份 `syncStatus` 永远停在 `pending`，自动动作也不会被释放。
        """
        if mode not in CANDIDATE_MODES:
            raise ValueError(f"不认识的接管状态：{mode or '(空)'}")
        session_name = (session_name or "").strip()
        if not session_name:
            raise ValueError("没说是哪个会话")
        account = self._accounts[0] if self._accounts else ""
        if not account:
            raise ValueError("这台机器还没绑微信号")
        current = self._store.candidate_mode(account, session_name)
        # candidate_id 优先用本机这一行记着的；没有就按会话名去同步名单那份里反查。
        candidate_id = current.candidate_id or self._store.contact_worker_id(account, session_name)
        self._store.set_candidate_mode(
            account, session_name, mode,
            candidate_id=candidate_id, reason=reason, version=current.version,
        )
        _log.info("本机按下候选人接管开关", mode=mode)
        # 本机已经生效了，剩下的都是「让外面知道」——失败一律不回滚。
        note = await self._sync_candidate_mode(account, session_name, mode, reason=reason)
        row = self._store.candidate_mode(account, session_name)
        return {"mode": row.mode, "synced": note is None, "note": note or ""}

    async def _sync_candidate_mode(
        self, account: str, session_name: str, mode: str, *, reason: str,
    ) -> str | None:
        """同步给业务后端。返回 None = 同步成了；返回一句人话 = 没成（**本机不动**）。"""
        if self._candidate_mode is None:
            return "还没连上业务端，这台电脑先自己生效了"
        row = self._store.candidate_mode(account, session_name)
        if not row.candidate_id:
            # 同步名单里还没有他（陌生人、刚加上还没归位）。本机闸照样成立。
            return "这个会话还没对上劳小智里的候选人，这台电脑先自己生效了"
        asked = await self._candidate_mode.request(
            row.candidate_id, mode, reason=reason or row.reason, expected_version=row.version,
        )
        if not asked.accepted:
            if asked.conflict_version:
                # ★ 版本冲突**不猜档位**：v1.5 §6 要求「查最新状态后由用户重新操作」，
                #   响应里只有版本号。把本机这一行记成欠账，等同步名单把真值带回来。
                self._store.set_candidate_mode(
                    account, session_name, row.mode, candidate_id=row.candidate_id,
                    reason=row.reason, version=asked.conflict_version,
                )
            return asked.reason or "没能同步给业务端"
        # ★ 到这里就结束了，**没有第二腿**。业务端 2026-08-26 改成「收到 request 就直接
        #   把 requestedMode 和 appliedMode 一起置成 synced」，明确说了「不用处理 report」，
        #   所以再调 `action=report` 只会撞出一个 409 REPORT_CONFLICT 和一句吓人的提示。
        #   `reported_at` 照旧写当下时间：那一列是历史遗留，不删也不留空。
        self._store.set_candidate_mode(
            account, session_name, mode, candidate_id=row.candidate_id, reason=reason or row.reason,
            version=asked.version or row.version, mode_version=asked.mode_version,
            trigger_id=asked.trigger_id,
            reported_at=self._clock().isoformat(),
        )
        return None

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
        # 090：陌生人主动发起。**默认关**，开了才会真起循环（见 `StrangerWatcher.enabled`）。
        if self._strangers is not None:
            side_tasks.append(asyncio.create_task(self._strangers.run(), name="gw-strangers"))
        # 093：已申请、还没通过的人隔一阵复查一次（`interval<=0` 时 run() 自己就返回了）。
        if self._friend_checker is not None:
            side_tasks.append(
                asyncio.create_task(self._friend_checker.run(), name="gw-friend-check")
            )
        # 会话名同步放**后台**跑：它是「换机之后别把同一个人叫成两个名字」的补课，
        # 不该挡住网关起来（后端慢或没登录时，同步失败只记账，见 ContactDirectory）。
        # ★ 它同时是**监听名单**的来源（085，用户 2026-08-20：就用 /client/contacts，不新增接口），
        # 所以这条循环停了不只是「备注名会重」，而是「HR 新圈的人分身认不出来」。
        side_tasks.append(asyncio.create_task(self._sync_contacts(), name="gw-contacts"))
        # 消息事件同样后台清：它决定后端排不排「未回复跟进」，但一条都不该挡住微信侧。
        if self._message_events is not None:
            side_tasks.append(
                asyncio.create_task(self._flush_message_events(), name="gw-msg-events")
            )
        # 回执补发：终态回执是 AI 端唯一的销账凭据，发不出去的那些要一直补到送达
        # （`docs/17 §5`）。**无条件起**——它不依赖任何可选组件。
        side_tasks.append(asyncio.create_task(self._flush_receipts(), name="gw-receipts"))
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
        await self._drain_message_events()
        await self._pool.close_all()

    async def _drain_message_events(self) -> None:
        """退出前把发件箱再清一次（有上限）。

        专员晚上关掉程序时，白天最后那条「候选人回话了」如果还压在本机，后端就会照旧
        四小时后追过去——而它已经排好队了，只差一个 HTTP 往返。清不完也不算丢：
        发件箱是落盘的，下次开机第一趟就补上。
        """
        if self._message_events is None:
            return
        try:
            await asyncio.wait_for(
                self._message_events.flush(), timeout=MESSAGE_EVENT_DRAIN_SECONDS
            )
        except TimeoutError:
            _log.warning("退出时消息事件没清完，留到下次开机再报",
                         pending=self._message_events.pending())
        except Exception as exc:  # noqa: BLE001 —— 退出路径上不许再抛
            _log.warning("退出时清消息事件出错", error=type(exc).__name__)
