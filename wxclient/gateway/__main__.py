"""网关入口：``python -m wxclient.gateway``。

默认 **dry-run**——整条回路都跑，但不真的往微信发消息。要真发必须显式加
``--allow-real-actions``，与 MCP 模式同一条纪律。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

from wxclient.config import load_settings
from wxclient.logging_config import configure_logging
from wxclient.mcp_server.config import REAL_ACTION_CONFIRMATION, McpServerSettings
from wxclient.mcp_server.runtime import WxMcpRuntime

from .config import GatewaySettings
from .runner import GatewayRunner
from .sessions import GatewayStore
from .ticket import BusinessTicketProvider, LocalFileTicketProvider, TicketProvider
from .wechat_io import QueueMessageSink, QueueMessageSource

_log = structlog.get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wxclient-gateway", description="微信自动化招聘网关")
    parser.add_argument("--config", help="微信运行参数配置文件")
    parser.add_argument(
        "--account",
        action="append",
        default=[],
        metavar="WECHAT_ID",
        help="要接管的微信号（可重复）。每个号一条 AI 端连接。",
    )
    parser.add_argument("--allow-real-actions", action="store_true", help="真的往微信发消息")
    parser.add_argument("--gateway-db", help="网关状态库路径，默认 <data_dir>/gateway.db")
    return parser


def build_ticket_provider(settings: GatewaySettings) -> TicketProvider:
    if settings.local_ticket_file is not None:
        _log.warning("使用本地票据文件（仅供联调，生产禁用）")
        return LocalFileTicketProvider(settings.local_ticket_file)
    return BusinessTicketProvider(
        settings.business_api_url,
        settings.internal_key.get_secret_value(),
        timeout_seconds=settings.ticket_timeout_seconds,
    )


async def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()

    accounts = [a.strip() for a in args.account if a.strip()]
    if not accounts:
        _log.error("至少要用 --account 指定一个微信号")
        return 2

    gateway_settings = GatewaySettings()
    try:
        gateway_settings.validate_for_start()
    except ValueError as exc:
        _log.error("网关配置不合法", error=str(exc))
        return 2

    wechat_settings = load_settings(args.config)
    mcp_settings = McpServerSettings(allow_real_actions=args.allow_real_actions)
    runtime = WxMcpRuntime(wechat_settings, mcp_settings)

    database_path = Path(args.gateway_db) if args.gateway_db else wechat_settings.data_dir / "gateway.db"
    store = GatewayStore(database_path)

    runner = GatewayRunner(
        settings=gateway_settings,
        ticket_provider=build_ticket_provider(gateway_settings),
        store=store,
        source=QueueMessageSource(runtime=runtime, store=store),
        sink=QueueMessageSink(
            runtime=runtime,
            allow_real_actions=args.allow_real_actions,
            confirmation=REAL_ACTION_CONFIRMATION,
        ),
        accounts=accounts,
    )

    runtime.start()
    _log.info(
        "网关启动",
        accounts=len(accounts),
        real_actions=args.allow_real_actions,
        ai_ws_url=gateway_settings.ai_ws_url,
    )
    try:
        await runner.run()
    except KeyboardInterrupt:
        _log.info("收到中断，正在收尾")
    finally:
        await runner.stop()
        runtime.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
