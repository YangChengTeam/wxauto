from __future__ import annotations

import argparse
from pathlib import Path

from wxclient.logging_config import configure_logging

from .config import McpServerSettings
from .runtime import WxMcpRuntime
from .server import create_mcp_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows WeChat MCP server")
    parser.add_argument("--config", type=Path, default=None, help="wxclient JSON config")
    parser.add_argument("--host", default=None, help="listen host; use 0.0.0.0 for remote agents")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--path", default=None, help="Streamable HTTP path, default /mcp")
    parser.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        help="allowed HTTP Host header, e.g. 192.168.1.20:* (repeatable)",
    )
    parser.add_argument(
        "--allow-real-actions",
        action="store_true",
        help="allow tools to execute real UI/write actions after per-call confirmation",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    overrides = {
        key: value
        for key, value in {
            "host": args.host,
            "port": args.port,
            "path": args.path,
            "allowed_hosts": args.allowed_hosts,
        }.items()
        if value is not None
    }
    if args.allow_real_actions:
        overrides["allow_real_actions"] = True
    settings = McpServerSettings(**overrides)
    configure_logging(level="INFO")
    runtime = WxMcpRuntime.from_config(args.config, settings)
    mcp = create_mcp_server(args.config, settings=settings, runtime=runtime)
    runtime.start()
    try:
        mcp.run(transport="streamable-http")
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
