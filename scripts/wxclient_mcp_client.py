from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Small Streamable HTTP MCP test client")
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--tool", default=None, help="tool to call; omit to list tools")
    parser.add_argument("--arguments", default="{}", help="tool arguments as JSON")
    parser.add_argument(
        "--upload-file",
        type=Path,
        default=None,
        help="read this local file and populate wx_upload_media arguments",
    )
    return parser


async def run(url: str, tool: str | None, arguments: dict[str, Any]) -> None:
    token = os.getenv("WXCLIENT_MCP_AUTH_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    # Do not route a local MCP endpoint through system HTTP proxy variables.
    async with httpx.AsyncClient(
        headers=headers,
        trust_env=False,
        timeout=httpx.Timeout(300.0),
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                if not tool:
                    result = await session.list_tools()
                    print(json.dumps([item.name for item in result.tools], ensure_ascii=False, indent=2))
                    return
                result = await session.call_tool(tool, arguments)
                print(
                    json.dumps(
                        {
                            "is_error": result.isError,
                            "structured_content": result.structuredContent,
                            "content": [
                                getattr(item, "text", str(item)) for item in result.content
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    arguments = json.loads(args.arguments)
    if not isinstance(arguments, dict):
        raise ValueError("--arguments must decode to a JSON object")
    if args.upload_file is not None:
        if args.tool != "wx_upload_media":
            raise ValueError("--upload-file requires --tool wx_upload_media")
        arguments.update(
            {
                "filename": args.upload_file.name,
                "content_base64": base64.b64encode(args.upload_file.read_bytes()).decode("ascii"),
            }
        )
    asyncio.run(run(args.url, args.tool, arguments))


if __name__ == "__main__":
    main()
