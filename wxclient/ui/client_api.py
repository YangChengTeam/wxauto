"""招聘分身客户端 ↔ 业务后端的四个接口。

契约见 `docs/招聘分身_客户端认证与状态接口_v1.1.md`（本文件是它的客户端实现，字段名、
枚举、错误码都以那份为准；**改动必须两边一起改**）。

```text
POST /recruitment-agent/client/auth/login          邮箱密码登录，不触发邮箱 OTP
POST /recruitment-agent/client/auth/logout         退出并撤销当前 Token
PUT  /recruitment-agent/client/status              客户端在线/离线/异常
PUT  /recruitment-agent/client/wechat-uia/status   本机微信 UIA 连接状态
```

三件必须做对的事：

1. **没有心跳**（契约 §7）。客户端只在状态**变化**时上报；服务端也不会按时间把 online
   自动改成 offline。所以「正常退出前先报 offline」是硬要求——漏了这一步，企业端会一直
   显示这台机器在线。
2. **Token 不进日志、不进错误详情**。这里只在内存里传，落盘由 `token_store` 负责（DPAPI）。
3. **登录失败不区分「邮箱不存在」和「密码错误」**（契约 §3.4）：两种都翻成同一句话，
   否则这个接口就成了一个「这个邮箱在不在册」的查询器。

用标准库 `urllib` 而不是 `httpx`：登录发生在**启动最早的那几秒**，桌面端在那一段为了
少 import 了不少东西（见 `ui/__main__.py` 顶上的说明），不值得为几个 POST 再拉一整个
http 栈进来。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

#: 契约 §5.1：固定值，服务端用它做审计与契约校验。
CLIENT_TYPE = "recruitment_agent_windows"

LOGIN_PATH = "/recruitment-agent/client/auth/login"
LOGOUT_PATH = "/recruitment-agent/client/auth/logout"
CLIENT_STATUS_PATH = "/recruitment-agent/client/status"
WECHAT_UIA_STATUS_PATH = "/recruitment-agent/client/wechat-uia/status"

#: 会话被撤销时服务端回的 code（契约 §7 无心跳时的状态边界）。看到它必须清 Token 回登录页。
REVOKED_CODE = "CLIENT_SESSION_REVOKED"
#: 另一台设备已有活动会话（契约 §5.1）。要问过人才能顶掉，不能自己决定。
SESSION_EXISTS_CODE = "ACTIVE_CLIENT_SESSION_EXISTS"

#: 错误码 → 能直接摆到界面上的一句话（契约 §5.1 错误码表）。
#: 服务端也会给 message，但它面向的是所有调用方；这里挑的是「这台机器上的人该怎么办」。
MESSAGES = {
    "VALIDATION_ERROR": "填的内容格式不对",
    "INVALID_CREDENTIALS": "邮箱或密码错误",
    "ACCOUNT_UNAVAILABLE": "这个账号已停用，请联系管理员",
    "TENANT_UNAVAILABLE": "所属公司已停用或到期，请联系管理员",
    "CLIENT_LOGIN_DENIED": "安全策略拒绝了这次登录，请联系管理员",
    "AGENT_NOT_PROVISIONED": "这个账号还没有开通招聘分身",
    "AGENT_ASSOCIATION_CONFLICT": "账号与招聘分身的关联有冲突，请联系管理员",
    SESSION_EXISTS_CODE: "这个招聘分身已在另一台电脑登录",
    "ACCOUNT_LOCKED": "连续登录失败次数太多，账号已临时锁定",
    "CLIENT_UPGRADE_REQUIRED": "客户端版本过低，请升级后再登录",
    "RATE_LIMITED": "登录太频繁了，等一会儿再试",
    REVOKED_CODE: "客户端登录已失效，请重新登录",
}


class ApiError(RuntimeError):
    """一次接口调用失败。``message`` 是能直接摆到界面上的中文。"""

    def __init__(self, message: str, *, code: str = "", status: int = 0,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    @property
    def revoked(self) -> bool:
        return self.code == REVOKED_CODE or (self.status == 401 and not self.code)

    @property
    def session_exists(self) -> bool:
        return self.code == SESSION_EXISTS_CODE

    @property
    def bad_payload(self) -> bool:
        """请求本身不合格——**同样的内容重发多少次都还是这个结果**。

        用来把它和「网络抖了一下」分开：后者要重试，前者重试就是每 15 秒刷一条一样的
        错误日志，把真正的问题埋掉。
        """
        return self.status == 400 or self.code == "VALIDATION_ERROR"


def now_iso() -> str:
    """契约 §3.3 的时间格式：UTC ISO-8601 带毫秒和 Z。"""
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def new_event_id() -> str:
    """状态上报的幂等键（契约 §7/§8）：重复的 eventId 服务端不重复写状态和审计。"""
    return str(uuid.uuid4())


class ClientApi:
    """四个接口的薄封装。**只管发出去、拿回来、把错误翻成人话**，不认识业务状态机。"""

    def __init__(self, base_url: str, *, timeout: float = 8.0) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self._timeout = timeout

    # ── 登录 / 退出 ──────────────────────────────────────────────

    def login(self, email: str, password: str, *, device_id: str, device_name: str,
              client_version: str, os_version: str = "",
              replace_existing: bool = False) -> dict[str, Any]:
        payload = {
            "email": email.strip().lower(),  # 契约 §5.1：去空格转小写后匹配
            "password": password,
            "clientType": CLIENT_TYPE,
            "deviceId": device_id,
            "deviceName": device_name,
            "clientVersion": client_version,
            "replaceExistingSession": bool(replace_existing),
        }
        if os_version:
            payload["osVersion"] = os_version[:120]
        return self._request("POST", LOGIN_PATH, payload=payload)

    def logout(self, token: str) -> dict[str, Any]:
        return self._request("POST", LOGOUT_PATH, token=token)

    # ── 状态 ─────────────────────────────────────────────────────

    def put_client_status(self, token: str, status: str, *, client_version: str,
                          reason: str = "", event_id: str = "",
                          occurred_at: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "eventId": event_id or new_event_id(),
            "status": status,
            "clientVersion": client_version,
            "occurredAt": occurred_at or now_iso(),
        }
        if reason:
            payload["reason"] = reason
        return self._request("PUT", CLIENT_STATUS_PATH, payload=payload, token=token)

    def put_wechat_uia_status(self, token: str, status: str, *,
                              instance: dict[str, Any] | None = None,
                              failure: dict[str, Any] | None = None,
                              capability_status: str = "",
                              event_id: str = "", occurred_at: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "eventId": event_id or new_event_id(),
            "status": status,
            "occurredAt": occurred_at or now_iso(),
        }
        if capability_status:
            # 契约 §8：只有 passed 才让企业端进入「运行中」。
            payload["capabilityStatus"] = capability_status
        if instance:
            payload["wechatInstance"] = instance
        if failure:
            payload["failure"] = failure
        return self._request("PUT", WECHAT_UIA_STATUS_PATH, payload=payload, token=token)

    # ── 发请求 ───────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None,
                 token: str = "") -> dict[str, Any]:
        import urllib.error
        import urllib.request

        if not self.base_url:
            raise ApiError("还没配业务端地址，先去配置中心填上")

        headers = {"content-type": "application/json",
                   "x-request-id": str(uuid.uuid4())}
        if token:
            headers["authorization"] = f"Bearer {token}"
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}{path}", data=data,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            raise _from_http_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 - 连不上、超时、DNS，对人来说都是一句话
            raise ApiError(f"连不上业务端：{exc}") from exc
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise ApiError("业务端返回的不是合法 JSON") from exc
        return body if isinstance(body, dict) else {}


def _from_http_error(exc: Any) -> ApiError:
    """把错误信封（契约 §3.4）翻成 ApiError。

    ★ 只读 `error.code` 决定文案，**不直接把服务端的 message 摆到登录页上**：
    那句话可能是给运维看的（含内部名词），而这一屏是给招聘专员看的。拿不到 code 时
    才退回服务端的 message。
    """
    status = int(getattr(exc, "code", 0) or 0)
    code = ""
    message = ""
    details: dict[str, Any] = {}
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = str(error.get("message") or "")
            raw_details = error.get("details")
            # 校验失败时服务端给的是一个**数组**（哪个字段错了），不是对象。
            # 它恰恰是最该留下来的东西——2026-08-15 那次 400 就是靠它一眼看出
            # 少了 wechatInstance.accountRef。
            if isinstance(raw_details, dict):
                details = raw_details
            elif isinstance(raw_details, list):
                details = {"fields": raw_details}
    except Exception:  # noqa: BLE001 - 拿不到信封就只剩状态码可说
        pass
    text = MESSAGES.get(code) or message or f"业务端拒绝了这次请求（HTTP {status}）"
    return ApiError(text, code=code, status=status, details=details)
