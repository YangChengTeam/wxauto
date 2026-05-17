from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WXCLIENT_", extra="ignore")

    data_dir: Path = Field(default=Path(".wxclient"))
    database_path: Path | None = None
    default_account_id: str = "default"
    wechat_path: Path | None = None
    dry_run: bool = True
    wechat_driver_order: list[str] = Field(default_factory=lambda: ["uia"])
    omni_visual_rpa_config: dict[str, Any] = Field(default_factory=dict)
    friend_check_interval_seconds: int = 60
    message_scan_interval_seconds: int = 15
    agent_provider: str = "openai_agents"
    llm_base_url: str = "http://172.16.6.23:3000"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "glm-5.1"
    agent_system_prompt: str = (
        "You are a WeChat assistant. Reply in Chinese unless the user asks otherwise. "
        "Keep the response concise, natural, and directly useful."
    )
    agent_timeout_seconds: float = 30.0
    agent_context_message_count: int = 12

    @property
    def resolved_database_path(self) -> Path:
        return self.database_path or self.data_dir / "wxclient.db"


def load_settings(path: str | Path | None = None) -> Settings:
    raw: dict[str, Any] = {}
    if path:
        config_path = Path(path)
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return Settings(**raw)
