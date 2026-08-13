"""把 AI 端产出物（招聘海报图等）取到本机，再交给微信发出去（067）。

为什么要这一层：图落在 AI 服务那边的会话 `outbox/`，取图地址 ``/v1/sessions/{sid}/files/{fid}``
**要鉴权、不是直链**；而 wxauto 发文件走的是 UIA 把文件从磁盘拖进微信——它只认**本机路径**。
中间这一段就是本模块：带自己的票去下载 → 落临时目录 → 用完删掉。

守的几条线：
- **只认自家 host**：地址从 ``ai_ws_url`` 推导，非本 origin 的一律拒。AI 端推来的东西一律当外部输入，
  否则等于给「让网关去下载任意 URL」开了口子（SSRF + 把用户机器当下载器）。
- **限体积**：边下边数，超了当场断，不给一个坏指令把磁盘塞满的机会。
- **票是自己的**：用网关自己的 session_ticket 鉴权，不接受上游塞任何凭证过来。
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import structlog

_log = structlog.get_logger(__name__)

#: 单个文件上限。海报图是百 KB 级，20MB 已经宽得离谱——够用又不至于被一条指令拖垮。
MAX_FILE_BYTES = 20 * 1024 * 1024

#: 只允许取会话文件这一条路径。别的路径（哪怕同 host）不给下。
_ALLOWED_PATH = re.compile(r"^/v1/sessions/[^/]+/files/[^/]+$")

#: 文件名里不许出现的东西——下下来的名字要用来在本机建文件，绝不能带路径。
_UNSAFE_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


class DownloadError(RuntimeError):
    """取文件失败。回执里要说人话，所以消息直接面向用户。"""


def http_base_from_ws(ws_url: str) -> str:
    """``wss://host/v1/ws`` → ``https://host``。不新增配置项，就跟着 WS 那一个地址走。"""
    parts = urlsplit(ws_url.strip())
    if parts.scheme not in {"ws", "wss"} or not parts.netloc:
        raise DownloadError(f"AI_WS_URL 不是合法的 ws 地址：{ws_url!r}")
    scheme = "https" if parts.scheme == "wss" else "http"
    return f"{scheme}://{parts.netloc}"


def _safe_name(raw: str, fallback: str) -> str:
    name = _UNSAFE_NAME.sub("_", unquote(raw or "").strip()).strip(". ")
    return name[:120] if name else fallback


def _name_from_headers(headers: Any, fallback: str) -> str:
    """文件名优先取响应的 Content-Disposition——**不让 AI 端报文件名**，少一处可被摆布的输入。"""
    disposition = str(headers.get("content-disposition") or "")
    hit = re.search(r"filename\*=UTF-8''([^;]+)", disposition) or re.search(
        r'filename="?([^";]+)"?', disposition
    )
    return _safe_name(hit.group(1) if hit else "", fallback)


@dataclass(frozen=True)
class FileRef:
    """AI 端下发的一个文件描述符。只有 ID 和地址，**没有路径**（路径服务端不给、也不该给）。"""

    file_id: str
    url: str

    @staticmethod
    def parse(raw: Any) -> FileRef | None:
        if not isinstance(raw, dict):
            return None
        url = str(raw.get("url") or "").strip()
        file_id = str(raw.get("file_id") or "").strip()
        return FileRef(file_id, url) if url else None


class FileFetcher:
    """带票去 AI 服务取会话文件，落到一次性的临时目录。"""

    def __init__(
        self,
        *,
        ws_url: str,
        ticket_provider,          # TicketProvider：mint(account_id) → session_ticket
        max_bytes: int = MAX_FILE_BYTES,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = http_base_from_ws(ws_url)
        self._tickets = ticket_provider
        self._max_bytes = max_bytes
        self._timeout = timeout_seconds
        self._client = client

    def _absolute(self, url: str) -> str:
        """相对路径按自家 base 拼；绝对地址必须同 origin，否则拒。"""
        if url.startswith("/"):
            path = urlsplit(url).path
            if not _ALLOWED_PATH.match(path):
                raise DownloadError(f"不允许的文件地址：{url}")
            return f"{self._base}{url}"
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != self._base:
            raise DownloadError("只允许从劳小智 AI 服务取文件，拒绝外部地址")
        if not _ALLOWED_PATH.match(parts.path):
            raise DownloadError(f"不允许的文件地址：{url}")
        return url

    async def _fetch_one(self, client: httpx.AsyncClient, ref: FileRef, ticket: str, dest_dir: Path) -> Path:
        url = self._absolute(ref.url)
        headers = {"x-agent-session-ticket": ticket}
        async with client.stream("GET", url, headers=headers, timeout=self._timeout) as resp:
            if resp.status_code == 404:
                raise DownloadError("这个文件在 AI 服务上找不到了（可能会话已经清理）")
            if resp.status_code in (401, 403):
                raise DownloadError("取文件被拒：票据无效或这份文件不归本账号")
            if resp.status_code >= 400:
                raise DownloadError(f"取文件失败（HTTP {resp.status_code}）")
            name = _name_from_headers(resp.headers, f"{ref.file_id or 'file'}.bin")
            path = dest_dir / name
            written = 0
            with path.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    written += len(chunk)
                    if written > self._max_bytes:
                        raise DownloadError(f"文件超过 {self._max_bytes // (1024 * 1024)}MB 上限，没有下载")
                    fh.write(chunk)
        _log.info("已取回 AI 端产出文件", file_id=ref.file_id, bytes=written, name=name)
        return path

    @asynccontextmanager
    async def materialize(self, account_id: str, refs: list[FileRef]):
        """下载这批文件，交出本机路径；退出时**连临时目录一起删**（产出物不在这台机器上留底）。"""
        if not refs:
            raise DownloadError("没有可下载的文件")
        ticket = await self._tickets.mint(account_id)
        with TemporaryDirectory(prefix="lxz_send_") as tmp:
            dest = Path(tmp)
            client = self._client or httpx.AsyncClient()
            owns = self._client is None
            try:
                paths = [await self._fetch_one(client, ref, ticket, dest) for ref in refs]
                yield paths
            finally:
                if owns:
                    await client.aclose()
