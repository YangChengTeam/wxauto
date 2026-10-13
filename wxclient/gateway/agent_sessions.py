"""配置中心名单里那个「清除」：把这个联系人在 AI 端留下的会话与记忆整个删掉（用户 2026-10-13）。

用户原话：「在微信自动化客户端把清除的实现，加在设置的名单上，整个的清除在名单列表上加一个按钮」。
用途是演示账号重置——同一个人删掉重来，AI 端要按全新的人对待。

链路：``DELETE {AI 端}/v1/sessions/{session_id}?include_memory=true&peer=<会话名>``，票据头同上传文件。
契约：AI 服务仓 ``.agents/specs/147-session-candidate-erasure/handoff-gateway.md`` 与 ``docs/05_rest_api.md`` §4.5。

守的线：
- **只删这张票开得到的会话**（AI 端按票据身份判归属，别人的 → 403）。
- 删的是 AI 端的会话、关系记忆、开场去重、结果通知；**不删**名单、业务端候选人、微信里的聊天记录。
- **每次现换一张票**（``ticket.py`` 四条铁律：不缓存、不落盘、不打日志）。
- 删不成就**照实抛**：这是人点的按钮，失败要让人看见，不像上传那样悄悄退回老路。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
import structlog

from .agent_files import TICKET_HEADER, _error_code
from .ticket import TicketError, TicketProvider

_log = structlog.get_logger(__name__)

#: AI 端删掉某个联系人的会话后推来的事件（`ws/wechatChannel.ts: PEER_ERASED_EVENT`）。
PEER_ERASED_EVENT = "wechat.peer_erased"


class EraseError(RuntimeError):
    """AI 端没删成。文案直接给人看。"""


class AgentSessionEraser:
    """删 AI 端某个联系人的会话（连带联系人记忆）。"""

    def __init__(
        self,
        *,
        base_url: str,
        ticket_provider: TicketProvider,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tickets = ticket_provider
        self._timeout = timeout_seconds
        self._client = client

    async def erase(self, account_id: str, peer_id: str, session_id: str) -> dict[str, Any]:
        """删掉，回 AI 端的回执（只有数量）。会话本来就不在（404）算删好了，回 ``{}``。"""
        try:
            ticket = await self._tickets.mint(account_id)
        except TicketError as exc:
            raise EraseError(f"换不到票，没法去 AI 端删：{exc}") from exc
        url = f"{self._base_url}/v1/sessions/{quote(session_id, safe='')}"
        params = {"include_memory": "true", "peer": peer_id}
        try:
            if self._client is not None:
                response = await self._client.delete(
                    url, headers={TICKET_HEADER: ticket}, params=params, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.delete(url, headers={TICKET_HEADER: ticket}, params=params)
        except Exception as exc:
            # 只记异常类型：细节里可能带票据片段（同 ticket.py 的口径）。
            raise EraseError(f"连不上 AI 端（{type(exc).__name__}）") from exc
        status = response.status_code
        if status == 404:
            _log.info("AI 端本来就没有这个会话，按已清除处理", 会话=peer_id)
            return {}
        if status == 202:
            body = _json(response)
            erasure = body.get("erasure") if isinstance(body.get("erasure"), dict) else {}
            return dict(erasure)
        code = _error_code(response)
        if status == 403:
            raise EraseError("AI 端不让删：这个会话不归当前登录的账号")
        if status == 401:
            raise EraseError("AI 端没认这张票，请重新登录后再试")
        raise EraseError(f"AI 端没删成（状态码 {status} {code}）".replace(" ）", "）"))


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
