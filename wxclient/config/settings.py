from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    """设置 / 状态库 / 日志放哪。

    源码跑：沿用 ``./.wxclient``（相对仓库目录），跟现有装置一致，别把人家的设置搬家。

    打包成 exe 之后不能再用相对路径：cwd 是双击时的任意目录——从快捷方式启动是一个，
    从开始菜单是另一个，从资源管理器双击又是一个。真机上就见过一次：exe 的 cwd 落在
    映射的网络盘 ``Z:\``，设置和日志跟着写去了网络盘。更糟的是装在 ``Program Files``
    下时那儿根本不可写。所以冻结态锚到每用户固定位置。
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "劳小智微信助手"
    return Path(".wxclient")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WXCLIENT_", extra="ignore")

    data_dir: Path = Field(default_factory=_default_data_dir)
    database_path: Path | None = None
    default_account_id: str = "default"
    wechat_path: Path | None = None
    dry_run: bool = True
    wechat_driver_order: list[str] = Field(default_factory=lambda: ["uia"])

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
