from __future__ import annotations

import unittest

from wxclient.wechat_api import (
    CompositeWeChatDriver,
    UiaWeChatDriver,
    WeChatApi,
    WeChatApiError,
)
from wxclient.wechat_api.api import CompositeWeChatDriver as SplitCompositeDriver
from wxclient.wechat_api.api import WeChatApi as SplitWeChatApi
from wxclient.wechat_api.errors import WeChatApiError as SplitWeChatApiError
from wxclient.wechat_api.uia import UiaWeChatDriver as SplitUiaWeChatDriver


class WeChatApiStructureTests(unittest.TestCase):
    def test_public_exports_are_backed_by_responsibility_modules(self) -> None:
        self.assertIs(WeChatApi, SplitWeChatApi)
        self.assertIs(CompositeWeChatDriver, SplitCompositeDriver)
        self.assertIs(UiaWeChatDriver, SplitUiaWeChatDriver)
        self.assertIs(WeChatApiError, SplitWeChatApiError)

    def test_facade_composes_uia_driver_without_inheriting_it(self) -> None:
        api = WeChatApi(dry_run=True)

        self.assertNotIsInstance(api, UiaWeChatDriver)
        self.assertIsInstance(api._uia_driver, UiaWeChatDriver)
        self.assertIs(api._composite_driver.drivers[0], api._uia_driver)
        self.assertTrue(api._is_dry_run(None))


class RestoreMinimizedWindowTests(unittest.TestCase):
    """微信最小化时 connect 直接找不到窗口，动作前必须按 hwnd 恢复（2026-08-12 真机实测）。"""

    def test_minimized_window_yields_its_handle(self) -> None:
        self.assertEqual(
            UiaWeChatDriver._minimized_handle({"handle": 199668, "is_iconic": True}),
            199668,
        )

    def test_normal_window_is_left_alone(self) -> None:
        self.assertIsNone(
            UiaWeChatDriver._minimized_handle({"handle": 199668, "is_iconic": False})
        )

    def test_missing_or_malformed_probe_result_is_tolerated(self) -> None:
        for window_info in (None, {}, {"is_iconic": True}, {"is_iconic": True, "handle": "x"}):
            with self.subTest(window_info=window_info):
                self.assertIsNone(UiaWeChatDriver._minimized_handle(window_info))

    def test_forced_restore_ignores_iconic_flag(self) -> None:
        # UIA 子树塌掉时探测未必还报 is_iconic，但只要有句柄就该恢复一次再复探
        self.assertEqual(
            UiaWeChatDriver._minimized_handle({"handle": 199668, "is_iconic": False}, force=True),
            199668,
        )
        self.assertIsNone(UiaWeChatDriver._minimized_handle({}, force=True))

    def test_restore_without_handle_is_a_noop(self) -> None:
        # 探测结果里没有可用句柄 → 不动窗口、不抛、如实返回 False（不能骗调用方去复探）
        # 故意不拿真句柄做用例：Windows 上跑测试会真的把那个窗口弹出来。
        for window_info in (None, {}, {"is_iconic": True}, {"is_iconic": True, "handle": "x"}):
            with self.subTest(window_info=window_info):
                self.assertFalse(UiaWeChatDriver._restore_if_minimized(window_info))
                self.assertFalse(UiaWeChatDriver._restore_if_minimized(window_info, force=True))


