"""网关装配：CLI 入口和 UI 共用这一份接线。

``AGENTS.md`` 的架构约束要求「新增入口必须复用同一个 ``WeChatRuntime``，不得各自复制
一份队列与封装」。装配代码同理——它决定了票据来源、账号、真实动作开关这些安全相关的
选择，复制一份就等于多一处可以悄悄放宽的地方。所以入口只负责解析参数，装配全走这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from wxclient.config import load_settings
from wxclient.heavy_import import HEAVY_IMPORT_LOCK
from wxclient.runtime import REAL_ACTION_CONFIRMATION, RuntimeSettings, WeChatRuntime

from .capabilities import Capability, probe_capabilities
from .commands import COMMANDS, CommandExecutor
from .config import GatewaySettings
from .download import FileFetcher
from .friends import FriendWatcher
from .guard import OutboundGuard, action_limits_from
from .pacing import MIN_FRIEND_INTERVAL_SECONDS, clamp_interval
from .runner import GatewayRunner
from .sessions import GatewayStore
from .ticket import BusinessTicketProvider, LocalFileTicketProvider, TicketProvider
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


def build_ticket_provider(settings: GatewaySettings) -> TicketProvider:
    if settings.local_ticket_file is not None:
        _log.warning("使用本地票据文件（仅供联调，生产禁用）")
        return LocalFileTicketProvider(settings.local_ticket_file)
    return BusinessTicketProvider(
        settings.business_api_url,
        settings.internal_key.get_secret_value(),
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

    ticket_provider = build_ticket_provider(gateway_settings)

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
        ),
        friend_watcher=FriendWatcher(
            accounts=resolved_accounts,
            runtime=runtime,
            interval_seconds=friend_interval,
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
