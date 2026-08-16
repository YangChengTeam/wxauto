"""客户端登录与状态上报（不含 GUI，到处都能跑）。

对的是 `docs/招聘分身_客户端认证与状态接口_v1.1.md`。这一层管的每件事都能「把人挡在门外」
或者「放错人进来」，所以全都要测：

① 登录请求的形状——字段名、`clientType`、邮箱大小写、`replaceExistingSession`；
② 错误码翻译——尤其「不区分邮箱不存在和密码错误」这条硬要求；
③ Token 的存法——非 Windows 一个字节都不落盘；
④ 会话被撤销（401）能不能被认出来并清干净；
⑤ 状态上报的两条规矩：相同状态不重复报、上报失败不能把状态「吞掉」。
"""

from __future__ import annotations

import json
import sys
import time
import tempfile
import unittest
import urllib.error
from unittest import mock
from io import BytesIO
from pathlib import Path
from typing import Any

from wxclient.ui import auth, client_api, device, token_store, uia_status
from wxclient.ui.client_api import ApiError, ClientApi
from wxclient.ui.status_reporter import StatusReporter
from wxclient.ui.token_store import (
    Protector,
    RefusingProtector,
    StoredSession,
    TokenStore,
)

LOGIN_OK = {
    "accessToken": "lxz_rc_token",
    "tokenType": "Bearer",
    "expiresIn": None,
    "expiresAt": None,
    "sessionId": "sess-1",
    "scope": ["recruitment_agent:client"],
    "clientType": "recruitment_agent_windows",
    "user": {"id": "u-1", "name": "陈敏", "email": "hr@example.com"},
    "agent": {"id": "agent-1", "name": "陈敏的招聘分身"},
    "activationStatus": {"stage": "waiting_wechat_uia_connection", "enabled": False},
}


class FakeApi:
    """记下每次调用的假 API。只实现被用到的那几个方法。"""

    def __init__(self, *, login_result: Any = None, error: Exception | None = None) -> None:
        self.base_url = "https://business.test"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._login_result = login_result if login_result is not None else dict(LOGIN_OK)
        self._error = error

    def login(self, email: str, password: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("login", {"email": email, "password": password, **kwargs}))
        if self._error is not None:
            raise self._error
        return self._login_result

    def logout(self, token: str) -> dict[str, Any]:
        self.calls.append(("logout", {"token": token}))
        return {"success": True}

    def put_client_status(self, token: str, status: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("client_status", {"token": token, "status": status, **kwargs}))
        if self._error is not None:
            raise self._error
        return {"accepted": True}

    def put_wechat_uia_status(self, token: str, status: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("wechat_status", {"token": token, "status": status, **kwargs}))
        if self._error is not None:
            raise self._error
        return {"accepted": True}


class MemoryProtector(Protector):
    """测试用：假装加密（倒序），好验证「写出去的不是明文」这件事本身。"""

    available = True
    name = "test"

    def protect(self, plain: str) -> str:
        return plain[::-1]

    def unprotect(self, blob: str) -> str:
        return blob[::-1]


def http_error(status: int, code: str, message: str = "x",
               details: dict[str, Any] | None = None) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"code": code, "message": message,
                                 "details": details or {}}}).encode("utf-8")
    return urllib.error.HTTPError("https://business.test/x", status, message, {},  # type: ignore[arg-type]
                                  BytesIO(body))


class LoginRequestTests(unittest.TestCase):
    """请求体的形状（契约 §5.1）。字段名错一个，后端就只会回 400。"""

    def setUp(self) -> None:
        self.sent: dict[str, Any] = {}
        self.api = ClientApi("https://business.test")
        self.api._request = self._capture  # type: ignore[method-assign]

    def _capture(self, method: str, path: str, *, payload: Any = None,
                 token: str = "") -> dict[str, Any]:
        self.sent = {"method": method, "path": path, "payload": payload, "token": token}
        return dict(LOGIN_OK)

    def test_the_login_body_matches_the_contract(self) -> None:
        self.api.login("  HR@Example.COM ", "pw", device_id="dev-1",
                       device_name="陈敏的办公电脑", client_version="1.0.0",
                       os_version="Windows 11")

        self.assertEqual(self.sent["method"], "POST")
        self.assertEqual(self.sent["path"], "/recruitment-agent/client/auth/login")
        payload = self.sent["payload"]
        self.assertEqual(payload["email"], "hr@example.com", "★ 去空格 + 转小写后再匹配账号")
        self.assertEqual(payload["clientType"], "recruitment_agent_windows")
        self.assertEqual(payload["deviceId"], "dev-1")
        self.assertEqual(payload["clientVersion"], "1.0.0")
        self.assertIs(payload["replaceExistingSession"], False, "默认不顶掉别人的设备")

    def test_replacing_another_device_is_explicit(self) -> None:
        self.api.login("hr@example.com", "pw", device_id="d", device_name="n",
                       client_version="1.0.0", replace_existing=True)

        self.assertIs(self.sent["payload"]["replaceExistingSession"], True)

    def test_status_reports_carry_an_idempotency_key(self) -> None:
        """★ eventId 是幂等键（契约 §7）：断线重发同一条，服务端不该记两遍。"""
        self.api.put_client_status("tok", "offline", client_version="1.0.0",
                                   reason="client_shutdown")

        payload = self.sent["payload"]
        self.assertEqual(self.sent["method"], "PUT")
        self.assertTrue(payload["eventId"])
        self.assertTrue(payload["occurredAt"].endswith("Z"), "时间是 UTC ISO-8601")
        self.assertEqual(self.sent["token"], "tok")

    def test_the_wechat_report_uses_the_contract_field_names(self) -> None:
        """★ 字段名错一个（capability_status vs capabilityStatus）后端就只当没收到，
        而这一格恰恰决定企业端显不显示「运行中」。"""
        self.api.put_wechat_uia_status("tok", "connected",
                                       instance={"processVersion": "4.0.1.15"},
                                       capability_status="passed")

        payload = self.sent["payload"]
        self.assertEqual(self.sent["path"], "/recruitment-agent/client/wechat-uia/status")
        self.assertEqual(payload["capabilityStatus"], "passed")
        self.assertEqual(payload["wechatInstance"], {"processVersion": "4.0.1.15"})
        self.assertNotIn("capability_status", payload)

    def test_an_unchecked_connection_omits_the_capability_field(self) -> None:
        """没算出能力档时**不发这个字段**（契约 §8：省略 = 服务端进 checking），
        不许塞一个空串或者瞎猜一个 passed。"""
        self.api.put_wechat_uia_status("tok", "connected")

        self.assertNotIn("capabilityStatus", self.sent["payload"])



