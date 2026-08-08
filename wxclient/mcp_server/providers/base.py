from __future__ import annotations

from typing import Protocol

from mcp.server.fastmcp import FastMCP

from ..runtime import WxMcpRuntime


class McpToolProvider(Protocol):
    """Extension point for independently registering MCP tool groups."""

    def register(self, mcp: FastMCP, runtime: WxMcpRuntime) -> None: ...
