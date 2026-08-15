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
from io import BytesIO
from pathlib import Path
from typing import Any

from wxclient.ui import auth, client_api, device, token_store, uia_status
from wxclient.ui.client_api import ApiError, ClientApi
from wxclient.ui.status_reporter import StatusReporter
from wxclient.ui.token_store import Protector, StoredSession, TokenStore

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
        self.assertNotIn("password", raw, "密码存在本机等于没有密码")


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

    def test_exposed_uia_is_connected_and_carries_no_made_up_identity(self) -> None:
        state = uia_status.classify({"platform": "Windows", "logged_in": True,
                                     "uia_exposed": True,
                                     "selected_window": {"process_version": "4.0.1.15"}})
        assert state is not None
        self.assertEqual(state.status, uia_status.CONNECTED)
        self.assertIsNone(state.failure)
        self.assertEqual(state.instance, {"processVersion": "4.0.1.15"})
        self.assertNotIn("accountRef", state.instance or {},
                         "★ 读不到微信账号就别编一个「稳定标识」，那会让服务端的占用检查形同虚设")

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

    def test_nothing_is_reported_before_login(self) -> None:
        """没 Token 的时候（还没登录）不该往服务端打——那些请求只会得到 401。"""
        reporter = StatusReporter(self.api, lambda: "", client_version="1.0.0")
        self.addCleanup(reporter.stop)

        reporter.client_online()
        reporter.wechat_probe({"platform": "Windows", "window_count": 0})
        self._drain()

        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