class SendMessageVerificationTests(unittest.TestCase):
    """★ 075 发后核对（需求文档 §12.4）。

    底座 `send_messages_to_friend` 打完字按回车就返回成功，**没发出去也返回成功**——
    文字会留在输入框里。067 在文件那条路径上真机踩过一模一样的坑（文件躺在输入框里、
    回执却报 done）。宁可报失败也不谎报「已发送」：AI 会照着回执告诉用户「已经发过去了」。
    """

    def _driver(self, leftovers: list[int]) -> UiaWeChatDriver:
        driver = UiaWeChatDriver(dry_run=False)
        self.nudged = 0

        def fake_len(_friend: str) -> int:
            return leftovers.pop(0) if leftovers else 0

        def fake_nudge(_friend: str) -> None:
            self.nudged += 1

        driver._ensure_ready = lambda: None                     # type: ignore[method-assign]
        driver._pyweixin_class = lambda _name: _FakeMessages    # type: ignore[method-assign]
        driver._chat_input_text_len = fake_len                  # type: ignore[method-assign]
        driver._nudge_send = fake_nudge                         # type: ignore[method-assign]
        return driver

    def test_empty_input_box_means_it_really_went_out(self) -> None:
        result = self._driver([0]).send_message("王建国", "您好")
        self.assertTrue(result["verified"])
        self.assertEqual(self.nudged, 0)

    def test_stuck_text_gets_one_nudge_then_succeeds(self) -> None:
        """补按一次回车把停在框里的那条发走。这**不是**重发——不会产生第二条消息。"""
        result = self._driver([5, 0]).send_message("王建国", "您好")
        self.assertTrue(result["verified"])
        self.assertEqual(self.nudged, 1)

    def test_still_stuck_raises_instead_of_reporting_success(self) -> None:
        """★ 核心：发不出去就要报失败，不能让 AI 去跟用户说「已经发过去了」。"""
        driver = self._driver([5, 5])
        with self.assertRaises(WeChatApiError) as caught:
            driver.send_message("王建国", "您好")
        self.assertIn("没能发出去", str(caught.exception))

    def test_unprobeable_input_box_is_reported_as_unverified_not_failed(self) -> None:
        """探测不到输入框 = 「不知道」，据实说没核对过，别把不知道讲成已确认。"""
        result = self._driver([-1]).send_message("王建国", "您好")
        self.assertFalse(result["verified"])
        self.assertEqual(self.nudged, 0)


class _FakeMessages:
    @staticmethod
    def send_messages_to_friend(**_kwargs) -> None:
        return None


class _FakeControl:
    """够用的假控件：认得出「你要的是哪一个子控件」，并记下被点过没有。"""

    def __init__(self, name: str, text: str = "", children: dict | None = None,
                 exists_: bool = True, log: list | None = None) -> None:
        self.name = name
        self._text = text
        self._children = children or {}
        self._exists = exists_
        self.log = log if log is not None else []
        self.set_texts: list[str] = []

    def exists(self, timeout: float = 0) -> bool:
        return self._exists

    def window_text(self) -> str:
        return self._text

    def descendants(self, **_: object) -> list:
        return []

    def child_window(self, **criteria):
        key = criteria.get("_key") or criteria.get("control_type")
        if key == "Edit":
            key = f"Edit{criteria.get('found_index', '')}"
        child = self._children.get(key)
        if child is None:
            return _FakeControl(f"{self.name}/缺席", exists_=False, log=self.log)
        return child

    def set_text(self, value: str) -> None:
        self.set_texts.append(value)

    def type_keys(self, value: str, **_: object) -> None:
        self.set_texts.append(value)

    def click_input(self) -> None:
        self.log.append(f"click:{self.name}")

    def close(self) -> None:
        self.log.append("close:面板")


class _FakeUielements:
    Groups = type("G", (), {"ContactProfileViewGroup": {"_key": "profile"}})()
    Buttons = type("B", (), {"AddToContactsButton": {"_key": "add"},
                             "ConfirmButton": {"_key": "confirm"}})()
    Windows = type("W", (), {"VerifyFriendWindow": {"_key": "verify"}})()


