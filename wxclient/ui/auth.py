"""登录：谁在用这台机器。

**只有一条路**：邮箱 + 密码，向业务后端的招聘分身客户端接口登录
（`docs/招聘分身_客户端认证与状态接口_v1.1.md`，实现在 `client_api.py`）。

★ 2026-08-15 之前这里还有一个「本机模式」——账号存在本机文件里、参数也用本机那份，
用来在后端还没有的时候把流程跑起来。**现在删掉了**：正式接口已经定稿，留着两条登录路
的后果是「这台机器到底归谁」有两个答案，而其中一个（本机账号）**服务端根本不知道**，
于是撤销会话、停用账号、换设备这些管控对它一律无效。宁可后端没上线时登不进去，
也不留一条服务端管不到的入口。

三件事这里必须守住：

* **Token 不落明文**：磁盘上那份由 `token_store` 用 DPAPI 加密；非 Windows 一个字节都不写。
* **会话被撤销要认得出来**：任何一次调用回 `401 CLIENT_SESSION_REVOKED`，就地清 Token、
  回登录页（契约 §7）。这是「不过期的 Token」唯一的刹车。
* **顶掉别人的设备要问过人**：`409 ACTIVE_CLIENT_SESSION_EXISTS` 不自动重试，把「另一台
  电脑正在用」摆给人看，他点了确认才带 `replaceExistingSession=true` 再来一次（契约 §5.1）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import device as device_mod
from .client_api import ApiError, ClientApi
from .token_store import StoredSession, TokenStore


class AuthError(RuntimeError):
    """登录失败。message 是能直接摆到界面上的中文。"""

    def __init__(self, message: str, *, code: str = "",
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    @property
    def needs_replace_confirm(self) -> bool:
        """另一台电脑还占着 → 要人点头才能顶掉。"""
        return self.code == "ACTIVE_CLIENT_SESSION_EXISTS"


@dataclass(frozen=True)
class Identity:
    """登录成功之后，我们知道的关于「谁在用这台机器」的事。

    ``token`` 只在内存和 DPAPI 密文里；**绝不进日志、不进环境变量、不进 gateway.env**。
    """

    email: str = ""
    user_id: str = ""
    user_name: str = ""
    agent_id: str = ""
    agent_name: str = ""
    session_id: str = ""
    tenant_name: str = ""
    token: str = ""
    stage: str = ""
    @property
    def title(self) -> str:
        return self.user_name or self.email

    @property
    def account(self) -> str:
        """网关那边用来给消息记账的账号标识。

        用 ``agent_id``：一个 HR 一个分身、一个分身一个微信（PRD §1.3），它是服务端认的
        那个稳定标识。原来这里是人手抄的微信号——抄错一个字符的表现是「消息游标全乱」。
        """
        return self.agent_id or self.email


class ClientAuth:
    """登录 / 退出 / 记住这台机器上的会话。GUI 只调这一层，不碰 HTTP。"""

    name = "劳小智"

    def __init__(self, base_url: str, data_dir: Path, *, client_version: str = "",
                 api: ClientApi | None = None, store: TokenStore | None = None,
                 on_revoked: Callable[[], None] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.client_version = client_version
        self.api = api or ClientApi(base_url)
        self.store = store or TokenStore(self.data_dir)
        self.device = device_mod.load(self.data_dir)
        self._on_revoked = on_revoked
        self._identity: Identity | None = None

    # ── 状态 ─────────────────────────────────────────────────────

    @property
    def identity(self) -> Identity | None:
        return self._identity

    @property
    def token(self) -> str:
        return self._identity.token if self._identity else ""

    @property
    def configured(self) -> bool:
        """业务端地址配了没有。没配就连登录页都别装成能登。"""
        return bool(self.api.base_url)

    def restore(self) -> Identity | None:
        """重开客户端时接上上次那个会话。

        Token 不过期（契约 §2.2），所以正常情况下**不该让人每天重登一次**。它到底还算不算
        数，由第一次状态上报去问服务端（401 → 回登录页），这里不额外发一个探活请求。
        """
        session = self.store.load()
        if session is None or not session.usable:
            return None
        self._identity = Identity(
            email=session.email,
            user_name=session.user_name,
            agent_id=session.agent_id,
            agent_name=session.agent_name,
            session_id=session.session_id,
            tenant_name=session.tenant_name,
            token=session.token,
        )
        return self._identity

    # ── 登录 / 退出 ──────────────────────────────────────────────

    def remembered_login(self) -> tuple[str, str]:
        """记住的（邮箱，密码）。两个都有才够自动登录一次（`ui/login_page.py`）。

        为什么记了 Token 还要记密码：Token 会**没**——换了 Windows 账号、数据目录被清、
        文件损坏、自己点了退出登录。那几种情况下人没变、授权也没变，却要重打一遍密码。
        勾了「记住登录状态」的人要的就是别再打（用户 2026-08-15、2026-08-16）。
        ★ 但**被撤销**不算这种情况：`handle_revoked` 会把密码一起清掉，见下面。
        """
        return self.store.credentials()

    @property
    def autologin_allowed(self) -> bool:
        """记住的账号准不准**自动**登一次。

        自己点过退出登录就不准了（跨重启有效）——密码照样回填进框，但要他自己点「登录」。
        「不用再打密码」和「别替我做登录这个决定」是两件事，这里把它们分开。
        """
        return self.store.auto_login_allowed()

    def login(self, email: str, password: str, *, replace_existing: bool = False,
              remember: bool = True) -> Identity:
        """``remember=False`` 时**不把 Token 和密码落盘**：这台电脑一关，登录状态就没了。

        默认记住——招聘分身是常驻这台电脑替人干活的，每天开机重登一次纯属折腾。
        公用电脑上取消勾选，是唯一一个「不记住」还说得通的场景。
        """
        try:
            body = self.api.login(
                email, password,
                device_id=self.device.device_id,
                device_name=self.device.device_name,
                client_version=self.client_version,
                os_version=self.device.os_version,
                replace_existing=replace_existing,
            )
        except ApiError as exc:
            raise AuthError(str(exc), code=exc.code, details=exc.details) from exc

        token = str(body.get("accessToken") or "")
        if not token:
            raise AuthError("业务端没给 Token，登不进去")
        user = body.get("user") if isinstance(body.get("user"), dict) else {}
        agent = body.get("agent") if isinstance(body.get("agent"), dict) else {}
        activation = body.get("activationStatus")
        identity = Identity(
            email=email.strip().lower(),
            user_id=str(user.get("id") or ""),
            user_name=str(user.get("name") or ""),
            agent_id=str(agent.get("id") or ""),
            agent_name=str(agent.get("name") or ""),
            session_id=str(body.get("sessionId") or ""),
            token=token,
            stage=str(activation.get("stage") or "") if isinstance(activation, dict) else "",
        )
        if not identity.agent_id:
            # 没有分身就没有「用哪个身份记账」，往下跑只会记出一堆无主数据。
            raise AuthError("业务端没返回招聘分身信息，请联系管理员")
        self._identity = identity
        # 勾了就把「下次还认得这台机器」需要的东西一次记全：加密会话（Token+密码）
        # 和登录框里预填的邮箱。★ 这两件必须同进同退——分在两处写，「不记住」就会
        # 只忘一半（2026-08-16 复查：原来一半在这里、一半在登录页的 `_succeeded`）。
        if remember:
            remember_account(self.data_dir, identity.email)
            self.store.save(StoredSession(
                token=identity.token,
                session_id=identity.session_id,
                email=identity.email,
                user_name=identity.user_name,
                agent_id=identity.agent_id,
                agent_name=identity.agent_name,
                tenant_name=identity.tenant_name,
                password=password,
            ))
        else:
            # 上一次可能是勾着记住登的，这次明确说了不记住，就得把旧的清掉——
            # 否则「不记住」只对这次生效，下次打开还是自动进去了。
            self.store.clear()
            forget_account(self.data_dir)
        return identity

    def logout(self) -> None:
        """退出登录。**先把 Token 捏在手里，再作废本机会话，最后告诉服务端。**

        三步的顺序都有理由：

        * Token 先存进局部变量——本机清干净之后还得拿它去调撤销接口。
        * 本机**立刻**作废。Token 和会话必须当场没（`forget_session`），拖着的话登录页
          的自动登录就可能把人又登回去（2026-08-16 真机上的「第一次点退出登录又弹回
          主界面」正是这么来的）。
        * 撤销请求**一定要发**，而且发不出去也不能拦着人退出。漏发的后果是企业端一直
          显示这台机器在线，而它已经没有 Token 去纠正了——只能去企业端手工撤销那条会话。

        ★ 作废的是**会话**，不是记住的账号：勾了「记住登录状态」的话，邮箱密码留在
        DPAPI 那份密文里，下次登录页直接把密码回填进框（用户 2026-08-16）。别自己登回去
        这件事由 `forget_session()` 落的 ``auto_login=False`` 管，跨重启有效。
        被撤销走的是另一条路（`handle_revoked`），那条仍然全清。
        """
        token = self.token
        self._identity = None
        self.store.forget_session()
        if token:
            try:
                self.api.logout(token)
            except ApiError:
                pass  # 服务端说不通也得让人退出去；那边的会话可以在企业端手工撤销

    def handle_revoked(self) -> None:
        """任何一次调用发现会话被撤销时走这里：清干净 + 通知界面回登录页。

        ★ `store.clear()` 会把记住的密码一起清掉，这一点是**故意的**：撤销的理由可能是
        管理员在企业端手工撤、账号被停用、或者别的设备顶掉了这台。留着密码等于让这台
        机器转头自己登回来，契约 §2.2 的「立即失效」当场作废。撤销之后要回来，必须有人
        坐在这台电脑前重新输一次密码。
        """
        self._identity = None
        self.store.clear()
        if self._on_revoked is not None:
            self._on_revoked()

    def set_revoked_hook(self, hook: Callable[[], None] | None) -> None:
        self._on_revoked = hook


# ── 上次登录的邮箱（登录框里预填）──────────────────────────────────

UI_STATE_FILE = "console-ui.json"


def remember_account(data_dir: Path, email: str) -> None:
    """记住上次登录的邮箱，给登录框预填用。**这个文件里只有邮箱，没有凭证**——
    Token 和密码在 `token_store` 那份 DPAPI 密文里，明文文件一个字节都不许沾。

    由 `ClientAuth.login(remember=True)` 一处调用，和加密会话同进同退。
    """
    path = Path(data_dir) / UI_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_account": email.strip()}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass  # 记不住只是下次要重打一遍，不该让登录失败


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
