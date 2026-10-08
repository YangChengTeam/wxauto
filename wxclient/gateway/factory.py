"""网关装配：CLI 入口和 UI 共用这一份接线。

``AGENTS.md`` 的架构约束要求「新增入口必须复用同一个 ``WeChatRuntime``，不得各自复制
一份队列与封装」。装配代码同理——它决定了票据来源、账号、真实动作开关这些安全相关的
选择，复制一份就等于多一处可以悄悄放宽的地方。所以入口只负责解析参数，装配全走这里。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from wxclient.config import load_settings
from wxclient.heavy_import import HEAVY_IMPORT_LOCK
from wxclient.runtime import REAL_ACTION_CONFIRMATION, RuntimeSettings, WeChatRuntime

from .. import logrole, tuning
from .agent_files import AgentFileUploader, http_base_from_ws
from .attachments import AttachmentFetcher
from .autofriend import AutoFriendWatcher
from .candidate_mode import CandidateModeClient
from .candidates import CandidateDirectory
from .capabilities import Capability, probe_capabilities
from .commands import COMMANDS, CommandExecutor
from .config import GatewaySettings
from .contacts import ContactDirectory
from .download import FileFetcher
from .friendcheck import PendingFriendWatcher
from .friends import FriendWatcher
from .guard import (
    OutboundGuard,
    action_limits_from,
    read_intervals_from,
    write_intervals_from,
)
from .message_events import MessageEventReporter
from .moments import MomentsWatcher
from .pacing import clamp_interval, set_jitter_ratio
from .runner import GatewayRunner
from .sessions import GatewayStore
from .ticket import ClientTokenTicketProvider, LocalFileTicketProvider, TicketProvider
from .watchlist import Watchlist, WatchScope
from .wechat_io import QueueMessageSink, QueueMessageSource

_log = structlog.get_logger(__name__)

#: 换票走业务后端。
_backend = _log.bind(**{logrole.ROLE_KEY: logrole.BACKEND})
#: 能力探测直接问微信客户端。
_wechat = _log.bind(**{logrole.ROLE_KEY: logrole.WECHAT})


@dataclass(frozen=True)
class Gateway:
    """装配好的一套网关。``runtime`` 要先 ``start()``，``runner`` 交给 asyncio 跑。"""

    runtime: WeChatRuntime
    runner: GatewayRunner
    settings: GatewaySettings
    accounts: tuple[str, ...]
    allow_real_actions: bool


def build_ticket_provider(
    settings: GatewaySettings, client_token: Callable[[], str] | None = None
) -> TicketProvider:
    if settings.local_ticket_file is not None:
        _backend.warning("使用本地票据文件（仅供联调，生产禁用）")
        return LocalFileTicketProvider(settings.local_ticket_file)
    return ClientTokenTicketProvider(
        settings.business_api_url,
        client_token or (lambda: ""),
        timeout_seconds=settings.ticket_timeout_seconds,
    )


def normalize_accounts(accounts: object) -> tuple[str, ...]:
    """把 ``--account`` 或环境变量里的账号列表洗成干净元组，顺序去重。"""
    if isinstance(accounts, str):
        raw = accounts.replace("；", ";").replace("，", ",").replace(";", ",").split(",")
    else:
        raw = list(accounts or [])
    seen: dict[str, None] = {}
    for item in raw:
        name = str(item).strip()
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def build_gateway(
    *,
    accounts: object,
    allow_real_actions: bool = False,
    config_path: str | Path | None = None,
    gateway_db: str | Path | None = None,
    client_token: Callable[[], str] | None = None,
) -> Gateway:
    """装配一套可运行的网关。配置不合法直接抛 ``ValueError``，绝不带着半套配置启动。"""
    resolved_accounts = normalize_accounts(accounts)
    if not resolved_accounts:
        raise ValueError("至少要指定一个微信号")

    gateway_settings = GatewaySettings()
    gateway_settings.validate_for_start()

    wechat_settings = load_settings(config_path)
    runtime_settings = RuntimeSettings(allow_real_actions=allow_real_actions)
    runtime_settings.validate_for_start()
    runtime = WeChatRuntime(wechat_settings, runtime_settings)

    database_path = Path(gateway_db) if gateway_db else wechat_settings.data_dir / "gateway.db"
    store = GatewayStore(database_path)

    # 监听范围的权威在 AI 端（连上后由 WS 推下来覆盖）；这里装的是推之前的本地兜底。
    # ★ 名单落盘再读回来（069）：不这么做的话，进程一重启就要等 AI 端重推才恢复，
    #   中间那段网关是全哑的——名单为空意味着一个会话都不碰。
    watchlist = Watchlist.from_local(
        only=normalize_accounts(gateway_settings.peer_allowlist),
        never=normalize_accounts(gateway_settings.peer_denylist),
        persist=lambda scope: store.put_watchlist(
            {"only": sorted(scope.only), "never": sorted(scope.never)}
        ),
    )
    if (saved := store.get_watchlist()) is not None:
        watchlist.apply_scope(
            WatchScope(
                only=frozenset(saved.get("only") or ()),
                never=frozenset(saved.get("never") or ()),
                received=True,
                origin="上次 AI 端推送（本地留存）",
            )
        )
    # 演示模式关抖动、换地板（`GatewaySettings._apply_demo_mode`）；平时这两样就是 pacing 的常量。
    set_jitter_ratio(gateway_settings.jitter_ratio)
    # 写死在各模块里的时间项（`wxclient.tuning`）：极速开着换成填的值，关着恢复原值。
    tuned = tuning.apply()
    if tuned:
        _log.warning("极速模式：写死的时间项已按配置替换", **tuned)
    friend_interval = gateway_settings.friend_check_interval_seconds
    if friend_interval > 0:
        friend_interval = clamp_interval(
            friend_interval, gateway_settings.friend_floor_seconds, what="加好友检查间隔"
        )

    ticket_provider = build_ticket_provider(gateway_settings, client_token)

    file_fetcher = FileFetcher()

    #: 074：能力表按**当下**算，不是启动时拍一张快照——微信随时可能被关掉、掉线、
    #: 缩回托盘。探测本身要跟重型 import 串行（Windows 加载器锁，见 heavy_import.py），
    #: 探不出来就当「还没探到微信」处理，宁可说做不了也不要假装能做。
    #: 138：本机登录的是哪个微信（`accountRef`，和状态上报给业务端的同一个值）。按进程打开的账号目录算，
    #: 不碰界面；但要枚举进程打开的文件，保活每轮、每条开场指令都问一遍不划算，缓存一分钟。
    #: 认不出 = 空串（不猜）：开场协调随之报做不了，回到原来的流程。
    account_cache: dict[str, Any] = {"ref": "", "at": 0.0}

    def current_account_ref() -> str:
        now = time.monotonic()
        if account_cache["at"] and now - account_cache["at"] < 60:
            return account_cache["ref"]
        try:
            with HEAVY_IMPORT_LOCK:
                from wxclient.uia_probe.account import probe_account
            account = probe_account()
        except Exception as exc:  # noqa: BLE001 —— 认不出就当认不出，不影响别的
            _wechat.warning("认微信账号失败", error=str(exc))
            account = None
        account_cache.update(ref=account.account_ref if account else "", at=now)
        return account_cache["ref"]

    def current_capabilities() -> dict[str, Capability]:
        try:
            with HEAVY_IMPORT_LOCK:
                from wxclient.uia_probe import probe_wechat_uia
            uia = probe_wechat_uia()
        except Exception as exc:  # noqa: BLE001 —— 探测失败 = 不知道 = 按做不了报
            _wechat.warning("能力探测时 UIA 探测失败", error=str(exc))
            uia = None
        return probe_capabilities(
            uia=uia,
            allow_real_actions=allow_real_actions,
            has_file_channel=file_fetcher is not None,
            supported_ops=set(COMMANDS),
            account_ref=current_account_ref(),
        )

    # 候选人会话名两条路（api2 §4）。和换票共用同一个 Client Token 取值函数——
    # 它们本来就是同一个员工身份，没登录时两边都自动躺平（不报错、不刷日志）。
    # ★ 它同时是**监听名单**的来源（spec 085，用户 2026-08-20：就用 /client/contacts，
    # 不新增接口）——名单在 `GatewayRunner._apply_derived_watchlist()` 里从本机那张表派生。
    contacts = ContactDirectory(
        gateway_settings.business_api_url,
        client_token or (lambda: ""),
    )
    # 候选人回写（api2 §9）。同一个 Client Token、同一条「没登录就躺平」的规矩。
    candidates = CandidateDirectory(
        gateway_settings.business_api_url,
        client_token or (lambda: ""),
    )
    # 微信消息事件上报（`docs/招聘分身_客户端微信消息事件上报_v1.md`）：我方发出去了、
    # 候选人回话了，各报一条，业务后端据此排/取消「未回复跟进」。
    #
    # ★ **只走流程不真动微信的机器一条都不报**，所以这里在 `allow_real_actions` 为假时
    #   干脆不建这个上报器（而不是建了再在每个调用点各判一次 dry-run——那种判断迟早
    #   会漏掉一处）。dry-run 的一轮里微信侧什么都没发生，把它报成「已发送」就是拿假动作
    #   去改真数据：后端会从一条根本不存在的消息开始算「多久没回」。
    message_events = (
        MessageEventReporter(
            gateway_settings.business_api_url,
            client_token or (lambda: ""),
            store=store,
        )
        if allow_real_actions
        else None
    )

    # 091：候选人级接管同步。和换票、会话名共用同一个 Client Token——同一个员工身份。
    # ★ 它**不影响本机那道闸**：同步不上去只是「外面还不知道」，接管在这台电脑上早生效了。
    candidate_mode = CandidateModeClient(
        gateway_settings.business_api_url,
        client_token or (lambda: ""),
    )

    # 一把闸两处用：AI 端下发的指令和本机朋友圈巡看的点赞，同一本账（计数在 store 里，
    # 实例本身无状态；给同一个实例只是让「同一把闸」这件事看得见）。
    guard = OutboundGuard(
        store,
        max_sends_per_hour=gateway_settings.max_sends_per_hour,
        active_hours=gateway_settings.active_hours,
        action_limits=action_limits_from(gateway_settings),
        write_intervals=write_intervals_from(gateway_settings),
        read_intervals=read_intervals_from(gateway_settings),
    )
    command_executor = CommandExecutor(
        runtime=runtime,
        store=store,
        guard=guard,
        account_id=resolved_accounts[0],
        allow_real_actions=allow_real_actions,
        confirmation=REAL_ACTION_CONFIRMATION,
        fetcher=file_fetcher,
        capabilities=current_capabilities,
        candidates=candidates,
        message_events=message_events,
        account_ref=current_account_ref,
    )

    runner = GatewayRunner(
        settings=gateway_settings,
        ticket_provider=ticket_provider,
        store=store,
        source=QueueMessageSource(
            runtime=runtime,
            store=store,
            watchlist=watchlist,
            hot_window_seconds=gateway_settings.hot_window_seconds,
            prefilter_by_session_list=gateway_settings.prefilter_by_session_list,
            read_unread_strangers=gateway_settings.read_unread_strangers,
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
            # 104：对方发来的图片/文件另存到本机随轮送 AI 端。目录跟 gateway.db 放一起，
            # 同样在 `.wxclient/` 下、同样禁止入库。
            attachment_fetcher=(
                AttachmentFetcher(runtime=runtime, root=database_path.parent / "attachments")
                if gateway_settings.inbound_attachments
                else None
            ),
        ),
        sink=QueueMessageSink(
            runtime=runtime,
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
        ),
        accounts=list(resolved_accounts),
        watchlist=watchlist,
        capabilities=current_capabilities,
        account_ref=current_account_ref,
        candidate_mode=candidate_mode,
        contacts=contacts,
        candidates=candidates,
        message_events=message_events,
        command_executor=command_executor,
        # 2026-09-28：另存下来的附件直接传到 AI 端这个会话拿 file_id（docs/21 §2.0），不再塞进 WS。
        # 地址从 WS 门推出来（同一反代前缀）；票和 WS 建连一样每次现换。传不上去 runner 自己退回内联。
        file_uploader=(
            AgentFileUploader(
                base_url=http_base_from_ws(gateway_settings.ai_ws_url),
                ticket_provider=ticket_provider,
            )
            if gateway_settings.inbound_attachments
            else None
        ),
        # 092：名单里还不是好友的人自动补加。加好友本身仍走 `command_executor`——
        # 能力闸、频控闸、时间窗、幂等一道不绕。
        auto_friend=AutoFriendWatcher(
            store=store,
            runtime=runtime,
            commands=command_executor,
            enabled=gateway_settings.auto_add_friend,
            min_interval_seconds=gateway_settings.friend_floor_seconds,
        ),
        # 2026-09-12：别人发来的申请默认自动通过（`auto_accept_friend`），走 `command_executor`——
        # 能力闸、加好友配额、时间窗、幂等一道不绕；dry-run 机器只数不点。
        friend_watcher=FriendWatcher(
            accounts=resolved_accounts,
            runtime=runtime,
            interval_seconds=friend_interval,
            active_hours=gateway_settings.active_hours,
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
            auto_accept=gateway_settings.auto_accept_friend,
            commands=command_executor,
        ),
        # 093：已申请、还没通过的人，隔一阵复查一次是不是通过了。和 092 一正一反。
        friend_checker=PendingFriendWatcher(
            accounts=resolved_accounts,
            runtime=runtime,
            store=store,
            interval_seconds=gateway_settings.friend_recheck_interval_seconds,
            deadline_hours=gateway_settings.friend_recheck_deadline_hours,
            # 让路和占闸都按「加好友最小间隔」来，和加好友自己守的是同一个数。
            friend_interval_seconds=gateway_settings.friend_interval_seconds,
            active_hours=gateway_settings.active_hours,
        ),
        # 用户 2026-09-08：本机自己巡看朋友圈，只碰新动态。**默认关**（间隔 0）。
        moments_watcher=MomentsWatcher(
            accounts=resolved_accounts,
            runtime=runtime,
            store=store,
            guard=guard,
            interval_seconds=gateway_settings.moments_poll_interval_seconds,
            min_interval_seconds=gateway_settings.moments_floor_seconds,
            auto_like=gateway_settings.moments_auto_like,
            auto_comment=gateway_settings.moments_auto_comment,
            comment_material=gateway_settings.moments_comment_material,
            active_hours=gateway_settings.active_hours,
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
        ),
    )
    return Gateway(
        runtime=runtime,
        runner=runner,
        settings=gateway_settings,
        accounts=resolved_accounts,
        allow_real_actions=allow_real_actions,
    )
