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


def error_of(response: httpx.Response) -> tuple[str, str]:
    """从错误响应里取出**原因码**，取不出就空着。

    ★ 为什么非要这一步：原来 4xx 只记一个状态码，于是「被拒了」和「为什么被拒」之间
    差着一次登机器翻代码。2026-08-31 就吃过一次——`GET /client/contacts` 连着几天回 409，
    日志里只有 `status=409`，而真正的原因码是 `WECHAT_ACCOUNT_NOT_BOUND`（这台设备
    当前没绑微信账号，一个分身两台设备时落败那台就是这样）。会话名同步因此整段冻住、
    派生监听名单跟着停在那一刻，而现场看不出任何异常。有这个码就是一眼的事。

    **只取 `error.code` 与 `error.message`，绝不取 `details`**：业务端的错误信封
    （`{error:{code,message,details?}}`）里 `details` 会回显请求内容，而请求体里有候选人
    姓名、手机号、会话名——那是真实 PII，日志里不该有第二份（同 `_send` 的口径）。
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 —— 错误响应不一定是 JSON，取不到就算了
        return "", ""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "", ""
    code = str(error.get("code") or "")
    # 截断：这条是给人看的一行日志，后端哪天回一段长文不该把日志撑爆。
    message = str(error.get("message") or "")[:200]
    return code, message


def error_shape(response: httpx.Response) -> str:
    """错误响应的**顶层键名**，只在 `error_of` 什么都取不出来时用来兜底。

    ★ 为什么要这一条（2026-09-01）：`error_of` 只认 `{error:{code,message}}` 这一种信封，
    形状对不上就**静默**返回空。真机上 `GET /client/contacts` 连着回 409，而日志里
    `错误码`/`说明` 两个字段都是空的——于是「被拒了」和「为什么被拒」之间又差着一次登机器，
    正是 `error_of` 当初要消灭的那件事，只是换了个形状溜回来。

    **只记键名，不记值**：`details` 会回显请求内容，里面有候选人姓名、手机号、会话名，
    那是真实 PII（同 `error_of` 的口径）。键名本身不承载 PII。
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 —— 不是 JSON 就如实说，别猜
        text = (response.text or "").strip()
        return "非 JSON" if text else "空响应体"
    if isinstance(body, dict):
        return "键：" + "、".join(sorted(body)[:12]) if body else "空对象"
    return type(body).__name__


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
        headers = {"authorization": f"Bearer {token}"}
        # multipart（104 的证件/简历上传）由 httpx 自己定 boundary 和 content-type；
        # 只有 JSON 请求才手写这一头。
        if "files" not in kwargs:
            headers["content-type"] = "application/json"
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
        extra: dict[str, str] = {}
        if response.status_code >= 400:
            # 取不到就**不加这两个键**：5xx 常常压根不是 JSON，硬塞两个空字段只会让日志变脏。
            code, message = error_of(response)
            if code or message:
                extra = {"错误码": code, "说明": message}
        _log.info("业务接口已回应", method=method, path=path, status=response.status_code, **extra)
        return response
