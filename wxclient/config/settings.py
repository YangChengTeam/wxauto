from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field
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
