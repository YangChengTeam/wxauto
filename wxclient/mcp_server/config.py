from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


REAL_ACTION_CONFIRMATION = "EXECUTE_REAL_ACTION"


class McpServerSettings(BaseSettings):
    """Settings owned by the MCP transport, separate from WeChat settings."""

    model_config = SettingsConfigDict(env_prefix="WXCLIENT_MCP_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    public_url: str | None = None
    auth_token: SecretStr = SecretStr("")
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    )
    allowed_origins: list[str] = Field(default_factory=list)
    allow_real_actions: bool = False
    confirmation_phrase: str = REAL_ACTION_CONFIRMATION
    task_timeout_seconds: float = 180.0
    upload_max_bytes: int = 20 * 1024 * 1024
    upload_dir: Path | None = None

    @property
    def endpoint(self) -> str:
        base = self.public_url or f"http://{self.host}:{self.port}"
        return f"{base.rstrip('/')}/{self.path.lstrip('/')}"

    @property
    def is_loopback(self) -> bool:
        return self.host.strip().lower() in {"127.0.0.1", "localhost", "::1"}

    def validate_for_start(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("MCP port must be between 1 and 65535")
        if not self.path.startswith("/"):
            raise ValueError("MCP path must start with '/'")
        if self.task_timeout_seconds <= 0:
            raise ValueError("MCP task timeout must be positive")
        if self.upload_max_bytes <= 0:
            raise ValueError("MCP upload limit must be positive")
        if not self.is_loopback and not self.auth_token.get_secret_value():
            raise ValueError(
                "WXCLIENT_MCP_AUTH_TOKEN is required when MCP listens on a non-loopback host"
            )
        if not self.allowed_hosts:
            raise ValueError("At least one MCP allowed host is required")
