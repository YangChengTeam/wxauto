"""客户端 Token 的存放处。

契约 §2.2 的原话：Token **不设自动过期**，但必须能随时撤销；客户端必须用 Windows
Credential Manager 或 DPAPI 保存，**禁止写入明文配置文件和日志**。

一个不过期的凭证躺在磁盘上，明文和加密的差别不是「更安全一点」，而是「拿到这个文件
就等于拿到这个 HR 的招聘分身」和「拿不到」。所以这里的规矩是硬的：

* Windows → DPAPI（`CryptProtectData`，按**当前用户**加密，换个 Windows 账号解不开）。
* 非 Windows（开发机、CI）→ **一个字节都不落盘**，只留在内存里。宁可每次重开都要重新
  登录，也不在 Linux 上留一份明文 Token 当「反正只是开发环境」。

密文之外还要记住几样不算凭证的东西（sessionId、agentId、显示名），它们决定了「重开
之后还认不认得这台机器上是谁」，明文存在同一个 JSON 里没有问题。

★ 2026-08-15 起「记住登录状态」**连密码一起记**（用户要求），同样走 DPAPI，和 Token
分开加密。密码比 Token 值钱——Token 只开得了 `/recruitment-agent/client/*`，密码开得了
整个企业端（工资、合同、人才库）。所以它配了三条硬约束：

* 只在人**明确勾上「记住登录状态」**时才写；取消勾选连旧的一起清掉。
* 只用于「把密码回填进登录框」和「本机 Token 没了但人没变」时的自动重登
  （`ui/login_page.py`）。
* **会话被撤销（管理员撤、被别的设备顶掉、账号停用）时和 Token 一起清掉**——见
  `auth.ClientAuth.handle_revoked`。不清的话，管理员刚撤销，这台机器转头拿密码
  自己又登回来了，契约 §2.2「立即失效」就成了一句空话。

★ 2026-08-16 改：**人自己点「退出登录」不再清密码**（用户要求「记住登录状态应该也记住
密码，把密码回填到密码框」）。走 `forget_session()`：Token 和会话作废，邮箱密码留着，
外加落一个 ``auto_login=False``。那个标记是这条改动的配套，不是可选项——没有它，
「退出登录」只在本次运行里成立，客户端一重开就拿着记住的密码自己登回去了。
撤销那条路仍然是 `clear()`，全清；管控的刹车不跟着这条便利改动松。
"""

from __future__ import annotations

import base64
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FILE_NAME = "client-session.json"

#: DPAPI 的附加熵。换个应用即使跑在同一个 Windows 账号下，也解不开这份密文。
_ENTROPY = b"laoxiaozhi.recruitment-agent.client-token.v1"


@dataclass(frozen=True)
class StoredSession:
    """磁盘上那份会话。``token`` 是唯一的凭证，其余只是「上次是谁」。"""

    token: str = ""
    session_id: str = ""
    email: str = ""
    user_name: str = ""
    agent_id: str = ""
    agent_name: str = ""
    tenant_name: str = ""
    password: str = ""
    """勾了「记住登录状态」才有值。和 ``token`` 一样单独 DPAPI 加密，绝不明文落盘。"""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.token and self.agent_id)


class Protector:
    """加解密后端。有两种实现，选哪种只看这台机器是不是 Windows。"""

    available = False
    name = "none"

    def protect(self, plain: str) -> str:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def unprotect(self, blob: str) -> str:  # pragma: no cover - 由子类实现
        raise NotImplementedError


class DpapiProtector(Protector):
    """Windows DPAPI。按当前用户加密，密文离开这个账号就是一串废字节。"""

    available = True
    name = "dpapi"

    def protect(self, plain: str) -> str:
        from ctypes import windll  # 只在 Windows 上 import，别污染别的平台

        blob = _dpapi_call(windll.crypt32.CryptProtectData, plain.encode("utf-8"))
        return base64.b64encode(blob).decode("ascii")

    def unprotect(self, blob: str) -> str:
        from ctypes import windll

        raw = base64.b64decode(blob.encode("ascii"))
        return _dpapi_call(windll.crypt32.CryptUnprotectData, raw).decode("utf-8")


class RefusingProtector(Protector):
    """非 Windows：不加密，也就**不落盘**。Token 只在内存里活到进程结束。"""

    available = False
    name = "memory"


def default_protector() -> Protector:
    return DpapiProtector() if sys.platform == "win32" else RefusingProtector()