class ErrorTranslationTests(unittest.TestCase):
    def test_wrong_password_and_unknown_email_say_the_same_thing(self) -> None:
        """★ 契约 §3.4 硬要求：两者说同一句话。分开说，这个接口就成了「查这个邮箱在不在册」。"""
        message = client_api.MESSAGES["INVALID_CREDENTIALS"]

        self.assertEqual(message, "邮箱或密码错误")
        self.assertNotIn("不存在", message)

    def test_a_revoked_session_is_recognisable(self) -> None:
        error = client_api._from_http_error(http_error(401, "CLIENT_SESSION_REVOKED"))

        self.assertTrue(error.revoked, "认不出来就回不到登录页，等于一把撤不掉的钥匙")
        self.assertEqual(str(error), "客户端登录已失效，请重新登录")

    def test_another_device_conflict_keeps_the_details(self) -> None:
        error = client_api._from_http_error(http_error(
            409, "ACTIVE_CLIENT_SESSION_EXISTS", details={"deviceName": "办公电脑",
                                                          "canReplace": True}))

        self.assertTrue(error.session_exists)
        self.assertEqual(error.details["deviceName"], "办公电脑",
                         "要把「是哪台机器」摆给人看，才谈得上让他决定顶不顶")

    def test_an_unknown_code_still_says_something_usable(self) -> None:
        """认不出来的错也得给人一句话——先用服务端那句，实在没有才退回状态码。"""
        self.assertEqual(str(client_api._from_http_error(http_error(500, "", "服务器开小差"))),
                         "服务器开小差")

        blank = urllib.error.HTTPError("https://business.test/x", 503, "", {},  # type: ignore[arg-type]
                                       BytesIO(b""))
        self.assertIn("503", str(client_api._from_http_error(blank)))


class TokenStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def test_a_machine_without_dpapi_writes_nothing_to_disk(self) -> None:
        """★ 契约 §2.2：Token 不设过期时间，落一份明文在磁盘上等于把分身送人。

        非 Windows（开发机 / CI）没有 DPAPI，那就**一个字节都不写**——宁可重开要重登。
        """
        store = TokenStore(self.dir)  # 默认 protector：Linux 上是 RefusingProtector
        store.save(StoredSession(token="secret-token", agent_id="agent-1"))

        if sys.platform != "win32":
            self.assertFalse(store.path.exists(), "非 Windows 上不该留下任何文件")
            self.assertFalse(store.persistent)
            self.assertEqual(TokenStore(self.dir).load(), None, "换个进程就什么都没有")

    def test_the_token_is_never_written_in_the_clear(self) -> None:
        store = TokenStore(self.dir, MemoryProtector())
        store.save(StoredSession(token="secret-token", agent_id="agent-1", email="hr@x.com"))

        raw = store.path.read_text(encoding="utf-8")

        self.assertNotIn("secret-token", raw)
        self.assertIn("agent-1", raw, "不是凭证的那些照常明文，重开时要靠它认人")

    def test_a_saved_session_comes_back_and_clears_completely(self) -> None:
        TokenStore(self.dir, MemoryProtector()).save(
            StoredSession(token="tok", agent_id="agent-1", user_name="陈敏"))

        restored = TokenStore(self.dir, MemoryProtector()).load()
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.token, "tok")
        self.assertEqual(restored.user_name, "陈敏")

        store = TokenStore(self.dir, MemoryProtector())
        store.clear()
        self.assertFalse(store.path.exists())
        self.assertIsNone(TokenStore(self.dir, MemoryProtector()).load())

    def test_a_corrupt_file_is_treated_as_not_logged_in(self) -> None:
        """★ 换了 Windows 账号就解不开这份密文。那时要退回登录页，不是崩在启动路上。"""
        (self.dir / token_store.FILE_NAME).write_text("{ 坏掉的", encoding="utf-8")

        self.assertIsNone(TokenStore(self.dir, MemoryProtector()).load())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI 只有 Windows 上有")
    def test_dpapi_round_trip_on_the_real_machine(self) -> None:
        store = TokenStore(self.dir)
        store.save(StoredSession(token="lxz_rc_real", agent_id="agent-1"))

        raw = store.path.read_text(encoding="utf-8")
        self.assertNotIn("lxz_rc_real", raw)
        restored = TokenStore(self.dir).load()
        assert restored is not None
        self.assertEqual(restored.token, "lxz_rc_real")


