"""换票：用**这台机器登录时拿到的客户端 Token**，换取该招聘专员的短期 AI 会话票据。

四条铁律（``CLAUDE.md`` §4.1）：
1. 票不缓存、不落盘、不打日志；
2. 每次建连/重连都现换一张；
3. 换票失败一律 fail-closed——停掉该专员的自动化，不降级、不重试绕过；
4. 网关不持有任何员工的 LLM token，票里装什么它不看。

★ 2026-08-15 换了凭证：原来用的是一把手抄进这台机器的 `internal key`（服务间共享密钥，
所有装机的人都得抄一遍，撤销要改所有机器）。现在用客户端登录接口签发的、**按会话可撤销的**
Token（`docs/招聘分身_客户端认证与状态接口_v1.1.md` §5.3）：退出登录、换设备、管理员撤销
会话，这台机器立刻换不到票，也就立刻停手。

契约里两处容易踩的点：

* **请求体是空的**。换谁的票由服务端从会话派生（tenant/user/agent 全在 Token 背后），
  客户端**不许**上传身份——传了也不作数，还会让人误以为这台机器能替别人换票。
* **客户端必须先是 online**。显式报过 offline 或会话被撤销都换不到票（`CLIENT_NOT_ONLINE`）。
  所以状态上报（`ui/status_reporter.py`）不是装饰，它是这条链路的前置条件。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog

_log = structlog.get_logger(__name__)

TICKET_PATH = "/recruitment-agent/client/session-ticket"

#: 契约 §5.3 的错误码 → 说人话。**分得开的失败要给不同的话**：
#: 「额度用完」要去充值/等下月，「没权限」要找管理员，「不在线」自己会好——
#: 全都说成「换票失败」的话，排障只能靠猜。
MESSAGES = {
    "CLIENT_TOKEN_REQUIRED": "这台机器还没登录，换不到票",
    "CLIENT_TOKEN_INVALID": "客户端登录已失效，请重新登录",
    "CLIENT_SESSION_REVOKED": "客户端登录已失效，请重新登录",
    "EMPLOYEE_AI_MONTHLY_LIMIT_REACHED": "这位专员本月的 AI 额度已用完",
    "EMPLOYEE_AI_PERMISSION_DENIED": "这个账号没有 AI 使用权限，请联系管理员",
    "EMPLOYEE_AI_TOKEN_INACTIVE": "这个账号的 AI 通道已停用，请联系管理员",
    "CLIENT_NOT_ONLINE": "业务端记的这台机器不在线，等状态上报同步后会自己恢复",
    "EMPLOYEE_AI_PROVISIONING": "AI 通道正在开通，稍后重试",
    "AI_CREDENTIAL_CONFIGURATION_ERROR": "AI 凭证服务配置不完整，请联系管理员",
}


class TicketError(RuntimeError):
    """换票失败。调用方必须停止该专员的自动化动作，不得继续。"""

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class TicketProvider(Protocol):
    async def mint(self, wechat_account_id: str) -> str:
        """返回一张可立即使用的 session_ticket 密文。失败抛 TicketError。"""
        ...


class ClientTokenTicketProvider:
    """拿客户端 Token 向业务后端换票（生产实现）。

    ``token`` 是个**取值函数**而不是一个字符串：Token 会在运行中变（重新登录、被撤销），
    每次换票现取，才不会拿着一把早就作废的钥匙一直敲门。
    """

    def __init__(
        self,
        base_url: str,
        token: Callable[[], str],
        *,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._client = client

    async def mint(self, wechat_account_id: str) -> str:
        token = (self._token() or "").strip()
        if not self._base_url:
            raise TicketError("业务后端地址未配置")
        if not token:
            raise TicketError("这台机器还没登录，换不到票")

        url = f"{self._base_url}{TICKET_PATH}"
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        }

        try:
            if self._client is not None:
                response = await self._client.post(
                    url, headers=headers, json={}, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, headers=headers, json={})
        except Exception as exc:  # noqa: BLE001 —— 任何异常都必须 fail-closed
            # 不记 url 之外的任何细节，避免把凭证写进日志。
            _log.warning("换票请求失败", account=wechat_account_id, error=str(exc))
            raise TicketError(f"换票请求失败: {exc}") from exc

        if response.status_code != 200:
            code = _error_code(response)
            text = MESSAGES.get(code) or f"换票被拒绝: HTTP {response.status_code}"
            # 只记稳定错误码，不记服务端 message——那句话里可能带上下文。
            _log.warning(
                "换票被拒绝",
                account=wechat_account_id,
                status=response.status_code,
                code=code or None,
            )
            raise TicketError(text, code=code)

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise TicketError("换票响应不是合法 JSON") from exc

        # 契约 §5.3 的响应是 snake_case（`session_ticket`），和这一族接口的 camelCase
        # 不同；两种都收，上线时不至于卡在一个下划线上。
        ticket = None
        if isinstance(body, dict):
            ticket = body.get("session_ticket") or body.get("sessionTicket")
        if not isinstance(ticket, str) or not ticket.strip():
            raise TicketError("换票响应缺少 session_ticket")

        # 只记「换到了」，绝不记票据内容。
        _log.info("已换取短票", account=wechat_account_id)
        return ticket.strip()


def _error_code(response: Any) -> str:
    """从错误信封（契约 §3.4）里取稳定错误码。取不到就空着，由状态码兜底。"""
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("code") or "") if isinstance(error, dict) else ""


class LocalFileTicketProvider:
    """从本地文件读票（仅供联调，生产禁用）。

    文件格式：``{"<wechat_account_id>": "<session_ticket>"}``。
    该文件必须在 ``.gitignore`` 覆盖范围内，绝不入库。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def mint(self, wechat_account_id: str) -> str:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise TicketError(f"本地票据文件不存在: {self._path}") from exc
        except Exception as exc:  # noqa: BLE001
            raise TicketError(f"本地票据文件不可读: {exc}") from exc

        if not isinstance(raw, dict):
            raise TicketError("本地票据文件必须是 JSON 对象")

        ticket = raw.get(wechat_account_id)
        if not isinstance(ticket, str) or not ticket.strip():
            raise TicketError(f"本地票据文件里没有 {wechat_account_id} 的票")

        _log.info("已读取本地联调票", account=wechat_account_id)
        return ticket.strip()
