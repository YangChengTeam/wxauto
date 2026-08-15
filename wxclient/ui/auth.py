"""登录与「登录后取配置」。

**为什么要有登录**：在这之前，这台机器上的人手工往「参数设置」里填微信号、AI 端地址、
还有网关唯一的那把 internal key——凭证靠手抄，装十台机器就抄十遍，错一个字符的表现是
「服务起来了但一直连不上」。改成登录之后，参数由后端按这个账号下发，人只需要认得自己
的账号密码。

**这一版后端还没有**（用户 2026-08-15：「本次只做客户端，后端留插座」）。所以这里做成两个
可替换的 provider，接口一模一样：

* ``RemoteProvider``——真后端。契约见本文件末尾 ``REMOTE_CONTRACT``，也抄了一份进 AGENTS.md。
  配了 ``WXCLIENT_UI_LOGIN_API_URL`` 就走它。
* ``LocalAccountProvider``——本机兜底。账号存在本机一个文件里（scrypt 派生，不存明文），
  配置就用本机 ``gateway.env`` 那份。它让整条流程今天就能跑通、能测，后端就绪时只是换个
  provider，界面一行都不用动。

**刻意不做的事**：不给「跳过登录」的后门。留一个「没配后端就放行」的分支，等于把登录做成
装饰——那还不如不做。本机模式照样要密码，第一次运行时当场设一个。

用 ``urllib``（标准库）而不是 ``httpx`` 发请求：登录发生在**启动最早的那几秒**，那正是
桌面端花了大力气瘦身的一段（见 ``ui/__main__.py`` 顶上的说明），不值得为两个 POST 再拉
一整个 http 栈进来。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from hashlib import scrypt
from pathlib import Path
from typing import Any, Protocol

ACCOUNT_FILE = "console-account.json"

#: scrypt 参数。登录一次算一遍，几十毫秒——人感觉不到，暴力猜密码的人感觉得到。
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_KEY_LEN = 32

LOGIN_PATH = "/desktop/login"
CONFIG_PATH = "/desktop/config"

#: 后端接口契约（客户端这一侧已经按它写好了，后端照着实现就能接上）。
REMOTE_CONTRACT = """\
POST {base}/desktop/login
    请求  {"account": "...", "password": "...", "device_id": "..."}
    成功  200 {"token": "...", "expires_at": "2026-08-15T12:00:00Z",
               "display_name": "张三", "tenant_name": "某某劳务"}
    失败  401 {"error": {"code": "INVALID_CREDENTIALS", "message": "账号或密码错误"}}

GET {base}/desktop/config      Authorization: Bearer <token>
    成功  200 {"settings": {"WXCLIENT_UI_ACCOUNTS": "zk_weixin", ...},
               "plans": [{"id": "...", "title": "...", "detail": "...",
                          "next_run_at": "...", "enabled": true}],
               "server_time": "2026-08-15T12:00:00Z"}
    失败  401 → 票过期，客户端会退回登录页