class DeviceIdentityTests(unittest.TestCase):
    def test_the_device_id_is_random_and_stable(self) -> None:
        """★ 契约 §5.1：20–128 位 URL-safe 随机串，**不许**用 MAC / 硬盘序列号。"""
        data_dir = Path(tempfile.mkdtemp())

        first = device.load(data_dir)
        second = device.load(data_dir)

        self.assertEqual(first.device_id, second.device_id, "同一台机器每次都是同一个")
        self.assertTrue(20 <= len(first.device_id) <= 128)
        self.assertTrue(first.device_name)
        other = device.load(Path(tempfile.mkdtemp()))
        self.assertNotEqual(first.device_id, other.device_id, "不同机器不能撞")


class ClientAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.api = FakeApi()
        self.store = TokenStore(self.dir, MemoryProtector())
        self.auth = auth.ClientAuth("https://business.test", self.dir,
                                    client_version="1.0.0", api=self.api, store=self.store)

    def test_not_remembering_leaves_nothing_on_disk(self) -> None:
        """★ 「记住登录状态」没勾上，就**一个字节都不落盘**——公用电脑上这是唯一说得通的用法。

        还得把上一次记住的清掉：不然「不记住」只对这一次生效，下次打开照样自动进去。
        """
        self.auth.login("hr@example.com", "pw")
        self.assertIsNotNone(self.store.load(), "先记一次")

        self.auth.login("hr@example.com", "pw", remember=False)

        self.assertIsNone(self.store.load(), "不记住就得把旧的也清干净")

    def test_remembering_is_the_default(self) -> None:
        """招聘分身常驻这台电脑替人干活，每天开机重登一次纯属折腾。"""
        self.auth.login("hr@example.com", "pw")

        # 重开客户端 = 新的 ClientAuth，读同一份落盘会话
        again = auth.ClientAuth("https://business.test", self.dir,
                                client_version="1.0.0", api=FakeApi(), store=self.store)
        restored = again.restore()

        assert restored is not None
        self.assertEqual(restored.email, "hr@example.com")

    def test_a_successful_login_is_remembered_for_next_time(self) -> None:
        identity = self.auth.login("hr@example.com", "pw")

        self.assertEqual(identity.agent_id, "agent-1")
        self.assertEqual(identity.account, "agent-1",
                         "★ 网关用它记账——不再手抄微信号")
        self.assertEqual(identity.title, "陈敏")

        again = auth.ClientAuth("https://business.test", self.dir, api=FakeApi(),
                                store=TokenStore(self.dir, MemoryProtector())).restore()
        assert again is not None
        self.assertEqual(again.token, "lxz_rc_token", "重开客户端不该让人再登一次")

    def test_a_login_without_an_agent_is_refused(self) -> None:
        """★ 没有分身就没有「用谁的身份记账」，让它进去只会记出一堆无主数据。"""
        payload = dict(LOGIN_OK)
        payload["agent"] = {}
        self.auth.api = FakeApi(login_result=payload)  # type: ignore[assignment]

        with self.assertRaises(auth.AuthError):
            self.auth.login("hr@example.com", "pw")

    def test_another_device_conflict_is_handed_back_for_a_human_to_decide(self) -> None:
        self.auth.api = FakeApi(error=ApiError(  # type: ignore[assignment]
            "这个招聘分身已在另一台电脑登录", code="ACTIVE_CLIENT_SESSION_EXISTS",
            status=409, details={"deviceName": "办公电脑"}))

        with self.assertRaises(auth.AuthError) as caught:
            self.auth.login("hr@example.com", "pw")

        self.assertTrue(caught.exception.needs_replace_confirm,
                        "★ 顶掉的是同事正在用的机器，客户端不能自己决定")
        self.assertEqual(caught.exception.details["deviceName"], "办公电脑")

    def test_logging_out_tells_the_server_first_then_forgets_locally(self) -> None:
        self.auth.login("hr@example.com", "pw")

        self.auth.logout()

        self.assertIn("logout", [name for name, _ in self.api.calls],
                      "★ 只清本机不告诉服务端，企业端会一直显示这台机器在线")
        self.assertIsNone(self.auth.identity)
        self.assertIsNone(TokenStore(self.dir, MemoryProtector()).load())

    def test_a_revoked_session_clears_the_token_and_calls_back(self) -> None:
        self.auth.login("hr@example.com", "pw")
        seen: list[str] = []
        self.auth.set_revoked_hook(lambda: seen.append("回登录页"))

        self.auth.handle_revoked()

        self.assertEqual(seen, ["回登录页"])
        self.assertEqual(self.auth.token, "")
        self.assertIsNone(TokenStore(self.dir, MemoryProtector()).load())

    def test_remembering_only_ever_stores_the_email(self) -> None:
        auth.remember_account(self.dir, "hr@example.com")

        raw = (self.dir / auth.UI_STATE_FILE).read_text(encoding="utf-8")

        self.assertIn("hr@example.com", raw)
        self.assertEqual(auth.last_account(self.dir), "hr@example.com")
        self.assertNotIn("password", raw,
                         "★ 这是**明文** JSON。记住密码走 DPAPI 密文（token_store），"
                         "永远不许落到这里")

    # ── 记住登录状态 = 连密码一起记（用户 2026-08-15）──────────────

    def test_remembering_keeps_the_password_for_a_silent_relogin(self) -> None:
        """Token 会没（换 Windows 账号、清数据目录、重装），人却没变。

        勾了「记住登录状态」的人要的就是那时候也别再打一遍密码。
        """
        self.auth.login("hr@example.com", "pw-secret")

        self.assertEqual(self.store.credentials(), ("hr@example.com", "pw-secret"))

    def test_the_password_is_never_written_in_the_clear(self) -> None:
        """★ 落盘的必须是密文。明文的话，拿到这个文件 = 拿到这个 HR 的整个企业端账号
        （工资、合同、人才库），比拿到只开得了客户端接口的 Token 严重得多。"""
        self.auth.login("hr@example.com", "pw-secret")

        raw = (self.dir / "client-session.json").read_text(encoding="utf-8")

        self.assertNotIn("pw-secret", raw)
        self.assertIn("pw-secret"[::-1], raw, "MemoryProtector 是倒序『加密』")

    def test_not_remembering_forgets_the_password_too(self) -> None:
        self.auth.login("hr@example.com", "pw-secret")

        self.auth.login("hr@example.com", "pw-secret", remember=False)

        self.assertEqual(self.store.credentials(), ("", ""))

    def test_logging_out_forgets_the_password(self) -> None:
        """人自己点了退出——他就是想下来，别替他记着怎么再上去。"""
        self.auth.login("hr@example.com", "pw-secret")

        self.auth.logout()

        self.assertEqual(TokenStore(self.dir, MemoryProtector()).credentials(), ("", ""))

    def test_a_revoked_session_forgets_the_password_as_well(self) -> None:
        """★ 撤销的理由可能是管理员手工撤、账号停用、别的设备顶掉了这台。

        留着密码 = 这台机器转头自己登回来，契约 §2.2 的「立即失效」当场作废。
        """
        self.auth.login("hr@example.com", "pw-secret")

        self.auth.handle_revoked()

        self.assertEqual(TokenStore(self.dir, MemoryProtector()).credentials(), ("", ""),
                         "撤销之后必须有人坐在这台电脑前重新输一次密码")

    def test_credentials_survive_a_token_that_cannot_be_read(self) -> None:
        """自动重登要用的正是这种情况：Token 解不开（换了 Windows 账号），但人没换。"""
        self.auth.login("hr@example.com", "pw-secret")
        path = self.dir / "client-session.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["token_protected"] = ""          # 解不开的 Token
        path.write_text(json.dumps(raw), encoding="utf-8")

        store = TokenStore(self.dir, MemoryProtector())

        self.assertIsNone(store.load(), "Token 没了")
        self.assertEqual(store.credentials(), ("hr@example.com", "pw-secret"),
                         "但邮箱密码还在，够自动登一次")

    def test_a_machine_that_cannot_encrypt_stores_no_password(self) -> None:
        """加密不了的机器（非 Windows 的开发机 / CI）：一个字节都不落盘，密码也一样。

        ★ 必须**显式**用 RefusingProtector，不能靠「默认 protector」——真机上默认的就是
        DPAPI，那样这条断言在 Windows 上必然翻车（2026-08-15 就是这么红的）。
        """
        refusing = TokenStore(self.dir, RefusingProtector())
        client = auth.ClientAuth("https://business.test", self.dir, api=FakeApi(),
                                 store=refusing)

        client.login("hr@example.com", "pw-secret")

        self.assertEqual(TokenStore(self.dir, RefusingProtector()).credentials(), ("", ""))
        self.assertFalse((self.dir / "client-session.json").exists())


