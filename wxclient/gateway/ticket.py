"""换票：用网关自己的 internal key，换取某个招聘专员的 60 秒短票。

四条铁律（``CLAUDE.md`` §4.1）：
1. 票不缓存、不落盘、不打日志；
2. 每次建连/重连都现换一张；
3. 换票失败一律 fail-closed——停掉该专员的自动化，不降级、不重试绕过；
4. 网关不持有任何员工的 LLM token，票里装什么它不看。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import httpx
import structlog

_log = structlog.get_logger(__name__)

TICKET_PATH = "/internal/agent/wechat/session-ticket"


class TicketError(RuntimeError):
    """换票失败。调用方必须停止该专员的自动化动作，不得继续。"""


class TicketProvider(Protocol):
    async def mint(self, wechat_account_id: str) -> str:
        """返回一张可立即使用的 session_ticket 密文。失败抛 TicketError。"""
        ...


class BusinessTicketProvider:
    """向业务后端换票（生产实现）。"""

    def __init__(
        self,
        base_url: str,
        internal_key: str,
        *,
        timeout_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_key = internal_key
        self._timeout = timeout_seconds
        self._client = client

    async def mint(self, wechat_account_id: str) -> str:
        if not self._base_url or not self._internal_key:
            raise TicketError("业务后端地址或 internal key 未配置")

        url = f"{self._base_url}{TICKET_PATH}"
        headers = {
            "authorization": f"Bearer {self._internal_key}",
            "content-type": "application/json",
        }
        payload = {"wechat_account_id": wechat_account_id}

        try:
            if self._client is not None:
                response = await self._client.post(
                    url, headers=headers, json=payload, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
        except Exception as exc:  # noqa: BLE001 —— 任何异常都必须 fail-closed
            # 不记 url 之外的任何细节，避免把凭证写进日志。
            _log.warning("换票请求失败", account=wechat_account_id, error=str(exc))
            raise TicketError(f"换票请求失败: {exc}") from exc

        if response.status_code != 200:
            _log.warning(
                "换票被拒绝", account=wechat_account_id, status=response.status_code
            )
            raise TicketError(f"换票被拒绝: HTTP {response.status_code}")

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise TicketError("换票响应不是合法 JSON") from exc

        ticket = body.get("session_ticket") if isinstance(body, dict) else None
        if not isinstance(ticket, str) or not ticket.strip():
            raise TicketError("换票响应缺少 session_ticket")

        # 只记「换到了」，绝不记票据内容。
        _log.info("已换取短票", account=wechat_account_id)
        return ticket.strip()


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
