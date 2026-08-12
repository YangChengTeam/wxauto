"""UI 的启动参数。全部走环境变量，凭证一个都不在这（票据仍由网关自己换）。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class UiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WXCLIENT_UI_", extra="ignore")

    accounts: str = ""
    """要接管的微信号，多个用逗号分隔。"""

    allow_real_actions: bool = False
    """真的往微信发消息。默认关——UI 上也能看到当前是不是 dry-run。"""

    autostart: bool = True
    """开窗即启动服务。"""

    start_hidden: bool = True
    """启动后直接缩到右下角托盘，不弹窗。"""

    log_lines: int = 2000
    """日志窗口最多保留多少行。"""