class TokenStore:
    """读写这台机器上的客户端会话。线程安全性由调用方保证（只在 UI 线程用）。"""

    def __init__(self, data_dir: Path, protector: Protector | None = None) -> None:
        self.path = Path(data_dir) / FILE_NAME
        self._protector = protector or default_protector()
        self._memory: StoredSession | None = None

    @property
    def persistent(self) -> bool:
        """这台机器上的 Token 能不能活过重启。Linux 上是 False，界面据此说实话。"""
        return self._protector.available

    def _read_raw(self) -> dict[str, Any] | None:
        """磁盘上那份 JSON。读不到就是 None——这台机器上没登录过，或者文件坏了。"""
        if not self._protector.available or not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001 - 文件坏了/权限没了：当成没登录过
            return None
        return raw if isinstance(raw, dict) else None

    def load(self) -> StoredSession | None:
        if self._memory is not None:
            # 记着的那份不够用（Token 解不开、没有分身）就照样回 None——但**留着**它，
            # `credentials()` 还要从里面取邮箱和密码去自动重登。
            return self._memory if self._memory.usable else None
        raw = self._read_raw()
        if raw is None:
            return None
        token = self._unprotect_optional(raw.get("token_protected"))
        session = StoredSession(
            token=token,
            password=self._unprotect_optional(raw.get("password_protected")),
            session_id=str(raw.get("session_id") or ""),
            email=str(raw.get("email") or ""),
            user_name=str(raw.get("user_name") or ""),
            agent_id=str(raw.get("agent_id") or ""),
            agent_name=str(raw.get("agent_name") or ""),
            tenant_name=str(raw.get("tenant_name") or ""),
        )
        self._memory = session
        return session if session.usable else None

    def credentials(self) -> tuple[str, str]:
        """记住的（邮箱，密码）。**Token 还在不在都能取**——它正是给「Token 没了、
        人没换」那种情况用的（换了 Windows 账号、文件损坏、重装）。

        取不到就返回两个空串：没记过、没勾过、或者这台机器根本存不住（非 Windows）。
        """
        if self._memory is not None:
            return self._memory.email, self._memory.password
        raw = self._read_raw()
        if raw is None:
            return "", ""
        return str(raw.get("email") or ""), self._unprotect_optional(raw.get("password_protected"))

    def _unprotect_optional(self, blob: Any) -> str:
        """解一段可有可无的密文。解不开就当没有——它不该拦住登录。"""
        if not blob:
            return ""
        try:
            return self._protector.unprotect(str(blob))
        except Exception:  # noqa: BLE001
            return ""

    def auto_login_allowed(self) -> bool:
        """这台机器上「记住的账号」还准不准自动登一次。

        默认准。只有人**自己点过退出登录**才会变成 False（`forget_session`），而且这个
        「不准」要跨重启活着——否则退出登录就只是本次运行里的事，重开客户端又自己登回去了。
        重新登一次（`save`）就再把它翻回 True。
        """
        raw = self._read_raw()
        return bool(raw.get("auto_login", True)) if raw is not None else True

    def save(self, session: StoredSession) -> None:
        self._memory = session
        if not self._protector.available:
            return  # 加密不了就不写——磁盘上永远不该出现明文 Token
        data = {k: v for k, v in asdict(session).items()
                if k not in {"token", "password", "extra"}}
        data["token_protected"] = self._protector.protect(session.token)
        if session.password:
            data["password_protected"] = self._protector.protect(session.password)
        # 人刚亲手登进来，自动登录的闸自然重新打开（上一次可能是主动退出登录关掉的）。
        data["auto_login"] = True
        self._write(data)

    def forget_session(self) -> None:
        """退出登录：**Token 和会话作废，记住的邮箱密码留着**（用户 2026-08-16）。

        留着是为了把密码回填进登录框——勾了「记住登录状态」的人要的就是别再打一遍。
        同时落 ``auto_login=False``：填好了等他点「登录」，别自己登回去。这两件是一套，
        少一件就成了「点了退出登录，重开又在里面」。

        没有密码可留（没勾过记住）就退化成 `clear()`——留一份只剩邮箱的文件没有意义。
        """
        raw = self._read_raw()
        self._memory = None
        if raw is None or not raw.get("password_protected"):
            self.clear()
            return
        raw.pop("token_protected", None)
        raw["session_id"] = ""
        raw["auto_login"] = False
        self._write(raw)

    def _write(self, data: dict[str, Any]) -> None:
        """原子写这份 JSON。**写临时文件再 replace**——中途断电不会留下半份读不出来的。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            self.path.chmod(0o600)  # Windows 上是空操作，别的平台有意义
        except OSError:
            pass

    def clear(self) -> None:
        """退出登录 / 会话被撤销。**先删文件再清内存**，中途出错也不会留下一份还能用的。"""
        self._memory = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def _dpapi_call(func: Any, payload: bytes) -> bytes:
    """调一次 DPAPI。两个方向的入参出参形状一模一样，所以共用这一段。"""
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> _Blob:
        buffer = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

    source = _blob(payload)
    entropy = _blob(_ENTROPY)
    result = _Blob()
    ok = func(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0,
              ctypes.byref(result))
    if not ok:
        raise OSError(ctypes.GetLastError(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)
