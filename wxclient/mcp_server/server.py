from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from .auth import StaticTokenVerifier
from .config import McpServerSettings
from .providers import McpToolProvider, WeChatToolProvider
from .runtime import WxMcpRuntime


def create_mcp_server(
    config_path: str | Path | None = None,
    *,
    settings: McpServerSettings | None = None,
    runtime: WxMcpRuntime | None = None,
    providers: Iterable[McpToolProvider] | None = None,
) -> FastMCP:
    """Build the MCP server; extra providers can register independent tool groups."""
    server_settings = settings or McpServerSettings()
    server_settings.validate_for_start()
    active_runtime = runtime or WxMcpRuntime.from_config(config_path, server_settings)

    token = server_settings.auth_token.get_secret_value()
    token_verifier = StaticTokenVerifier(token) if token else None
    auth = None
    if token:
        resource_url = server_settings.public_url or f"http://{server_settings.host}:{server_settings.port}"
        auth = AuthSettings(
            issuer_url=AnyHttpUrl(resource_url),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=["wxclient:control"],
        )

    mcp = FastMCP(
        "wxauto-mcp",
        instructions=(
            "通过串行任务队列操作一个已登录的 Windows 微信会话。"
            "真实界面或写入操作需要 dry_run=false 且 confirmation=EXECUTE_REAL_ACTION。"
        ),
        log_level="WARNING",
        host=server_settings.host,
        port=server_settings.port,
        streamable_http_path=server_settings.path,
        stateless_http=True,
        json_response=True,
        token_verifier=token_verifier,
        auth=auth,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=server_settings.allowed_hosts,
            allowed_origins=server_settings.allowed_origins,
        ),
    )
    for provider in providers or (WeChatToolProvider(),):
        provider.register(mcp, active_runtime)
    return mcp