class AddFriendReturnsTheNameTests(unittest.TestCase):
    """★ 加好友要把「后续拿什么名字回消息」带回来（用户 2026-08-15）。

    三个名字别混：`name` 是对方昵称（给人看），`remark` 是我们设的备注，
    `session_name` = 备注优先、没有才退回昵称 —— 那才是 send_message(to=...) 该用的。
    """

    def _driver(self, profile_exists: bool = True, add_button_exists: bool = True):
        from unittest import mock

        from wxclient.wechat_api.uia import UiaWeChatDriver

        log: list[str] = []
        verify = _FakeControl("申请窗", children={
            "Edit0": _FakeControl("招呼语", log=log),
            "Edit1": _FakeControl("备注", log=log),
            "confirm": _FakeControl("确定", log=log),
        }, log=log)
        profile = _FakeControl(
            "名片", text="张三\n微信号：wxid_abc\n地区：深圳",
            children={"add": _FakeControl("添加到通讯录", exists_=add_button_exists, log=log)},
            exists_=profile_exists, log=log,
        )
        pane = _FakeControl("添加朋友面板", children={
            "Edit": _FakeControl("搜索框", log=log), "profile": profile,
        }, log=log)
        vendor_calls: list[dict] = []

        class _Navigator:
            @staticmethod
            def open_add_friend_panel(**_: object):
                return pane, _FakeControl("主窗口", log=log)

        class _Tools:
            @staticmethod
            def move_window_to_center(**_: object):
                return verify

        class _FriendSettings:
            @staticmethod
            def add_new_friend(**kwargs: object) -> None:
                vendor_calls.append(dict(kwargs))

        classes = {"Navigator": _Navigator, "Tools": _Tools, "FriendSettings": _FriendSettings}
        driver = UiaWeChatDriver(dry_run=False)
        stack = mock.patch.multiple(
            driver,
            _ensure_ready=mock.DEFAULT,
            _pyweixin_class=mock.DEFAULT,
            _pyweixin_submodule=mock.DEFAULT,
        )
        patched = stack.start()
        self.addCleanup(stack.stop)
        patched["_pyweixin_class"].side_effect = lambda name: classes[name]
        patched["_pyweixin_submodule"].side_effect = lambda name: _FakeUielements
        return driver, log, vendor_calls, verify

    def test_the_nickname_comes_back_and_the_remark_wins_as_session_name(self) -> None:
        driver, log, vendor_calls, verify = self._driver()

        result = driver.add_friend("13800138000", "你好", remark="张三-仓库工")

        self.assertEqual(result["name"], "张三", "对方昵称要读回来")
        self.assertEqual(result["remark"], "张三-仓库工")
        self.assertEqual(result["session_name"], "张三-仓库工", "★ 有备注就用备注")
        self.assertIn("click:添加到通讯录", log)
        self.assertIn("click:确定", log)
        self.assertEqual(verify.child_window(control_type="Edit", found_index=1).set_texts,
                         ["张三-仓库工"], "备注要真的填进去")
        self.assertEqual(vendor_calls, [], "自己走通了就不该再调 vendor")

    def test_without_a_remark_the_session_name_falls_back_to_the_nickname(self) -> None:
        driver, _log, _vendor, _verify = self._driver()

        result = driver.add_friend("13800138000", "你好")

        self.assertEqual(result["session_name"], "张三")

    def test_a_number_that_cannot_be_found_fails_loudly(self) -> None:
        """★ 原来查无此号是**静默返回成功**的，AI 端只会以为对方没通过而一直等。"""
        driver, log, vendor_calls, _verify = self._driver(profile_exists=False)

        with self.assertRaises(WeChatApiError) as caught:
            driver.add_friend("13800138000", "你好")

        self.assertIn("没搜到这个号", str(caught.exception))
        self.assertNotIn("click:添加到通讯录", log, "没搜到就绝不该点添加")
        self.assertEqual(vendor_calls, [], "这是明确失败，不是退化——别再让 vendor 加一遍")

    def test_dry_run_says_which_name_would_be_used(self) -> None:
        from wxclient.wechat_api.uia import UiaWeChatDriver

        result = UiaWeChatDriver(dry_run=True).add_friend("138", "你好", remark="李四-司机")

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["session_name"], "李四-司机")
        self.assertIsNone(result["name"])
if __name__ == "__main__":
    unittest.main()
