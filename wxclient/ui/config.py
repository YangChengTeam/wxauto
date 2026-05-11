from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from wxclient.config import Settings, load_settings


DEFAULT_GUI_CONFIG_PATH = Path(".wxclient") / "settings.json"

# Keys for form fields that should be persisted across sessions
_FORM_STATE_KEYS = (
    "friend_interval",
    "friend_limit",
    "friend_verify",
    "unread_interval",
    "unread_friend",
    "unread_reply_all",
    "watch_interval",
    "watch_duration",
    "watch_targets",
    "watch_auto_reply",
    "watch_reply_all",
    "add_number",
    "add_greetings",
    "add_remark",
    "show_automation_tasks",
)


def resolve_config_path(path: Path | None = None) -> Path:
    if path is not None:
        return path

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        frozen_parent_path = exe_dir.parent / ".wxclient" / "settings.json"
        frozen_local_path = exe_dir / ".wxclient" / "settings.json"
        for candidate in (frozen_parent_path, DEFAULT_GUI_CONFIG_PATH, frozen_local_path):
            if candidate.exists():
                return candidate
        return frozen_local_path

    return DEFAULT_GUI_CONFIG_PATH


def load_gui_settings(path: Path | None = None) -> tuple[Settings, Path]:
    config_path = resolve_config_path(path)
    if path is None and not config_path.exists():
        return Settings(), config_path
    return load_settings(config_path), config_path


def load_form_state(path: Path) -> dict[str, Any]:
    """Load persisted form field values from the settings file."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("gui_form_state", {})
    except Exception:
        return {}


def save_gui_settings(
    path: Path,
    settings: Settings,
    api_key: str | None = None,
    form_state: dict[str, Any] | None = None,
) -> None:
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
    if form_state:
        data["gui_form_state"] = form_state
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