settings 里只放 WXCLIENT_* 键；客户端原样落到 gateway.remote.env，本机在「配置中心」
改过的同名键仍然盖过它（用户 2026-08-15 裁决：本地可以改、后端可下发）。
"""


class AuthError(RuntimeError):
    """登录/取配置失败。message 是能直接摆到界面上的中文。"""


@dataclass(frozen=True)
class Identity:
    """登录成功之后，我们知道的关于「谁在用这台机器」的事。

    ``token`` **只在内存里**：它是能换配置的凭证，落盘就等于把登录这一步作废了。
    """

    account: str
    display_name: str = ""
    tenant_name: str = ""
    token: str = ""
    expires_at: str = ""

    @property
    def title(self) -> str:
        return self.display_name or self.account


@dataclass(frozen=True)
class Plan:
    """后端下发的一条计划。这一版界面只负责显示，不在本机编辑。"""

    id: str
    title: str
    detail: str = ""
    next_run_at: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class RemoteConfig:
    """一次「取配置」的结果。"""

    values: dict[str, str] = field(default_factory=dict)
    plans: tuple[Plan, ...] = ()
    source: str = ""  # 界面上要说清这份配置是哪来的：「后端」还是「本机」
    server_time: str = ""


class AuthProvider(Protocol):
    name: str

    def needs_setup(self) -> bool:
        """首次运行、还没有账号可用（只有本机模式会为真）。"""
        ...

    def login(self, account: str, password: str) -> Identity: ...

    def fetch_config(self, identity: Identity) -> RemoteConfig: ...


# ── 本机账号（兜底） ──────────────────────────────────────────────


def account_path(data_dir: Path) -> Path:
    return Path(data_dir) / ACCOUNT_FILE


def hash_password(password: str, salt: str) -> str:
    return scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                  dklen=_KEY_LEN, **_SCRYPT).hex()


def make_account_record(account: str, password: str) -> dict[str, str]:
    salt = secrets.token_hex(16)
    return {"account": account.strip(), "salt": salt, "password": hash_password(password, salt)}


def verify_account(record: dict[str, Any], account: str, password: str) -> bool:
    """核对账号密码。比较用 ``compare_digest``——普通 ``==`` 会随匹配长度早退。"""
    if str(record.get("account", "")).strip().lower() != account.strip().lower():
        return False
    salt = str(record.get("salt", ""))
    stored = str(record.get("password", ""))
    if not salt or not stored:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


class LocalAccountProvider:
    """本机账号 + 本机配置。后端就绪之前的兜底，也是单测里的那一档。"""

    name = "本机"

    def __init__(self, data_dir: Path) -> None:
        self._path = account_path(data_dir)

    def needs_setup(self) -> bool:
        return not self._path.exists()

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError as exc:
            raise AuthError("这台机器还没设过账号") from exc
        except Exception as exc:  # 文件坏了要说人话，不是甩 JSONDecodeError
            raise AuthError(f"本机账号文件读不了：{exc}") from exc
        if not isinstance(raw, dict):
            raise AuthError("本机账号文件格式不对")
        return raw

    def create(self, account: str, password: str) -> Identity:
        """首次运行时当场设一个账号。已经设过就不给悄悄改掉。"""
        if not account.strip():
            raise AuthError("账号不能为空")
        if len(password) < 6:
            raise AuthError("密码至少 6 位")
        if self._path.exists():
            raise AuthError("这台机器已经设过账号了")
        record = make_account_record(account, password)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        try:
            self._path.chmod(0o600)  # Windows 上是空操作，Linux/测试环境里有意义
        except OSError:
            pass
        return Identity(account=record["account"], display_name=record["account"])

    def login(self, account: str, password: str) -> Identity:
        record = self._read()
        if not verify_account(record, account, password):
            raise AuthError("账号或密码不对")
        return Identity(account=str(record.get("account", account)),
                        display_name=str(record.get("account", account)))

    def fetch_config(self, identity: Identity) -> RemoteConfig:
        """本机模式没有下发这回事：配置就是「配置中心」里填的那份。"""
        return RemoteConfig(values={}, plans=(), source=self.name)


# ── 真后端 ────────────────────────────────────────────────────────


class RemoteProvider:
    """向后端登录、取配置。契约见 ``REMOTE_CONTRACT``。"""

    name = "后端"

    def __init__(self, base_url: str, *, device_id: str = "", timeout: float = 8.0) -> None:
        self._base = base_url.rstrip("/")
        self._device_id = device_id
        self._timeout = timeout

    def needs_setup(self) -> bool:
        return False  # 账号是后端发的，这台机器上没什么可设的

    def login(self, account: str, password: str) -> Identity:
        body = self._post(LOGIN_PATH, {
            "account": account.strip(), "password": password, "device_id": self._device_id,
        })
        token = str(body.get("token") or "")
        if not token:
            raise AuthError("后端没给票，登不进去")
        return Identity(
            account=account.strip(),
            display_name=str(body.get("display_name") or account.strip()),
            tenant_name=str(body.get("tenant_name") or ""),
            token=token,
            expires_at=str(body.get("expires_at") or ""),
        )

    def fetch_config(self, identity: Identity) -> RemoteConfig:
        body = self._get(CONFIG_PATH, identity.token)
        raw = body.get("settings")
        values = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        # 只收 WXCLIENT_* ——后端多下发的键不该悄悄变成本机环境变量。
        values = {k: v for k, v in values.items() if k.startswith("WXCLIENT_")}
        return RemoteConfig(
            values=values,
            plans=parse_plans(body.get("plans")),
            source=self.name,
            server_time=str(body.get("server_time") or ""),
        )

    # 下面两个只做「发出去、拿回来、错误翻成人话」，不认识业务字段。

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request(path, data=data, headers={"content-type": "application/json"})

    def _get(self, path: str, token: str) -> dict[str, Any]:
        return self._request(path, data=None, headers={"authorization": f"Bearer {token}"})

    def _request(self, path: str, *, data: bytes | None, headers: dict[str, str]) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        if not self._base:
            raise AuthError("没配后端地址")
        request = urllib.request.Request(f"{self._base}{path}", data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AuthError(_http_message(exc)) from exc
        except Exception as exc:  # 连不上、超时、返回不是 JSON，都是一句话
            raise AuthError(f"连不上后端：{exc}") from exc
        if not isinstance(body, dict):
            raise AuthError("后端返回的不是一个对象")
        return body


def _http_message(exc: Any) -> str:
    """把 HTTP 错误翻成能摆到登录页上的一句话。"""
    status = getattr(exc, "code", 0)
    if status == 401:
        return "账号或密码不对"
    if status == 403:
        return "这个账号没有开通桌面端"
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        detail = str(payload.get("error", {}).get("message") or "")
    except Exception:  # noqa: BLE001 —— 拿不到细节就只报状态码
        detail = ""
    return f"后端拒绝了这次请求（HTTP {status}）" + (f"：{detail}" if detail else "")


def parse_plans(raw: object) -> tuple[Plan, ...]:
    """把后端下发的计划解析成 ``Plan``。烂数据跳过，不让一条脏记录把整页搞崩。"""
    if not isinstance(raw, list):
        return ()
    plans: list[Plan] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        plans.append(Plan(
            id=str(item.get("id") or f"plan-{index}"),
            title=title,
            detail=str(item.get("detail") or ""),
            next_run_at=str(item.get("next_run_at") or ""),
            enabled=bool(item.get("enabled", True)),
        ))
    return tuple(plans)


def build_provider(data_dir: Path, environ: dict[str, str] | None = None) -> AuthProvider:
    """配了后端地址走后端，否则走本机账号。"""
    target = os.environ if environ is None else environ
    base = (target.get("WXCLIENT_UI_LOGIN_API_URL") or "").strip()
    if base:
        return RemoteProvider(base, device_id=(target.get("WXCLIENT_UI_DEVICE_ID") or "").strip())
    return LocalAccountProvider(data_dir)


# ── 「记住账号」 ──────────────────────────────────────────────────

UI_STATE_FILE = "console-ui.json"


def remember_account(data_dir: Path, account: str) -> None:
    """记住上次登录的账号名。**只记账号，永不记密码**——密码存在本机等于没有密码。"""
    path = Path(data_dir) / UI_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_account": account.strip()}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass  # 记不住只是下次要重打一遍账号，不该让登录失败


def forget_account(data_dir: Path) -> None:
    try:
        (Path(data_dir) / UI_STATE_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def last_account(data_dir: Path) -> str:
    try:
        raw = json.loads((Path(data_dir) / UI_STATE_FILE).read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001 - 文件没有/坏了：当成没记过
        return ""
    return str(raw.get("last_account") or "") if isinstance(raw, dict) else ""
