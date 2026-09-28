"""对方发来的图片 / 文件：经 REST 直接传到 AI 端这个会话，拿回 ``file_id``（用户 2026-09-28）。

用户原话：「wx 客户端直接上传到 AI 端、不用 ws……再由 AI 端移动到对应的目录」。

链路：另存到本机（`attachments.AttachmentFetcher`）→ 这里 ``POST {AI 端}/v1/sessions/{session_id}/files``
→ AI 端落进**这个会话的** inbox、回 ``file_id`` → 这一轮 ``message.send`` 只带 ``file_ids``。
契约：AI 服务仓 ``docs/21_wechat_inbound_attachments_contract.md`` §2.0。

守的线：
- **只送字节和文件名，不送路径**。放在哪由 AI 端定（它那边的红线：路径一律服务端派生）；
  这台 Windows 电脑上的路径对 Linux 服务端也没有意义。
- **每次上传现换一张票**（同 WS 建连，``ticket.py`` 四条铁律：不缓存、不落盘、不打日志）。
  这张票只进得去**它自己开的**会话（AI 端按票据 scope 绑定判），别的会话一律 403。
- **传不上去不是错**：换不到票、网络断、非 201、回的个数对不上 → 回 None，调用方退回老办法
  （随 WS 内联 base64，``attachments.inline_attachments``）。这一轮照跑，不因上传失败少回一句话。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import structlog

from .ticket import TicketError, TicketProvider

_log = structlog.get_logger(__name__)

#: AI 端认票据的请求头（AI 服务仓 `src/auth/providers/sessionTicket.ts`）。
TICKET_HEADER = "x-agent-session-ticket"


def http_base_from_ws(ws_url: str) -> str:
    """WS 门地址 → 同一反代前缀下的 REST 基址。

    ``wss://host/agent-ws/v1/ws`` → ``https://host/agent-ws``。WS 和 REST 在 AI 端是同一个进程、
    同一个反代前缀（2026-09-28 实测公网 ``/agent-ws/v1/sessions/*/files`` 回的是 AI 端自己的 401），
    所以不另配一个地址——两个地址分开配，迟早一个改了另一个没改。
    """
    parts = urlsplit(ws_url.strip())
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    path = path.removesuffix("/v1/ws")
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class AgentFileUploader:
    """把这一轮的附件传进 AI 端的会话。"""

    def __init__(
        self,
        *,
        base_url: str,
        ticket_provider: TicketProvider,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tickets = ticket_provider
        # 几 MB 的图走公网，给足时间；超时就退回内联，这一轮不会因此卡死。
        self._timeout = timeout_seconds
        self._client = client

    async def upload(
        self, account_id: str, peer_id: str, session_id: str, files: Sequence[dict[str, Any]]
    ) -> list[str] | None:
        """传上去，按 ``files`` 的顺序回 ``file_id``；任何一步不成回 None（已记日志，调用方退回内联）。

        ``files`` 每项 ``{"file_name", "content_type", "data"}``（``attachments.collect_attachments``）。
        """
        if not files:
            return []
        try:
            ticket = await self._tickets.mint(account_id)
        except TicketError as exc:
            _log.warning("附件上传前换不到票，这一轮改随消息带过去", 会话=peer_id, 原因=str(exc))
            return None
        url = f"{self._base_url}/v1/sessions/{quote(session_id, safe='')}/files"
        multipart = [
            ("files", (str(f["file_name"]), bytes(f["data"]), str(f.get("content_type") or "application/octet-stream")))
            for f in files
        ]
        try:
            if self._client is not None:
                response = await self._client.post(
                    url, headers={TICKET_HEADER: ticket}, files=multipart, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(url, headers={TICKET_HEADER: ticket}, files=multipart)
        except Exception as exc:  # noqa: BLE001 —— 上传只是更好的那条路，坏了退回内联
            # 只记异常类型：url 之外的细节可能带上票据片段（同 ticket.py 的口径）。
            _log.warning("附件上传到 AI 端失败，这一轮改随消息带过去", 会话=peer_id, 原因=type(exc).__name__)
            return None
        if response.status_code != 201:
            _log.warning(
                "AI 端没收下附件，这一轮改随消息带过去", 会话=peer_id, 状态码=response.status_code,
                错误码=_error_code(response),
            )
            return None
        ids = _file_ids(response)
        if ids is None or len(ids) != len(files):
            # 个数对不上就一个都不认：哪份对哪个 id 说不清，宁可整批退回内联再送一次。
            _log.warning("AI 端回的文件编号个数对不上，这一轮改随消息带过去", 会话=peer_id, 送去=len(files))
            return None
        _log.info(
            "附件已直接传到 AI 端",
            会话=peer_id,
            个数=len(ids),
            字节=sum(len(f["data"]) for f in files),
        )
        return ids


def _file_ids(response: httpx.Response) -> list[str] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    rows = body.get("files") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return None
    ids = [str(r.get("file_id") or "") for r in rows if isinstance(r, dict)]
    return ids if all(ids) else None


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("code") or "") if isinstance(error, dict) else ""
