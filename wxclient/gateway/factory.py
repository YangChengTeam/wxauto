"""网关装配：CLI 入口和 UI 共用这一份接线。

``AGENTS.md`` 的架构约束要求「新增入口必须复用同一个 ``WeChatRuntime``，不得各自复制
一份队列与封装」。装配代码同理——它决定了票据来源、账号、真实动作开关这些安全相关的
选择，复制一份就等于多一处可以悄悄放宽的地方。所以入口只负责解析参数，装配全走这里。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from wxclient.config import load_settings
from wxclient.heavy_import import HEAVY_IMPORT_LOCK
from wxclient.runtime import REAL_ACTION_CONFIRMATION, RuntimeSettings, WeChatRuntime

from .agent_status import AgentStatusReporter
from .candidates import CandidateDirectory
from .capabilities import Capability, probe_capabilities
from .commands import COMMANDS, CommandExecutor
from .config import GatewaySettings
from .contacts import ContactDirectory
from .download import FileFetcher
from .friends import FriendWatcher
from .guard import OutboundGuard, action_limits_from
from .message_events import MessageEventReporter
from .pacing import MIN_FRIEND_INTERVAL_SECONDS, clamp_interval
from .runner import GatewayRunner
from .sessions import GatewayStore
from .strangers import StrangerWatcher
from .ticket import ClientTokenTicketProvider, LocalFileTicketProvider, TicketProvider
from .watchlist import Watchlist, WatchScope
from .wechat_io import QueueMessageSink, QueueMessageSource

_log = structlog.get_logger(__name__)


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
        _log.warning("使用本地票据文件（仅供联调，生产禁用）")
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
    login_context_mode: Callable[[], tuple[str, int] | None] | None = None,
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
    friend_interval = gateway_settings.friend_check_interval_seconds
    if friend_interval > 0:
        friend_interval = clamp_interval(
            friend_interval, MIN_FRIEND_INTERVAL_SECONDS, what="加好友检查间隔"
        )

    ticket_provider = build_ticket_provider(gateway_settings, client_token)

    file_fetcher = FileFetcher()

    #: 074：能力表按**当下**算，不是启动时拍一张快照——微信随时可能被关掉、掉线、
    #: 缩回托盘。探测本身要跟重型 import 串行（Windows 加载器锁，见 heavy_import.py），
    #: 探不出来就当「还没探到微信」处理，宁可说做不了也不要假装能做。
    def current_capabilities() -> dict[str, Capability]:
        try:
            with HEAVY_IMPORT_LOCK:
                from wxclient.uia_probe import probe_wechat_uia
            uia = probe_wechat_uia()
        except Exception as exc:  # noqa: BLE001 —— 探测失败 = 不知道 = 按做不了报
            _log.warning("能力探测时 UIA 探测失败", error=str(exc))
            uia = None
        return probe_capabilities(
            uia=uia,
            allow_real_actions=allow_real_actions,
            has_file_channel=file_fetcher is not None,
            supported_ops=set(COMMANDS),
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

    # 089：分身级接管状态上报。和换票、会话名共用同一个 Client Token——同一个员工身份。
    # ★ 它**不影响本机那道闸**：报不上去只是「外面还不知道」，接管在这台电脑上早生效了。
    agent_status = AgentStatusReporter(
        gateway_settings.business_api_url,
        client_token or (lambda: ""),
    )

    runner = GatewayRunner(
        settings=gateway_settings,
        ticket_provider=ticket_provider,
        store=store,
        source=QueueMessageSource(runtime=runtime, store=store, watchlist=watchlist),
        sink=QueueMessageSink(
            runtime=runtime,
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
        ),
        accounts=list(resolved_accounts),
        watchlist=watchlist,
        capabilities=current_capabilities,
        agent_status=agent_status,
        login_context_mode=login_context_mode,
        contacts=contacts,
        candidates=candidates,
        message_events=message_events,
        command_executor=CommandExecutor(
            runtime=runtime,
            store=store,
            guard=OutboundGuard(
                store,
                max_sends_per_hour=gateway_settings.max_sends_per_hour,
                active_hours=gateway_settings.active_hours,
                action_limits=action_limits_from(gateway_settings),
            ),
            account_id=resolved_accounts[0],
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
            fetcher=file_fetcher,
            capabilities=current_capabilities,
            candidates=candidates,
            message_events=message_events,
        ),
        friend_watcher=FriendWatcher(
            accounts=resolved_accounts,
            runtime=runtime,
            interval_seconds=friend_interval,
            active_hours=gateway_settings.active_hours,
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
        ),
        # 090：陌生人主动发起。**默认关**（`stranger_intake`）——放开 fail-closed 名单
        # 是专员的一次知情选择，不是缺省行为。
        stranger_watcher=StrangerWatcher(
            accounts=resolved_accounts,
            runtime=runtime,
            store=store,
            interval_seconds=gateway_settings.stranger_scan_interval_seconds,
            enabled=gateway_settings.stranger_intake,
            active_hours=gateway_settings.active_hours,
            allow_real_actions=allow_real_actions,
        ),
    )
    return Gateway(
        runtime=runtime,
        runner=runner,
        settings=gateway_settings,
        accounts=resolved_accounts,
        allow_real_actions=allow_real_actions,
    )
