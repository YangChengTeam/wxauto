from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wxclient.config import Settings, load_settings


DEFAULT_GUI_CONFIG_PATH = Path(".wxclient") / "settings.json"


def resolve_config_path(path: Path | None = None) -> Path:
    return path or DEFAULT_GUI_CONFIG_PATH


def load_gui_settings(path: Path | None = None) -> tuple[Settings, Path]:
    config_path = resolve_config_path(path)
    if path is None and not config_path.exists():
        return Settings(), config_path
    return load_settings(config_path), config_path


def save_gui_settings(path: Path, settings: Settings, api_key: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "data_dir": str(settings.data_dir),
        "database_path": str(settings.database_path) if settings.database_path else None,
        "host": settings.host,
        "port": settings.port,
        "webhook_token": settings.webhook_token,
        "default_account_id": settings.default_account_id,
        "wechat_path": str(settings.wechat_path) if settings.wechat_path else None,
        "dry_run": settings.dry_run,
        "friend_check_interval_seconds": settings.friend_check_interval_seconds,
        "message_scan_interval_seconds": settings.message_scan_interval_seconds,
        "agent_provider": settings.agent_provider,
        "llm_base_url": settings.llm_base_url,
        "llm_model": settings.llm_model,
        "agent_system_prompt": settings.agent_system_prompt,
        "agent_timeout_seconds": settings.agent_timeout_seconds,
        "agent_context_message_count": settings.agent_context_message_count,
    }
    if api_key:
        data["llm_api_key"] = api_key
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
