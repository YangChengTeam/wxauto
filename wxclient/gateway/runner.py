"""编排：把协议层（换票 + WS）与业务层（入站 + 出站）接成一条回路。

一条入站消息的完整旅程：

    轮询取到 → 去重 → 去抖聚合 → [本文件] 取连接 → 建/接会话 → 发一轮 → 收卡
             → 守门 → 逐段投递 → 队列串行发出

连接策略（``CLAUDE.md`` §4.1）：**一个招聘专员一条 WS 连接**，因为 AI 端一条连接
绑定单一身份。连接常驻，票只在建连那一刻用一次。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from .. import logrole
from .agent_files import AgentFileUploader
from .attachments import (
    LEFT_BEHIND_TEXT,
    MATERIAL_TYPE_TEXT,
    PROGRESS_EVENT,
    collect_attachments,
    to_inline,
)
from .autofriend import AutoFriendWatcher
from .candidate_mode import CandidateModeClient
from .candidates import CandidateDirectory
from .capabilities import CAPABILITIES_OP, Capability, capabilities_frame
from .commands import (
    COMMAND_EVENT,
    MESSAGE_EVENT_OPS,
    NOTICE_EVENT,
    RESULT_OP,
    STAGE_RECEIVED,
    TERMINAL_STATUSES,
    CommandExecutor,
    CommandResult,
    accepted_names,
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
from .moments import MomentsWatcher
from .outbound import DeliveryResult, MessageSink, OutboundSender
from .pacing import PACING_EVENT, clamp_interval
from .schedule import ScheduleLoop, parse_schedule_at
from .sessions import CANDIDATE_MODES, GatewayStore
from .ticket import TicketProvider
from .watchlist import PUSH_EVENT, Watchlist
from .wsclient import AgentWsClient, AgentWsError, TurnResult, Unauthorized

_log = structlog.get_logger(__name__)

#: 直接操作微信客户端的那几行。
_wechat = _log.bind(**{logrole.ROLE_KEY: logrole.WECHAT})
#: 跟 AI 端打交道的那几行（那条 WS：派轮 / 卡片 / 指令 / 回执 / 名单 / 能力）。
_ai = _log.bind(**{logrole.ROLE_KEY: logrole.AI})
#: 跟业务后端打交道的那几行（Client Token 走的 HTTP）。
_backend = _log.bind(**{logrole.ROLE_KEY: logrole.BACKEND})


def _log_attachment_progress(peer_id: str, file_name: str, note: str) -> None:
    """附件进度的最后一步（4/4）走内联兜底时打这一行；上传成功那条在 `agent_files` 里打。"""
    _ai.info(PROGRESS_EVENT, 会话=peer_id, 文件=file_name, 步骤="4/4", 说明=note)

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

#: 一轮跑多久没收口就往实时日志喊一声「还在跑」。30 秒：短到「卡住了」当场看得见，
#: 长到正常一轮（几十秒）最多喊一两声，不会把日志刷满。这个数只影响日志，不影响超时。
TURN_HEARTBEAT_SECONDS = 30.0

#: 每轮往上带多少条历史。够 AI 端判「这一步走完没有」，又不至于把每轮的输入撑成一倍——
#: 这段文本每轮都要重新发一遍，是按 token 计费的。
HISTORY_TURNS = 30

#: 调试会话在会话表里占的「对端」。前后各两条下划线，撞不上任何真实 wxid。
#: 存进同一张表是为了让它和真实会话一样**长命**——关掉窗口再打开，上下文还在。
DEBUG_PEER_ID = "__debug__"

#: 这轮告诉 AI 端「我在跟谁说话」用的名字（078 拿它当关系记忆的键）。
#: 明写成调试，免得调试时说的话被记成某个真实联系人的关系记忆。
DEBUG_PEER_LABEL = "调试会话"

#: 本机朋友圈巡看自触发 AI 端写评论时用的「对端」（同 DEBUG_PEER_ID 的造法）。
#: 长命会话：AI 端在同一条会话里能看见前几趟评过什么，不至于每趟同一句话。
MOMENTS_PEER_ID = "__moments__"
#: 自触发 AI 端用的 task_type——102 的评论轮就走这一种（`content_mode: comment_moments`）。
MOMENTS_TASK_TYPE = "wechat_outreach"

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
            _ai.info("已为该账号建立 AI 端连接", account=account_id)
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
        message_events: MessageEventReporter | None = None,
        client_factory: Callable[..., AgentWsClient] | None = None,
        clock: Callable[[], datetime] | None = None,
        capabilities: Callable[[], dict[str, Capability]] | None = None,
        candidate_mode: CandidateModeClient | None = None,
        auto_friend: AutoFriendWatcher | None = None,
        friend_checker: PendingFriendWatcher | None = None,
        moments_watcher: MomentsWatcher | None = None,
        file_uploader: AgentFileUploader | None = None,
    ) -> None:
        self._settings = settings
        # 2026-09-28：对方发来的图片/文件先直接传到 AI 端的会话拿 file_id，传不上去才随消息内联。
        self._uploader = file_uploader
        self._store = store
        self._accounts = tuple(accounts)
        self._connect_error: str | None = None
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._watchlist = watchlist
        self._friends = friend_watcher
        self._commands = command_executor
        self._auto_friend = auto_friend
        self._friend_checker = friend_checker
        # 本机朋友圈巡看（用户 2026-09-08）：新动态点赞在它自己手上；评论要 AI 端写，
        # 由它回调这里自触发一轮（`_comment_new_moments`）。
        self._moments = moments_watcher
        if self._moments is not None:
            self._moments.on_new_posts = self._comment_new_moments
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
        #: `_note_derived_empty` 的去重签名（本机会话名数:生效名单数:来源）。纯日志用。
        self._derived_empty_note = ""
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
        if settings.demo_mode:
            # 「改了没生效」是老问题——演示模式开没开，实时日志里要一眼看得见。
            _wechat.warning(
                "演示模式已开：下限已解除、不加随机抖动（仅限演示号）",
                读消息秒=settings.demo_poll_interval_seconds,
                好友秒=settings.demo_friend_interval_seconds,
                朋友圈秒=settings.demo_moments_interval_seconds,
                说明="工作时间窗与配额不变",
            )
        self.loop = InboundLoop(
            accounts=accounts,
            source=source,
            store=store,
            handler=self.handle_turn,
            debounce_seconds=settings.debounce_seconds,
            poll_interval_seconds=clamp_interval(
                settings.poll_interval_seconds, settings.read_floor_seconds, what="读消息间隔"
            ),
            active_poll_interval_seconds=clamp_interval(
                settings.active_poll_interval_seconds,
                settings.read_floor_seconds,
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
        # 连续几轮都「查无此人」→ 移出监听名单（用户 2026-09-24）。跟 092 各装各的：
        # 自动补加好友关着的机器上也得生效。
        if hasattr(source, "set_not_found_handler"):
            source.set_not_found_handler(self._on_contact_not_found)
        # 093：反过来的那半边——已申请、还没通过的人，隔一阵复查一次是不是通过了。
        # 092 管「在名单里却不是好友」，093 管「是好友了却不在名单里」。
        if friend_checker is not None:
            friend_checker.on_accepted = self._on_friend_accepted
        # 2026-09-12 自动通过别人发来的好友申请：通过之后先临时纳入监听（他发的第一句才读得到），
        # 认得出号的照旧报给业务端。
        if friend_watcher is not None:
            friend_watcher.on_accepted = self._on_friend_requests_accepted

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
        _ai.debug("收到未处理的推送事件", ev=ev)

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
        _ai.info("AI 端消息", 内容=text, 来源=str(data.get("source") or "") or None)

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
            _ai.warning("指令没能执行", op=body.get("op"), status=status, reason=reason)
        else:
            _ai.info("指令回执", op=body.get("op"), status=status)
        listener = self._command_listener
        if listener is None:
            return
        try:
            listener(dict(body))
        except Exception as exc:  # noqa: BLE001 - 界面出错不该掀翻读循环
            _ai.warning("指令回执回调异常", error=str(exc))

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
            floor=self._settings.read_floor_seconds,
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
            _ai.warning("收到读不懂的微信指令帧")
            self._reject_commands(account_id, [data], "这条指令读不懂：缺少 op")
            return
        if self._commands is None:
            # ★ 不能只写日志就完事：AI 端那条指令会一直挂在在途表里等回执，业务后端
            #   超时重投又被幂等挡掉，这条计划项**永远发不出去且被记成已处理**。
            #   做不了就明说做不了（074：「做不了」和「不让做」给用户的话完全两样）。
            _ai.warning("收到微信指令但没有执行器，已回拒", 条数=len(commands))
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
            _ai.warning("指令回执发送失败", error=str(exc), status=status, 待补发=terminal)
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
            _ai.warning("回执过期未送达，已丢弃", command_id=row["command_id"],
                         attempts=row["attempts"], created_at=row["created_at"])
        for row in self._store.list_receipts(limit=RECEIPT_FLUSH_BATCH):
            account_id, command_id = row["account_id"], row["command_id"]
            try:
                client = await self._pool.acquire(account_id)
                await client.notify(RESULT_OP, row["payload"])
            except Exception as exc:  # noqa: BLE001
                attempts = self._store.bump_receipt_attempts(account_id, command_id)
                _ai.info("回执补发没成功，留着下一轮", command_id=command_id,
                          attempts=attempts, error=str(exc)[:100])
                # 连接这会儿不通，同一轮里后面几条也是白试。
                return
            self._store.drop_receipt(account_id, command_id)
            _ai.info("回执已补发", command_id=command_id, attempts=row["attempts"])

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
            _backend.warning("会话名上报异常", error=str(exc))
        # 业务端驱动到这个人身上、而且真发成功了 → 他早就通过了我们的申请（用户 2026-09-01）。
        # 不是好友的人 `pending_friend_request` 查不到，直接返回，所以这里不加条件判断。
        try:
            await self._note_sent_to_pending_friend(account_id, command, result)
        except Exception as exc:  # noqa: BLE001 —— 同上，认不出来不该影响回执
            _backend.warning("发消息顺带认好友通过异常", error=str(exc))
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
            _backend.info("这个会话还没归位到候选人，入站消息不上报", account=account_id)
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
                message_type=message.message_type,
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

    async def _sync_contacts_once(self, *, full: bool = False) -> bool:
        """同步一轮。失败只记账——同步不上不该把网关带停（ContactDirectory 自己不抛）。

        ``full=True``（点「同步名单」）：全量拉、**以业务端为准**删掉本机多出来的人。
        后台定时那条照旧增量——全量每 120 秒拉一遍不值当，也用不着：删人是专员点了才要立刻见效。

        返回「这是不是一次每个账号都拉全了的全量」——是的话名单**照业务端原样**换上，空的也换
        （用户 2026-09-24：「同步的时候应该及时更新列表」）。
        """
        if self._contacts is None:
            return False
        complete = full and bool(self._accounts)
        for account_id in self._accounts:
            if full:
                rows = await self._contacts.pull_all()
                if rows is None:
                    complete = False
                    _backend.warning("全量同步没拉全，这次不删本机名单", account=account_id)
                    continue
                dropped = self._store.replace_accepted_contacts(account_id, rows)
                _backend.info("候选人会话名已全量同步", account=account_id, saved=len(rows), 移除=dropped)
                continue
            since = self._store.contacts_synced_until(account_id)
            rows = await self._contacts.pull(updated_since=since)
            if rows:
                saved = self._store.save_contacts(account_id, rows)
                _backend.info("候选人会话名已同步", account=account_id, saved=saved, incremental=bool(since))
        self._apply_derived_watchlist(authoritative=complete)
        return complete

    def _apply_derived_watchlist(self, *, authoritative: bool = False) -> int:
        """从本机会话名映射派生监听名单（spec 085，用户 2026-08-20 定的口径）。返回名单长度。

        ★ **空的一律不接管**。这不是洁癖：名单是 fail-closed 白名单，空名单意味着一个会话
        都不碰；而 `apply_backend` 一旦被调用就会把 AI 端那条推送永久挡掉（`_backend_seen`）。
        现网 `gateway_contacts` 眼下还是 0 行——真拿一个空名单去接管，等来的就是整台机器全哑，
        而且退不回去。所以「派生出来是空的」按「这轮没拿到」处理，沿用现有那份。

        ``authoritative``（点「同步名单」且每个账号都全量拉全了）是唯一的例外：那一刻业务端
        **明说了**一个都没有，还留着旧名单 = 屏幕上挂着业务端早已不让回的人、网关也照回不误
        （2026-09-24 用户：同步了列表却不更新）。
        """
        if self._watchlist is None:
            return 0
        names: list[str] = []
        seen: set[str] = set()
        for account_id in self._accounts:
            # ★ 只认同步来的（用户 2026-09-24：「监听逻辑是同步的，不应该自己添加」）。
            #   以前这里还并进本机「临时准入」的陌生会话（090 扫会话列表 / 通过好友申请），
            #   0.1.80 让扫描真跑起来后把「腾讯新闻」这类服务号也收进了监听——已整条拆掉。
            for name in self._store.watched_session_names(account_id):
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        if not names:
            if authoritative:
                self._watchlist.apply_backend({"only": []})
                return 0
            self._note_derived_empty()
            return 0
        # 恢复出货了 → 把「空」的去重态清掉，下次再空还得再喊一次。
        self._derived_empty_note = ""
        self._watchlist.apply_backend({"only": names})
        return len(names)

    def _note_derived_empty(self) -> None:
        """派生出来是空的——**把它喊出来**，只在情况变了的时候喊一次。

        ★ 这里原来是一句 `return 0`，完全静默，而它恰恰是最需要看见的那个状态。
        `Watchlist.warn_if_idle()` 只在**名单真空着**时才喊；一旦有别的东西把名单填上了
        （迁移期 AI 端那条推送、或者上次落盘的那份），`ready` 就是 True，于是
        「后端这条路其实早就不出货了」一声不响。2026-08-31 的现场就是这样：连着几天
        `GET /client/contacts` 被 409 顶回、派生恒为空，机器一直跑在一份旧名单上，
        而日志、界面、AI 端三头都看不出异常，最后是登进 Windows 翻 sqlite 才查出来。

        喊的内容要能直接分诊，所以带上三个数：
        - `本机会话名=0` → 同步就没成功过（新装机还没拉第一轮，或者被后端拒了——
          那就去看 `会话名接口被拒` 那条的**错误码**）。
        - `本机会话名>0` 而 `可自动回复=0` → 名单里的人全是 `requested`：好友申请发出去了，
          对方一个都还没通过。这不是故障。
        - `现在在盯` + `来源` → 此刻**真正生效**的是哪一份、有几个会话。这一项是关键：
          它说明机器并没有全哑，只是没在用后端那份。

        只在这三个数变化时说一次：同步是 120 秒一轮，照打会把日志刷满，反而把状态
        真正变化的那一刻埋掉（同 `BackendEndpoint._note_not_ready` 的口径）。
        """
        known = sum(len(self._store.known_session_names(a)) for a in self._accounts)
        effective = self._watchlist.watched() if self._watchlist is not None else []
        origin = self._watchlist.scope.origin if self._watchlist is not None else ""
        signature = f"{known}:{len(effective)}:{origin}"
        if self._derived_empty_note == signature:
            return
        self._derived_empty_note = signature
        _backend.warning(
            "后端派生的监听名单是空的，这一轮不接管——现在生效的是另一份",
            本机会话名=known,
            可自动回复=0,
            现在在盯=len(effective),
            来源=origin,
        )

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

    async def _note_sent_to_pending_friend(
        self, account_id: str, command: dict[str, Any], result: CommandResult | None,
    ) -> None:
        """给一个「还在等他通过」的人**发消息发成功了** → 他其实早就通过了。

        用户 2026-09-01：「业务端会发消息过来 不用轮询」。所以认定「他通过了」除了定时
        搜索那条路，还有这条**白捡**的：业务端下发一条发消息指令，我们真发出去了，
        那就说明微信里这个会话是通的、他已经是好友——不必再等下一轮搜索。

        ★ 只认 `done`：`failed` 里正包含「他还不是好友、发不出去」这种情况，
          拿失败当通过就把状态写反了（同 `settle-only-when-said` 的教训）。
        ★ dry_run 一律不认（同 `_friend_state` / `_note_message_sent` 的口径）。
          台账里本来就只有真实动作留下的人（dry_run 的 `add_friend` 不产 `requested`），
          但**装了真实动作的机器改回 dry_run 之后台账还在**，不把这一下就会凭空报通过。
        ★ 认的是「发消息」这一类 op，复用 `MESSAGE_EVENT_OPS`——那张表的定义就是
          「这条指令等于我们真发了一条微信消息」，两处别各写一份。
        """
        if result is None or result.status != "done":
            return
        if self._commands is None or not self._commands.allow_real:
            return
        if str((command or {}).get("op") or "").strip() not in MESSAGE_EVENT_OPS:
            return
        args = (command or {}).get("args") or {}
        peer = str(args.get("to") or args.get("peer") or "").strip()
        if peer:
            await self._note_friend_accepted(account_id, peer)

    async def _report_accepted_now(
        self, account_id: str, number: str, remark: str, command_id: str = "",
    ) -> None:
        """认出「他通过了」→ **当场**报给业务后端，并把本机台账销掉。

        2026-09-01 用户改口径：去掉原来那半小时的延迟队列（`gateway_pending_accepted`）。
        原来的理由是「刚通过就报＝人家刚点完同意系统马上动起来，等一会儿更像真人」，
        现在按用户要求改成搜到就报——发现这件事本身已经隔了一个搜索间隔了。

        ★ 报**不成功就不销账**：销了就再没有人会去看他一眼，这个人从此停在「等通过」。
          留着下一轮复查会再确认一次、再报一次，链路自愈（同 `settle-only-when-said` 的教训）。
        """
        if self._contacts is None:
            return
        try:
            result = await self._contacts.report(
                number=number, remark=remark, status=CONTACT_ACCEPTED,
                command_id=command_id, occurred_at=self._clock().isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 —— 一条报不出去不该拖垮这一趟
            _backend.warning("好友通过上报出错，留着下一轮", 原因=str(exc))
            return
        if result is None:
            # `report()` 自己已经记过为什么（缺字段 / 请求失败），这里不重复喊。
            return
        # 本机那一格照旧记（管的是「别把同一个人重复当成待通过」），台账销掉＝不再复查他。
        self._store.mark_contact_status(account_id, number, CONTACT_ACCEPTED)
        self._store.drop_friend_request(account_id, number)
        _backend.info("好友通过已上报", 会话=remark)

    def _account(self) -> str:
        """指令归哪个号。现在一条连接一个号（`CommandExecutor` 也是取第一个）。"""
        return self._accounts[0] if self._accounts else ""

    async def _report_accepted_by_name(self, result: CommandResult, command_id: str) -> None:
        """我方通过的那几位，凡是本机认得出号的都报一条 `accepted`。"""
        account_id = self._account()
        # 只报这一趟**真通过了**的（`accepted`；老驱动层没这个键时退回 `items`）。
        for name in accepted_names(result.result):
            number = self._store.contact_number_for(account_id, name)
            if not number:
                _backend.info("通过了好友申请但本机不认识这个名字，不上报")  # 名字不进日志
                continue
            await self._report_accepted_now(account_id, number, name, command_id=command_id)

    async def _on_friend_requests_accepted(
        self, command: dict[str, Any], result: CommandResult | None, names: Sequence[str],
    ) -> None:
        """自动通过了几个人的好友申请（`friends.py`）→ 认得出号的报 `accepted` 给业务端。

        ★ **不再自己把人加进监听**（用户 2026-09-24：「监听逻辑是同步的，不应该自己添加」）。
        他要被自动回复，得等业务端把他写进 `GET /client/contacts`、下一轮同步带回来。
        """
        await self._report_contact(command, result)

    async def _note_friend_accepted(self, account_id: str, peer_id: str) -> None:
        """和这个人有过一次成功的消息往来 = 他通过了我们的申请（api2 §4.1「检测到对方通过」）。

        为什么用「消息往来」当信号：这是**免费且确定**的一条——微信里发得出去/收得到私聊
        消息，就说明已经是好友了。另一条路是去查好友列表，那要真动微信、要占节拍配额，
        为一个能白捡的事实不值得（[[wechat-rate-control]]）。

        ★ **不为此轮询**（用户 2026-09-01：「业务端会发消息过来 不用轮询」）。
          触发点是业务端下发的发消息指令**发成功了**那一刻，不是我们主动去盯着他——
          `requested` 的人始终不进监听名单（085 fail-closed，名单只收 accepted）。
          曾经想过放开名单让他进轮询，代价是每人每轮白开一次不存在的会话窗口，
          而且会把后端写的「计划点名要加谁」也放进来——那些人我们根本还没加。

        只认本机台账里还挂着的人（`pending_friend_request`），不是台账里的直接返回，
        所以给任何人发消息都可以顺手调一次，代价是一次本地查表。
        """
        if self._contacts is None:
            return
        number = self._store.pending_friend_request(account_id, peer_id)
        if not number:
            return
        await self._report_accepted_now(account_id, number, peer_id)

    async def _comment_new_moments(self, account_id: str, posts: list[dict[str, Any]]) -> None:
        """本机巡看到新动态 → **自己给 AI 端起一轮** `comment_moments`，让它按素材写评论。

        走的是 102 那条现成的计划触发路，只是触发方从业务端换成了本机：
        发给 AI 端的就是一条触发项 JSON（`content_mode: comment_moments` + 素材），
        多带一份 `gateway_posts`——本机刚看回来的动态。AI 端认出是网关自己送来的，
        就直接用这份，不再往回投一条 `read_recent_moments`（省一次开朋友圈窗口）。
        AI 端回来的 `comment_moments` 指令走 `_on_push` → `_dispatch_commands`，
        能力闸、配额、节拍、同一条只评一次的台账一道不绕——评论那一侧一个字不新写。

        触发号按「账号 + 这批动态的键」派生：同一批动态再交一次（上一趟 AI 端没接住、
        游标没前移）AI 端按幂等认领挡掉，不会评两遍。
        """
        material = self._settings.moments_comment_material.strip()
        if not material or not posts:
            return
        digest = hashlib.sha1(  # 只是派生一个幂等键，不是安全用途
            "\n".join(f"{account_id}\t{post.get('content', '')}" for post in posts).encode("utf-8")
        ).hexdigest()[:16]
        trigger = {
            "trigger_id": f"local-moments:{digest}",
            "kind": "promotion",
            "reason": "本机朋友圈巡看发现新动态，请按素材给它们写评论",
            "content_mode": "comment_moments",
            "content_text": material,
            "gateway_posts": [
                {
                    "content": str(post.get("content") or ""),
                    "posted_at": str(post.get("posted_at") or ""),
                    "photos": int(post.get("photos") or 0),
                    "videos": int(post.get("videos") or 0),
                }
                for post in posts
            ],
        }
        text = json.dumps(trigger, ensure_ascii=False)
        for attempt in (1, 2):
            try:
                client = await self._pool.acquire(account_id)
                session_id = await self._ensure_session(
                    client, account_id, MOMENTS_PEER_ID, MOMENTS_TASK_TYPE
                )
                result = await client.send_message(session_id, text)
                _ai.info(
                    "新动态已交给劳小智写评论", account=account_id, count=len(posts),
                    cards=len(result.cards),
                )
                return
            except Unauthorized:
                self._store.drop_session(account_id, MOMENTS_PEER_ID)
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise
            except AgentWsError:
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise

    async def _on_contact_not_found(self, account_id: str, peer_id: str) -> None:
        """名单里这个会话连续几轮都「查无此人」→ 在本机移出监听名单，之后不再去读它（用户 2026-09-24）。

        落在本机「不管」里（同配置中心的「移出」，`Watchlist.exclude`）：业务端同步只换「只管」，
        不会把他带回来；每轮白开一次搜不到的会话，既浪费读名额，又在日志里刷一遍查无此人。
        唯一放回来的路：093 复查确认他通过了好友（`_on_friend_accepted` → `Watchlist.include`）。
        """
        if self._watchlist is None:
            return
        if self._watchlist.exclude(peer_id):
            _log.warning(
                "名单里这个会话连续几轮都查无此人，已移出监听名单，不再读它（请核对微信里的备注名）",
                会话=peer_id,
                account=account_id,
            )

    async def _on_friend_accepted(self, account_id: str, session_name: str) -> None:
        """复查确认对方通过了（093）→ 走**既有**那条上报路，并让他立刻进监听名单。

        这里一个字的上报逻辑都不重写：`_note_friend_accepted` 本来就是干这个的
        （查号 → 排队等着报 `accepted` → 本机记一格），只是原来唯一的触发点是
        「收到他的消息」，而没进名单的人根本不会被读到消息——那段代码等的调用永远不来。
        复查补的就是这个调用。

        **立刻重算名单**：他现在就该被读、被回复（不然复查白做了）。不重算的话，
        他要等下一次 `_sync_contacts`（默认几分钟一轮）才进得来，中间他说的话仍然没人读。

        ★ **报不出去就留着台账**（上报口没配、后端不可达）：下一轮复查会再确认一次、
          再报一次。代价是后端不可达时会重复搜同一个人——受 15 分钟间隔和 ≥300s 共享闸
          约束，可以接受；换来的是**上报绝不会静默丢掉**，而那正是这次要治的病
          （2026-09-01：requested 报上去了、人通过了，accepted 再没上来过）。
          销账只发生在 `_report_accepted_now` 里真报成功那一刻。
        """
        await self._note_friend_accepted(account_id, session_name)
        # 之前因「查无此人」被自动移出过的，名字现在找得到了 → 放回来，不然他永远不会被回。
        if self._watchlist is not None:
            self._watchlist.include(session_name)
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
            result = await self._sender.deliver(account_id, peer_id, card)
        except Exception as exc:  # noqa: BLE001 —— 见上：投递阶段不允许触发重投
            _wechat.warning(
                "投递回复时出错，本批不再重投（可能已经发出去一部分，重投会重复骚扰对方）",
                account=account_id,
                error=str(exc),
            )
            return DeliveryResult(skipped_reason=f"投递失败：{exc}")
        return result

    def _elapsed(self, started: datetime) -> int:
        """从 ``started`` 到现在过了几秒（取整，只用于日志）。"""
        return max(0, int((self._clock() - started).total_seconds()))

    def _note_stage(self, peer_id: str, stage: dict[str, Any]) -> None:
        """AI 端报了个阶段（055）→ 原样打进实时日志。

        ★ 可以直接显示的理由：``stage.text`` 是**服务端映射出来的**安全短句
          （见 AI 端 `src/ws/stage.ts`：codex 的命令行、cwd、搜索词、命令 stdout
          一个字节都不进这个字段），网关这边不需要再洗一遍。

        这个回调跑在 WS 读循环里，**只许打日志**——阻塞它就等于阻塞整条连接。
        """
        text = str(stage.get("text") or "").strip()
        if not text:
            return
        _ai.info("AI 端阶段", 会话=peer_id, 阶段=text, key=str(stage.get("key") or "-"))

    async def _turn_heartbeat(self, peer_id: str, started: datetime) -> None:
        """一轮还没收口就每隔 ``TURN_HEARTBEAT_SECONDS`` 说一声「还在跑」。

        没有它的时候，AI 端超时（默认 180s）再重试一次 = 最长六分钟里那台 Windows
        电脑上一个字都没有：「派发一轮给 AI 端」之后直接静默，看的人分不出是 AI 在想、
        是卡住了、还是整条链路断了（用户 2026-08-29：读到了消息但没回复，屏幕上也没有）。

        它**只打日志**，不参与任何判定，也不会让这一轮提前收口。

        ★ 自己出错就自己停下，绝不上抛：这是一条纯观测的支路，让它有本事弄坏一次
          真实回复就本末倒置了（开发时就撞过一次——心跳里一个 AttributeError
          把整轮变成了「没把话说出去」）。
        """
        try:
            while True:
                await asyncio.sleep(TURN_HEARTBEAT_SECONDS)
                _ai.info(
                    "AI 端还在跑这一轮",
                    会话=peer_id,
                    已用秒=self._elapsed(started),
                    上限秒=int(self._settings.turn_timeout_seconds),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 观测支路不许影响这一轮
            _ai.warning("这一轮的心跳自己出错了，停掉它，不影响回复", 原因=str(exc))

    async def _run_dialog_turn(
        self,
        account_id: str,
        peer_id: str,
        transcript: str,
        messages: Sequence[InboundMessage],
    ) -> dict[str, Any] | None:
        """跑一轮，返回卡片。断线只重试一次——重试不了就这一轮作罢，下轮再说。

        ★ 这一轮在 Windows 端要跑几十秒，所以从头到尾都得看得见：喂进去了什么、
          AI 端跑到哪一阶（``on_stage``，AI 端本来就在流里发，这边原来直接丢掉）、
          还在跑多久了、最后是成了还是错了、错在哪。全部只是打日志，一处判定都没动。
        """
        history = self._history_for(account_id, peer_id, messages)
        refs = self._peer_refs(account_id, peer_id)
        # 104：这一批消息另存下来的图片/文件，这一轮送过去（AI 端落到会话 inbox）。
        # 怎么送在拿到会话之后定（`_turn_files`）：先直接上传拿 file_id，不成再内联。
        attachments, left_behind = collect_attachments(messages)
        uploaded: dict[str, list[str]] = {}
        if left_behind:
            _ai.warning(
                "这一轮有附件没能带上",
                会话=peer_id,
                个数=len(left_behind),
                清单=[
                    {"文件": e["file_name"], "原因": LEFT_BEHIND_TEXT.get(e["reason"], e["reason"])}
                    for e in left_behind
                ],
            )
        for attempt in (1, 2):
            started = self._clock()
            _ai.info(
                "喂给 AI 端",
                会话=peer_id,
                本轮=len(messages),
                历史行=history.count("\n") + 1 if history else 0,
                认得这个人=bool(refs),
                附件=len(attachments),
                # 只记名 / 类型 / 大小，不记内容（另存那步的口径一样）。base64 长度 ×3/4 ≈ 原字节数，够看。
                附件清单=[
                    {"文件": a["file_name"], "类型": a["kind"], "字节": len(a["data"])}
                    for a in attachments
                ],
                第几次=attempt,
            )
            heartbeat = asyncio.create_task(self._turn_heartbeat(peer_id, started))
            try:
                client = await self._pool.acquire(account_id)
                session_id = await self._ensure_session(client, account_id, peer_id)
                extra_turn = await self._turn_files(account_id, peer_id, session_id, attachments, uploaded)
                result = await client.send_message(
                    session_id,
                    transcript,
                    refs=refs,
                    extra={
                        "wechat": self._turn_meta(peer_id, messages),
                        "history": history,
                        **extra_turn,
                    },
                    on_stage=lambda stage: self._note_stage(peer_id, stage),
                )
            except Unauthorized as exc:
                # 票据/归属出问题：会话多半已经不属于我们了，清掉重来。
                _ai.warning(
                    "AI 端不认这条会话了，清掉重来",
                    会话=peer_id, 原因=str(exc), 第几次=attempt, 已用秒=self._elapsed(started),
                )
                self._store.drop_session(account_id, peer_id)
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise
                continue
            except AgentWsError as exc:
                # 断线 / 一轮超时。第一次原来是**静默**重试的——现场表现就是长时间没有下文。
                _ai.warning(
                    "这一轮没跑通",
                    会话=peer_id, 原因=str(exc), 第几次=attempt, 已用秒=self._elapsed(started),
                )
                await self._pool.invalidate(account_id)
                if attempt == 2:
                    raise
                continue
            finally:
                # 收了就停。`suppress` 连普通异常一起吃掉：心跳是观测支路，
                # 它自己怎么坏都不该改变这一轮的结果。
                heartbeat.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await heartbeat

            if not result.ok:
                # AI 端明确报错：卡片是 fallback，没有 reply_segments，出站自然不会发东西。
                error = result.error or {}
                _ai.warning(
                    "AI 端返回错误",
                    会话=peer_id,
                    code=error.get("code") or "-",
                    原因=str(error.get("message") or "-"),
                    已用秒=self._elapsed(started),
                )
                return None
            _ai.info(
                "AI 端这一轮跑完了",
                会话=peer_id,
                已用秒=self._elapsed(started),
                有卡片=result.card is not None,
            )
            self._log_claimed_documents(peer_id, attachments, result.card)
            return result.card
        return None

    async def _turn_files(
        self,
        account_id: str,
        peer_id: str,
        session_id: str,
        files: Sequence[dict[str, Any]],
        uploaded: dict[str, list[str]],
    ) -> dict[str, Any]:
        """这一轮的附件怎么带：直接传到这个会话、只带 ``file_ids``；传不上去才随消息内联 base64。

        用户 2026-09-28：「wx 客户端直接上传到 AI 端、不用 ws」。契约 AI 服务仓 docs/21 §2.0。
        ``uploaded`` 是这一轮里已经传过的（按会话记）：断线重试接回的是**同一个**会话就不再传一遍；
        会话被清掉重建了（`Unauthorized` 那条）是新会话，得重新传。
        """
        if not files:
            return {}
        if self._uploader is not None:
            ids = uploaded.get(session_id)
            if ids is None:
                ids = await self._uploader.upload(account_id, peer_id, session_id, files)
                if ids is not None:
                    uploaded[session_id] = ids
            if ids is not None:
                return {"file_ids": ids}
        for f in files:
            _log_attachment_progress(peer_id, str(f["file_name"]), "随这一轮消息内联送到 AI 端")
        return {"attachments": to_inline(files)}

    @staticmethod
    def _log_claimed_documents(peer_id: str, attachments: Sequence[dict[str, Any]], card: Any) -> None:
        """这一轮送过去的附件，AI 端认没认成材料（104 `documents[]`）——只打日志，一处判定都不动。

        上传到后端那一步在 AI 端做（106 统一回执），这台电脑看不到结果；但**会不会上传**在卡上
        就能看出来：认领了、且带「授权原话」的那份才会上报，其余的都不会。原来这里一个字都没有，
        专员只能对着后端问「他发的身份证怎么没进档」。字段值是证件号一类的 PII，只记个数。
        """
        if not attachments:
            return
        sent = [str(a.get("file_name") or "") for a in attachments]
        body = card.get("body") if isinstance(card, dict) else None
        documents = body.get("documents") if isinstance(body, dict) else None
        claimed = [d for d in (documents or []) if isinstance(d, dict)]
        if not claimed:
            _ai.info("AI 端没认领这一轮的附件", 会话=peer_id, 送去=sent)
            return
        names = {str(d.get("file_name") or "") for d in claimed}
        _ai.info(
            "AI 端认领了材料",
            会话=peer_id,
            清单=[
                {
                    "文件": str(d.get("file_name") or ""),
                    "类型": MATERIAL_TYPE_TEXT.get(str(d.get("material_type") or ""), str(d.get("material_type") or "-")),
                    "有授权原话": bool(str(d.get("consent_note") or "").strip()),
                    "字段数": len(d.get("fields") or {}) if isinstance(d.get("fields"), dict) else 0,
                }
                for d in claimed
            ],
            未认领=[n for n in sent if n not in names],
        )

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
                _ai.info("旧会话接不回，改为新建", account=account_id)
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
            # 全量：业务端已经不让盯的人要在这一下里消失，不能等增量（它永远不会带回删掉的行）。
            complete = await self._sync_contacts_once(full=True)
            if self._watchlist is not None and (complete or self._watchlist.watched()):
                return {
                    "watching": len(self._watchlist.watched()),
                    "blocked": len(self._watchlist.scope.never),
                }
        if not self._accounts:
            raise AgentWsError("这台机器上没有可用的微信账号")
        client = await self._pool.acquire(self._accounts[0])
        return await client.sync_watchlist()

    async def remove_watched(self, name: str) -> dict[str, Any]:
        """把一个会话移出自动回复名单（配置中心里点的那个删除）。返回 ``{removed, watching, blocked}``。

        ★ 删在**本机**（`Watchlist.exclude`，用户 2026-09-24）。原来走 WS 请 AI 端删，可名单权威
          0.1.83 起在业务后端，AI 端手上没有这份名单，永远回「本来就不在」，界面那一行删不掉。
          业务后端没有「移出」接口，本机「不管」就是这里唯一能落地的地方，同步也不会把人带回来。
        """
        if self._watchlist is None:
            raise AgentWsError("监听名单还没准备好")
        removed = [name] if self._watchlist.exclude(name) else []
        return {
            "removed": removed,
            "watching": len(self._watchlist.watched()),
            "blocked": len(self._watchlist.scope.never),
        }

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
        # 名单外、看到红点才读的会话（用户 2026-09-28）：AI 端据此收紧——判不出对方在找工作就
        # 一条都不发、在找就建档接待（spec 090）。★ 必须由网关说，不能让 AI 端拿「refs 是空的」去推：
        # 名单里没归位到候选人的会话 refs 也是空的，拿它当判据会让正常联系人一起哑掉。
        if any(m.first_contact for m in messages):
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
                _ai.warning("AI 端连接不可用", account=account_id, error=self._connect_error)
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
        # 093：已申请、还没通过的人隔一阵复查一次（`interval<=0` 时 run() 自己就返回了）。
        if self._friend_checker is not None:
            side_tasks.append(
                asyncio.create_task(self._friend_checker.run(), name="gw-friend-check")
            )
        # 本机朋友圈巡看：默认关（间隔 0），开了才起循环（见 `MomentsWatcher.enabled`）。
        if self._moments is not None:
            side_tasks.append(asyncio.create_task(self._moments.run(), name="gw-moments"))
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
            _wechat.warning("能力探测失败", error=str(exc))
            return
        try:
            client = await self._pool.acquire(account_id)
        except Exception as exc:  # noqa: BLE001
            _ai.warning("能力上报失败", error=str(exc))
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
            _ai.warning("能力上报失败", error=str(exc))
            return
        self._reported_capabilities[account_id] = (client, frame["capabilities"])
        unavailable = [
            cap["label"] for cap in frame["capabilities"].values() if not cap["available"]
        ]
        _ai.info("已上报本机能力", 做不了=unavailable or "无")

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
            _ai.warning("退出时还有未收口的轮次，已放弃", turns=abandoned)
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
            _backend.warning("退出时消息事件没清完，留到下次开机再报",
                         pending=self._message_events.pending())
        except Exception as exc:  # noqa: BLE001 —— 退出路径上不许再抛
            _backend.warning("退出时清消息事件出错", error=type(exc).__name__)
