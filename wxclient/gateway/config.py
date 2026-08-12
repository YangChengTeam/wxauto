"""网关配置。

秘密只从环境变量读，绝不写进仓库（``CLAUDE.md`` §5.1）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """网关运行参数，env 前缀 ``WXCLIENT_GATEWAY_``。"""

    model_config = SettingsConfigDict(env_prefix="WXCLIENT_GATEWAY_", extra="ignore")

    # ── 对端地址 ──
    ai_ws_url: str = ""
    """劳小智 AI 服务的 WS 门，如 ``wss://host/v1/ws``。"""

    business_api_url: str = ""
    """业务后端基址，用于换取员工短票。"""

    internal_key: SecretStr = SecretStr("")
    """网关对业务后端的服务间凭证——网关唯一持有的秘密。"""

    # ── 联调用的本地票据来源（生产必须留空）──
    local_ticket_file: Path | None = None

    # ── 对话 ──
    task_type: str = "wechat_reply"
    turn_timeout_seconds: float = 180.0
    """一轮对话的上限。AI 端整轮常在 20–30s，留足余量。"""

    ticket_timeout_seconds: float = 3.0
    """换票超时。超时即 fail-closed，不重试绕过。"""

    # ── 入站节奏（Phase B 使用，先在此登记避免二次改配置）──
    poll_interval_seconds: float = 5.0
    debounce_seconds: float = 3.0

    # ── 处理范围：哪些会话才轮到网关管 ──
    allow_groups: bool = False
    """是否处理群聊。**默认不处理**——群里替人自动应答的误伤面比单聊大得多，
    而且群消息的「对方」不是一个人。要开必须显式开。"""

    peer_allowlist: str = ""
    """只处理这些会话（微信里显示的名字，逗号分隔）。留空 = 不限制。
    谨慎上线时先只放两三个人进来。"""

    peer_denylist: str = ""
    """永远不处理这些会话（逗号分隔）。优先级高于白名单。"""

    # ── 守门（Phase B 使用）──
    max_sends_per_hour: int = 30
    active_hours: str = "09:00-21:00"

    allow_insecure_transport: bool = False
    """允许 ``http://`` / ``ws://`` 明文。默认拒绝——Bearer 凭证不得明文过网。"""

    def validate_for_start(self) -> None:
        if not self.ai_ws_url:
            raise ValueError("WXCLIENT_GATEWAY_AI_WS_URL 必填")
        if not self.ai_ws_url.startswith(("ws://", "wss://")):
            raise ValueError("AI_WS_URL 必须是 ws:// 或 wss://")
        if self.turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds 必须为正")
        if self.ticket_timeout_seconds <= 0:
            raise ValueError("ticket_timeout_seconds 必须为正")

        # 票据来源二选一，且必须有一个。
        has_business = bool(self.business_api_url)
        has_local = self.local_ticket_file is not None
        if has_business and has_local:
            raise ValueError("business_api_url 与 local_ticket_file 不能同时配置")
        if not has_business and not has_local:
            raise ValueError("必须配置 business_api_url 或 local_ticket_file 之一")
        if has_business and not self.internal_key.get_secret_value():
            raise ValueError("配置 business_api_url 时 WXCLIENT_GATEWAY_INTERNAL_KEY 必填")

        if not self.allow_insecure_transport:
            if self.ai_ws_url.startswith("ws://") and not _is_loopback_url(self.ai_ws_url):
                raise ValueError("非回环的 AI_WS_URL 必须用 wss://（凭证不得明文过网）")
            if self.business_api_url.startswith("http://") and not _is_loopback_url(
                self.business_api_url
            ):
                raise ValueError("非回环的 BUSINESS_API_URL 必须用 https://")


def _is_loopback_url(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host.strip("[]").lower() in {"127.0.0.1", "localhost", "::1"}