class UiaStatusTests(unittest.TestCase):
    """探测结果 → 契约 §8 的状态。报错方向错了，人就会去做无用功。"""

    def test_no_wechat_at_all_says_open_wechat(self) -> None:
        state = uia_status.classify({"platform": "Windows", "window_count": 0})
        assert state is not None
        self.assertEqual(state.status, uia_status.FAILED)
        self.assertEqual(state.failure_code, uia_status.PROCESS_NOT_FOUND)
        self.assertIn("打开", state.failure["message"])

    def test_a_login_window_says_log_into_wechat(self) -> None:
        """★ 「微信没开」和「微信开着但没登录」是两句不同的话。"""
        state = uia_status.classify({"platform": "Windows", "login_window": True,
                                     "logged_in": False, "window_count": 1})
        assert state is not None
        self.assertEqual(state.failure_code, uia_status.NOT_LOGGED_IN)

    def test_logged_in_but_not_exposed_is_its_own_failure(self) -> None:
        state = uia_status.classify({"platform": "Windows", "logged_in": True,
                                     "uia_exposed": False, "window_count": 1})
        assert state is not None
        self.assertEqual(state.failure_code, uia_status.WINDOW_UNAVAILABLE)

    CONNECTED = {"platform": "Windows", "logged_in": True, "uia_exposed": True,
                 "selected_window": {"process_version": "4.0.1.15"}}

    def setUp(self) -> None:
        # 注册在最前 = 跑在最后（addCleanup 是后进先出）：等 `_with_account` 的补丁
        # 撤掉、`_account` 变回那个带 lru_cache 的真函数，再去清缓存。
        self.addCleanup(uia_status.forget_account)

    def _with_account(self, account: Any) -> None:
        """把「当前微信账号」按住，别让单测结果取决于跑测试这台机器上开没开微信。

        ★ 按住的是 `_account` 这个**查询本身**，不是某个模块全局。2026-08-16 账号缓存
        改成「按微信进程号失效」（`lru_cache`）之后，往模块里写一个全局等于什么都没按住
        ——真机上跑测试时它会照样去枚举进程，于是「读不到账号」这条用例拿到的是本机
        真微信的账号，测试通过与否取决于跑测试的人开没开微信。
        """
        patcher = mock.patch.object(uia_status, "_account", lambda _pids: account)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_real_account_is_reported_as_an_opaque_ref(self) -> None:
        from wxclient.uia_probe.account import WeChatAccount

        self._with_account(WeChatAccount("wxid_6shq4q0wu92m12"))
        state = uia_status.classify(self.CONNECTED)
        assert state is not None and state.instance is not None

        self.assertEqual(state.status, uia_status.CONNECTED)
        self.assertIsNone(state.failure)
        self.assertEqual(state.instance["processVersion"], "4.0.1.15")
        self.assertNotIn("6shq4q0wu92m12", state.instance["accountRef"], "ref 必须不透明")
        self.assertIn("****", state.instance["accountMask"])
        self.assertEqual(state.instance["nickname"], uia_status.UNKNOWN_NICKNAME,
                         "★ 昵称只在微信自己的加密库里，读不到就填占位符 `-`（用户裁决）——"
                         "服务端要求必填，但绝不拿分身名之类的东西冒充微信昵称，"
                         "那会让企业端显示一个看着像真的、其实是假的微信身份")

    def test_a_wechat_relogin_makes_us_re_read_the_account(self) -> None:
        """★ 微信退出登录、换个号再进来 → accountRef 必须跟着换。

        账号缓存原来是「查一次就再也不查」的模块全局：换号之后这台机器还在报上一个号的
        ref，而服务端「一个微信只能被一个分身占用」正是按它判的——被占住的会是一个早就
        不在这台电脑上的账号。现在缓存的键是微信进程号，换号自然重认一次。

        同时守住另一头：**进程没变就别反复认**。枚举进程打开的文件在真机上要 2 秒多，
        而探测是 15 秒一拍。
        """
        from wxclient.uia_probe.account import WeChatAccount

        uia_status.forget_account()
        seen: list[str] = []

        def probe() -> Any:
            seen.append("call")
            return WeChatAccount(f"wxid_{len(seen)}")

        def ref_for(pid: int) -> str:
            probe_result = {**self.CONNECTED,
                            "selected_window": {"process_version": "4.0.1.15",
                                                "process_id": pid}}
            state = uia_status.classify(probe_result)
            assert state is not None and state.instance is not None
            return str(state.instance["accountRef"])

        with mock.patch("wxclient.uia_probe.account.probe_account", probe):
            before = ref_for(100)
            self.assertEqual(ref_for(100), before, "同一批进程应当命中缓存")
            self.assertEqual(len(seen), 1, "同一批进程不该反复枚举")
            after = ref_for(200)

        self.assertEqual(len(seen), 2, "进程换了就该重认一次")
        self.assertNotEqual(before, after)

    def test_an_unknown_account_is_left_out_not_made_up(self) -> None:
        """★ 读不到微信账号就别编一个「稳定标识」，那会让服务端的占用检查形同虚设。"""
        self._with_account(None)
        state = uia_status.classify(self.CONNECTED)
        assert state is not None

        self.assertIsNone(state.instance)

    def test_an_unreadable_version_does_not_sink_the_whole_report(self) -> None:
        """★ 2026-08-15 真机：探测结果的对外投影漏了 process_version，于是
        `wechatInstance.processVersion` 是 undefined，服务端每次退回 400——
        微信明明连着，企业端却永远进不了「运行中」。

        版本号读不到时填 `-`：服务端要求必填，少一个字段整条 connected 就白发。
        """
        from wxclient.uia_probe.account import WeChatAccount

        self._with_account(WeChatAccount("wxid_aaa"))
        state = uia_status.classify({"platform": "Windows", "logged_in": True,
                                     "uia_exposed": True, "selected_window": {}})
        assert state is not None and state.instance is not None

        self.assertEqual(state.instance["processVersion"], uia_status.UNKNOWN_NICKNAME)
        self.assertIn("accountRef", state.instance)

    def test_the_probe_hands_over_the_wechat_version(self) -> None:
        """★ 上报层只看得见探测结果**对外那份字典**里的键。版本号加在候选窗口上、
        漏在投影里，正是那次 400 的真因——这条盯住这一层的交接。
        """
        if sys.platform != "win32":
            self.skipTest("只在 Windows 上跑：探测要 pywinauto")
        from wxclient.uia_probe import windows as win_probe

        candidate = {"handle": 1, "process_version": "4.1.12.26", "score": 9,
                     "process_name": "Weixin.exe", "title": "微信"}
        real_find = win_probe._find_wechat_window_candidates
        real_count = win_probe._count_descendants
        win_probe._find_wechat_window_candidates = lambda: [candidate]
        win_probe._count_descendants = lambda *a, **k: 61
        try:
            found = win_probe.find_wechat_windows(max_depth=2, max_nodes=10)
        finally:
            win_probe._find_wechat_window_candidates = real_find
            win_probe._count_descendants = real_count

        self.assertEqual(found[0]["process_version"], "4.1.12.26")

    def test_a_non_windows_machine_reports_nothing(self) -> None:
        """★ 在 Linux 上报「微信没找到」，等于往企业端灌一条不存在的事实。"""
        self.assertIsNone(uia_status.classify({"platform": "Linux"}))

    def test_losing_a_live_connection_is_disconnected_not_failed(self) -> None:
        connected = uia_status.UiaState(uia_status.CONNECTED)
        now = uia_status.UiaState(uia_status.FAILED, uia_status.PROCESS_NOT_FOUND)

        moved = uia_status.transition(connected, now)

        self.assertEqual(moved.status, uia_status.DISCONNECTED)
        self.assertEqual(moved.failure_code, uia_status.CONNECTION_LOST)


class StatusReporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.reporter = StatusReporter(self.api, lambda: "tok", client_version="1.0.0")
        self.addCleanup(self.reporter.stop)

    def _drain(self, reporter: StatusReporter | None = None) -> None:
        """等上报线程把队列跑空。上报是异步的，断言之前必须等它落地。"""
        target = reporter or self.reporter
        for _ in range(200):
            if target._queue.empty():
                break
            time.sleep(0.01)
        time.sleep(0.05)  # 队列空了不代表最后一条跑完了

    def test_the_same_state_is_not_reported_twice(self) -> None:
        """★ 微信探测 15 秒一拍。照拍上报 = 每天几千条毫无信息量的审计（契约 §8）。"""
        probe = {"platform": "Windows", "window_count": 0}

        self.reporter.wechat_probe(probe)
        self.reporter.wechat_probe(probe)
        self._drain()

        posts = [call for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["status"], uia_status.FAILED)
        self.assertEqual(posts[0]["failure"]["code"], uia_status.PROCESS_NOT_FOUND)

    def test_a_changed_reason_is_reported_again(self) -> None:
        self.reporter.wechat_probe({"platform": "Windows", "window_count": 0})
        self.reporter.wechat_probe({"platform": "Windows", "login_window": True,
                                    "window_count": 1})
        self._drain()

        codes = [call["failure"]["code"] for name, call in self.api.calls
                 if name == "wechat_status"]
        self.assertEqual(codes, [uia_status.PROCESS_NOT_FOUND, uia_status.NOT_LOGGED_IN],
                         "原因换了就得说——它对应的是两句不同的恢复引导")

    def test_a_failed_report_does_not_get_swallowed_forever(self) -> None:
        """★ 上报失败还记着「报过了」，这个状态就**再也**报不出去了：
        去重表说报过，服务端那边压根没收到。
        """
        self.api._error = ApiError("连不上业务端")

        self.reporter.client_error("runtime_error")
        self._drain()
        self.api._error = None
        self.reporter.client_error("runtime_error")
        self._drain()

        reports = [call for name, call in self.api.calls if name == "client_status"]
        self.assertEqual(len(reports), 2, "第一次失败了，第二次还得再试")

    def test_a_revoked_session_during_a_report_is_escalated(self) -> None:
        seen: list[str] = []
        reporter = StatusReporter(FakeApi(error=ApiError("失效", code="CLIENT_SESSION_REVOKED",
                                                         status=401)),
                                  lambda: "tok", client_version="1.0.0",
                                  on_revoked=lambda: seen.append("清 token"))
        self.addCleanup(reporter.stop)

        reporter.client_error("runtime_error")
        for _ in range(200):
            if seen:
                break
            time.sleep(0.01)

        self.assertEqual(seen, ["清 token"])

    def test_quitting_reports_offline_before_the_process_goes_away(self) -> None:
        """★ 契约 §7：没有心跳，服务端不会自己把 online 改成 offline。

        退出前这一下必须**同步**发出去——异步丢队列的话，进程先没了，请求也就没了，
        企业端会一直显示这台早就关机的电脑在线。
        """
        self.reporter.shutdown()

        offline = [call for name, call in self.api.calls
                   if name == "client_status" and call["status"] == "offline"]
        self.assertEqual(len(offline), 1)
        self.assertEqual(offline[0]["reason"], "client_shutdown")

    CONNECTED_PROBE = {"platform": "Windows", "uia_exposed": True,
                       "selected_window": {"process_version": "4.0.1.15"}}

    def test_connecting_also_says_what_this_machine_can_do(self) -> None:
        """契约 §8：只有 ``capabilityStatus=passed`` 才让企业端进入「运行中」。

        连上微信 ≠ 能干活（只走流程不真发、发不了文件都算连上了），所以这一档必须
        跟着 connected 一起报——不然企业端会把一台什么都发不出去的机器显示成运行中。
        """
        reporter = StatusReporter(self.api, lambda: "tok", client_version="1.0.0",
                                  capability_check=lambda probe: "passed")
        self.addCleanup(reporter.stop)

        reporter.wechat_probe(self.CONNECTED_PROBE)
        self._drain(reporter)

        posts = [call for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["status"], uia_status.CONNECTED)
        self.assertEqual(posts[0]["capability_status"], "passed")

    def test_a_changed_capability_is_reported_again(self) -> None:
        """能力档变了要再说一次：微信没动，但「能不能真发」变了，企业端得知道。"""
        answers = iter(["limited", "limited", "passed"])
        reporter = StatusReporter(self.api, lambda: "tok", client_version="1.0.0",
                                  capability_check=lambda probe: next(answers))
        self.addCleanup(reporter.stop)

        for _ in range(3):
            reporter.wechat_probe(self.CONNECTED_PROBE)
        self._drain(reporter)

        levels = [call["capability_status"] for name, call in self.api.calls
                  if name == "wechat_status"]
        self.assertEqual(levels, ["limited", "passed"], "相同的档不重复报，变了才报")

    def test_a_broken_capability_check_still_reports_the_connection(self) -> None:
        """算不出能力表不该把「微信连上了」这条也一起吞掉。"""
        def boom(probe: dict[str, Any]) -> str:
            raise RuntimeError("探测炸了")

        reporter = StatusReporter(self.api, lambda: "tok", client_version="1.0.0",
                                  capability_check=boom)
        self.addCleanup(reporter.stop)

        reporter.wechat_probe(self.CONNECTED_PROBE)
        self._drain(reporter)

        posts = [call for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual([call["status"] for call in posts], [uia_status.CONNECTED])
        self.assertEqual(posts[0]["capability_status"], "")

    def test_a_rejected_payload_is_not_hammered_every_tick(self) -> None:
        """★ 2026-08-15 真机：服务端说「内容不合格」，我们每 15 秒把同一份内容再发一遍，
        日志刷了几百条一模一样的告警，真正的原因（少了哪个字段）反而被埋掉。

        内容不合格重发多少次都是同一个结果——**不回滚去重记录**，等内容变了再说。
        """
        self.api._error = ApiError("填的内容格式不对", code="VALIDATION_ERROR", status=400)
        probe = {"platform": "Windows", "window_count": 0}

        for _ in range(4):
            self.reporter.wechat_probe(probe)
        self._drain()

        posts = [call for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(posts), 1, "同一份不合格的内容只发一次")

    def test_a_retry_reuses_the_same_idempotency_key(self) -> None:
        """★ 契约 §12.3：eventId 是幂等键。重发**必须**沿用同一个。

        每次重发都现生成一个新 UUID 的话，这个键就白设了：请求其实送到了、回包在路上
        丢了，我们判成失败重发一次，服务端看到的是两个不同的事件 —— 同一件事记两遍审计。
        """
        self.api._error = ApiError("连不上业务端")
        probe = {"platform": "Windows", "window_count": 0}

        self.reporter.wechat_probe(probe)
        self._drain()
        self.api._error = None
        self.reporter.wechat_probe(probe)
        self._drain()

        keys = [call["event_id"] for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1], "同一条状态重试，键不能换")

    def test_a_different_state_gets_a_new_idempotency_key(self) -> None:
        """状态变了就是另一件事，得用新键——否则服务端会以 409 EVENT_ID_CONFLICT 拒掉。"""
        self.reporter.wechat_probe({"platform": "Windows", "window_count": 0})
        self._drain()
        self.reporter.wechat_probe({"platform": "Windows", "login_window": True,
                                    "window_count": 1})
        self._drain()

        keys = [call["event_id"] for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_a_new_session_throws_the_old_keys_away(self) -> None:
        """换了会话，上一个会话没送出去的键就别再用了（服务端全局唯一，会 409）。"""
        self.api._error = ApiError("连不上业务端")
        self.reporter.client_error("runtime_error")
        self._drain()

        self.reporter.session_changed()
        self.api._error = None
        self.reporter.client_error("runtime_error")
        self._drain()

        keys = [call["event_id"] for name, call in self.api.calls if name == "client_status"]
        self.assertNotEqual(keys[0], keys[1])

    def test_a_permanent_conflict_is_not_hammered_every_tick(self) -> None:
        """★ 409 也是「重发也不会变」：微信已被别的分身占着（契约表里 retryable=false），
        或者幂等键撞了。当成网络抖动去重试，就是每 15 秒刷一条一样的告警。
        """
        self.api._error = ApiError("这个微信已被别的分身占着",
                                   code="WECHAT_UIA_ACCOUNT_IN_USE", status=409)
        probe = {"platform": "Windows", "window_count": 0}

        for _ in range(4):
            self.reporter.wechat_probe(probe)
        self._drain()

        posts = [call for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(posts), 1)

    def test_a_transient_failure_is_still_retried(self) -> None:
        """网络抖一下和内容不合格是两回事：前者必须重来，否则这个状态永远报不出去。"""
        self.api._error = ApiError("连不上业务端")
        probe = {"platform": "Windows", "window_count": 0}

        self.reporter.wechat_probe(probe)
        self._drain()
        self.reporter.wechat_probe(probe)
        self._drain()

        posts = [call for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(posts), 2)

    def test_signing_out_says_goodbye_before_the_token_dies(self) -> None:
        """★ 2026-08-15 真机：退出登录时先撤销会话、后发 offline，那条上报吃 401，
        界面于是弹一句「客户端会话已失效」——自己撤销了自己，看着像出事。

        `shutdown()` 是**同步**的：它返回时 offline 已经送到，之后再撤销才安全。
        """
        self.reporter.shutdown("user_logout")

        offline = [call for name, call in self.api.calls
                   if name == "client_status" and call["status"] == "offline"]
        self.assertEqual(len(offline), 1, "返回时就该已经送到，不是丢进队列等着")
        self.assertEqual(offline[0]["reason"], "user_logout")

    def test_a_new_session_starts_with_a_clean_slate(self) -> None:
        """换了会话，去重记录必须清——不然重新登录后微信状态没变，
        我们以为「报过了」，而新会话那边一直是空的。"""
        probe = {"platform": "Windows", "window_count": 0}
        self.reporter.wechat_probe(probe)
        self._drain()

        self.reporter.session_changed()
        self.reporter.wechat_probe(probe)
        self._drain()

        posts = [call for name, call in self.api.calls if name == "wechat_status"]
        self.assertEqual(len(posts), 2)

    def test_nothing_is_even_computed_before_login(self) -> None:
        """★ 真机：人还停在登录页，界面却卡了 795ms。

        没登录时这一拍的结论根本没人要，可我们照样去认微信账号（枚举进程打开的文件，
        真机 2.4 秒）、照样把网关那套重型模块 import 进来算能力表。它们在后台线程上跑，
        GIL 一样是从界面线程手里抢的。**没登录就一件都别干。**
        """
        checked: list[str] = []

        def check(probe: dict[str, Any]) -> str:
            checked.append("算了")
            return "passed"

        reporter = StatusReporter(self.api, lambda: "", client_version="1.0.0",
                                  capability_check=check)
        self.addCleanup(reporter.stop)

        reporter.wechat_probe({"platform": "Windows", "uia_exposed": True,
                               "selected_window": {"process_version": "4.1.12.26"}})
        self._drain(reporter)

        self.assertEqual(checked, [], "没登录就别算能力表")
        self.assertEqual(self.api.calls, [])

    def test_nothing_is_reported_before_login(self) -> None:
        """没 Token 的时候（还没登录）不该往服务端打——那些请求只会得到 401。"""
        reporter = StatusReporter(self.api, lambda: "", client_version="1.0.0")
        self.addCleanup(reporter.stop)

        reporter.client_online()
        reporter.wechat_probe({"platform": "Windows", "window_count": 0})
        self._drain()

        self.assertEqual(self.api.calls, [])


class WeChatAccountTests(unittest.TestCase):
    """契约 §8 的 `accountRef`：服务端拿它判「一个微信被两个分身占用」。"""

    def _account(self, raw: str) -> Any:
        from wxclient.uia_probe.account import WeChatAccount

        return WeChatAccount(raw)

    def test_the_same_wechat_gets_the_same_ref_on_any_machine(self) -> None:
        """★ 目录名尾巴上的 `_c174` 是这台电脑这次安装的编号，不是账号的一部分。

        留着它的话，同一个微信在两台电脑上算出两个 ref，服务端那条占用检查永远不会触发。
        """
        from wxclient.uia_probe.account import _INSTALL_SUFFIX

        a = self._account(_INSTALL_SUFFIX.sub("", "wxid_6shq4q0wu92m12_c174"))
        b = self._account(_INSTALL_SUFFIX.sub("", "wxid_6shq4q0wu92m12_c9"))
        self.assertEqual(a.account_ref, b.account_ref)

    def test_different_accounts_get_different_refs(self) -> None:
        self.assertNotEqual(self._account("wxid_aaa").account_ref,
                            self._account("wxid_bbb").account_ref)

    def test_the_ref_does_not_leak_the_wxid(self) -> None:
        """不透明是契约明写的：ref 里不许出现原文。"""
        account = self._account("wxid_6shq4q0wu92m12")
        self.assertNotIn("6shq4q0wu92m12", account.account_ref)
        self.assertNotIn("6shq4q0wu92m12", account.account_mask)
        self.assertIn("****", account.account_mask)

    def test_two_wechats_on_one_machine_means_we_do_not_guess(self) -> None:
        """★ 认不出该用哪个就不给——给错了等于把另一个人的微信记到这个分身头上。"""
        import wxclient.uia_probe.account as mod

        class FakeProc:
            info = {"name": "Weixin.exe"}

            def open_files(self):
                class F:
                    def __init__(self, path): self.path = path
                return [F(r"C:\x\xwechat_files\wxid_aaa_c1\db\a.db"),
                        F(r"C:\x\xwechat_files\wxid_bbb_c2\db\b.db")]

        class FakePsutil:
            @staticmethod
            def process_iter(_fields): return [FakeProc()]

        real = sys.modules.get("psutil")
        sys.modules["psutil"] = FakePsutil  # type: ignore[assignment]
        try:
            self.assertIsNone(mod.probe_account())
        finally:
            if real is not None:
                sys.modules["psutil"] = real
            else:
                del sys.modules["psutil"]

if __name__ == "__main__":
    unittest.main()
