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
from wxclient.runtime import REAL_ACTION_CONFIRMATION, RuntimeSettings, WeChatRuntime

from .config import GatewaySettings
from .runner import GatewayRunner
from .sessions import GatewayStore
from .ticket import BusinessTicketProvider, LocalFileTicketProvider, TicketProvider
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

    runner = GatewayRunner(
        settings=gateway_settings,
        ticket_provider=build_ticket_provider(gateway_settings),
        store=store,
        source=QueueMessageSource(runtime=runtime, store=store),
        sink=QueueMessageSink(
            runtime=runtime,
            allow_real_actions=allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
        ),
        accounts=list(resolved_accounts),
    )
    return Gateway(
        runtime=runtime,
        runner=runner,
        settings=gateway_settings,
        accounts=resolved_accounts,
        allow_real_actions=allow_real_actions,
    )
