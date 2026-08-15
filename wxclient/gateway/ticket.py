"""换票：用**这台机器登录时拿到的客户端 Token**，换取该招聘专员的 60 秒短票。

四条铁律（``CLAUDE.md`` §4.1）：
1. 票不缓存、不落盘、不打日志；
2. 每次建连/重连都现换一张；
3. 换票失败一律 fail-closed——停掉该专员的自动化，不降级、不重试绕过；
4. 网关不持有任何员工的 LLM token，票里装什么它不看。

★ 2026-08-15 换了凭证：原来用的是一把手抄进这台机器的 `internal key`（服务间共享密钥，
所有装机的人都得抄一遍，撤销要改所有机器）。现在用客户端登录接口签发的、**按会话可撤销的**
Token（`docs/招聘分身_客户端认证与状态接口_v1.md`）：退出登录、换设备、管理员撤销会话，
这台机器立刻换不到票，也就立刻停手。

⚠️ 服务端的 `POST /recruitment-agent/client/ai-ticket` **尚未实现**（那份契约 §4 的清单里
还没有它）。在它上线之前，这条路会稳定地 fail-closed：网关连不上 AI 端，日志里是一句
「换票被拒绝」。这是**明知的缺口**，不要用「先临时读个环境变量」把它绕过去。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx
import structlog

_log = structlog.get_logger(__name__)

TICKET_PATH = "/recruitment-agent/client/ai-ticket"


class TicketError(RuntimeError):
    """换票失败。调用方必须停止该专员的自动化动作，不得继续。"""


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
        # 字段名跟客户端接口一族保持 camelCase（契约 §3.3）。
        payload = {"agentAccountId": wechat_account_id}

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

        if response.status_code == 401:
            # 会话被撤销 / Token 失效（契约 §7）。这是**要人重新登录**的那一类，
            # 和「网络不通」不是一回事，日志里必须分得开。
            _log.warning("客户端会话已失效，换不到票", account=wechat_account_id)
            raise TicketError("客户端登录已失效，请重新登录")
        if response.status_code != 200:
            _log.warning(
                "换票被拒绝", account=wechat_account_id, status=response.status_code
            )
            raise TicketError(f"换票被拒绝: HTTP {response.status_code}")

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise TicketError("换票响应不是合法 JSON") from exc

        # 两种写法都收：这一族接口是 camelCase，AI 端那边的既有字段是 snake_case，
        # 上线时哪一种都不至于卡在一个下划线上。
        ticket = None
        if isinstance(body, dict):
            ticket = body.get("sessionTicket") or body.get("session_ticket")
        if not isinstance(ticket, str) or not ticket.strip():
            raise TicketError("换票响应缺少 sessionTicket")

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
