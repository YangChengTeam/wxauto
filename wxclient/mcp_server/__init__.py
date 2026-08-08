"""Windows WeChat MCP server."""

from typing import Any

from .config import McpServerSettings
from .runtime import WxMcpRuntime


def create_mcp_server(*args: Any, **kwargs: Any) -> Any:
    """Import the optional MCP SDK only when the server is actually created."""
    try:
        from .server import create_mcp_server as factory
    except ImportError as exc:
        if exc.name and (exc.name == "mcp" or exc.name.startswith("mcp.")):
            raise RuntimeError('Install the MCP extra with `pip install -e ".[mcp]"`') from exc
        raise
    return factory(*args, **kwargs)

__all__ = ["McpServerSettings", "WxMcpRuntime", "create_mcp_server"]
