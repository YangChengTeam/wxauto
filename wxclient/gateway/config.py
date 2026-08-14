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

    # ── 节拍：读消息与加好友各走各的（用户 2026-08-13 要求；防风控见 pacing.py）──
    poll_interval_seconds: float = 60.0
    """多久读一次未读消息。默认 1 分钟——原来的 5 秒是「一秒不差地翻聊天列表」，
    正是 2026-08-12 那次自动退登（判定触发风控）的可疑行为之一。实际间隔还会带随机抖动。"""

    friend_check_interval_seconds: float = 900.0
    """多久查一次新好友申请。加好友是风控最敏感的一类动作，节拍要比读消息慢一个量级。
    ``0`` = 完全不查。**查到也不会自动通过**（见 ``friends.py``）。"""

    debounce_seconds: float = 3.0

    # ── 处理范围：哪些会话才轮到网关管 ──
    #
    # 权威名单由 AI 端通过 WS 推下来（见 watchlist.py），界面上不再让人手抄一份。
    # 下面三个只是**环境变量应急开关**：出事要立刻掐掉某个会话时不用等 AI 端改数据。
    peer_allowlist: str = ""
    """只处理这些会话（微信里显示的名字，逗号分隔）。**群名和人名放同一份名单**。

    留空 = 等 AI 端推名单下来；在那之前一个会话都不碰（069 fail-closed）。
    原来「留空 = 不限制」意味着装上就自动回复所有人，包括专员的家人朋友。"""

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
