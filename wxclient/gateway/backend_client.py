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
        #: 上一次「还没准备好」的原因，用来去重（见 `_note_not_ready`）。
        self._not_ready = ""

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def _path_of(self, url: str) -> str:
        """日志里只出现 path。**整条 url 不进日志**——同 ticket.py 的口径。"""
        return url[len(self._base_url):] or url

    def _note_not_ready(self, why: str) -> None:
        """「这台机器还没准备好」说一次就够。

        原来这条路是**完全静默**的：没配地址或还没登录时，会话名、候选人状态、消息事件
        全都悄悄地不发，而界面上一切正常——查起来毫无线索。但它也不能每次都喊：消息事件
        几秒一趟，照喊会把实时日志刷满。所以只在**原因变了**的时候说一次。
        """
        if self._not_ready == why:
            return
        self._not_ready = why
        _log.info("业务接口这一步跳过了", 原因=why)

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response | None:
        """发一次请求。没配地址/没登录/网络炸 → None（不是错，是「这台机器还没准备好」）。"""
        token = (self._token() or "").strip()
        if not self._base_url:
            self._note_not_ready("没配业务后端地址")
            return None
        if not token:
            self._note_not_ready("还没登录，没有 Client Token")
            return None
        self._not_ready = ""
        path = self._path_of(url)
        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
        # 请求发出去了、后端回了什么，两边各留一行。**只记 method/path/状态码**：
        # 请求体里有候选人姓名、手机号、会话名，那是真实 PII，日志文件不该有第二份。
        _log.info("请求业务接口", method=method, path=path)
        try:
            if self._client is not None:
                response = await self._client.request(
                    method, url, headers=headers, timeout=self._timeout, **kwargs
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.request(method, url, headers=headers, **kwargs)
        except Exception as exc:  # noqa: BLE001 —— 网络抖动不该影响微信侧
            # 只记异常类型与 path：url 之外的细节可能带上凭证片段（同 ticket.py 的口径）。
            _log.warning("业务接口请求失败", path=path, error=type(exc).__name__)
            return None
        _log.info("业务接口已回应", method=method, path=path, status=response.status_code)
        return response
