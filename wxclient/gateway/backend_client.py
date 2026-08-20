"""业务后端 W 面请求的公共底座（凭 Client Token 走的那几条路）。

这里只放**三条路真正共有**的那一段：怎么拿 token、怎么带头、走注入的 client 还是临时开一个、
网络异常怎么记账。响应语义**不收进来**——`candidates.py` 的 409 乐观锁、`contacts.py` 的
「4xx 一律当没拿到」各有各的道理，硬合并会把其中一边的语义压掉。

`token` 是**取值函数**不是字符串（同 `ticket.py`）：Token 会在运行中变（重登、被撤销），
每次现取才不会拿着一把作废的钥匙一直敲门。

★ 永不往上抛：这几条链断了都不该影响已经做完的微信动作。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import structlog

_log = structlog.get_logger(__name__)


class BackendEndpoint:
    """一条 W 面接口的底座。子类自己解释响应，这里只负责把请求送出去。"""

    def __init__(
        self,
        base_url: str,
        token: Callable[[], str],
        *,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
        """发一次请求。没配地址/没登录/网络炸 → None（不是错，是「这台机器还没准备好」）。"""
        token = (self._token() or "").strip()
        if not self._base_url or not token:
            return None
        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
        try:
            if self._client is not None:
                return await self._client.request(
                    method, url, headers=headers, timeout=self._timeout, **kwargs
                )
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.request(method, url, headers=headers, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 网络抖动不该影响微信侧
            # 只记异常类型与 path：url 之外的细节可能带上凭证片段（同 ticket.py 的口径）。
            _log.warning("业务接口请求失败", path=url[len(self._base_url):] or url,
                         error=type(exc).__name__)
            return None
