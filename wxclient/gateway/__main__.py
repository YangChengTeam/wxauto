"""网关入口：``python -m wxclient.gateway``。

默认 **dry-run**——整条回路都跑，但不真的往微信发消息。要真发必须显式加
``--allow-real-actions``（真实动作三重确认，见 ``CLAUDE.md`` §5.1）。

装配在 ``factory.build_gateway()``，与 UI 共用一份；本文件只负责解析参数和生命周期。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from wxclient.logging_config import configure_logging

from .factory import build_gateway

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


async def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()

    try:
        gateway = build_gateway(
            accounts=args.account,
            allow_real_actions=args.allow_real_actions,
            config_path=args.config,
            gateway_db=args.gateway_db,
        )
    except ValueError as exc:
        _log.error("网关无法启动", error=str(exc))
        return 2

    gateway.runtime.start()
    _log.info(
        "网关启动",
        accounts=len(gateway.accounts),
        real_actions=gateway.allow_real_actions,
        ai_ws_url=gateway.settings.ai_ws_url,
    )
    try:
        await gateway.runner.run()
    except KeyboardInterrupt:
        _log.info("收到中断，正在收尾")
    finally:
        await gateway.runner.stop()
        gateway.runtime.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(argv))


if __name__ == "__main__":
    sys.exit(main())
