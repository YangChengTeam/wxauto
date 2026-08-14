from __future__ import annotations

import unittest

from wxclient.wechat_api import (
    CompositeWeChatDriver,
    OmniVisualDriver,
    UiaWeChatDriver,
    WeChatApi,
    WeChatApiError,
)
from wxclient.wechat_api.api import CompositeWeChatDriver as SplitCompositeDriver
from wxclient.wechat_api.api import WeChatApi as SplitWeChatApi
from wxclient.wechat_api.errors import WeChatApiError as SplitWeChatApiError
from wxclient.wechat_api.omni import OmniVisualDriver as SplitOmniVisualDriver
from wxclient.wechat_api.uia import UiaWeChatDriver as SplitUiaWeChatDriver


class WeChatApiStructureTests(unittest.TestCase):
    def test_public_exports_are_backed_by_responsibility_modules(self) -> None:
        self.assertIs(WeChatApi, SplitWeChatApi)
        self.assertIs(CompositeWeChatDriver, SplitCompositeDriver)
        self.assertIs(UiaWeChatDriver, SplitUiaWeChatDriver)
        self.assertIs(OmniVisualDriver, SplitOmniVisualDriver)
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


if __name__ == "__main__":
    unittest.main()


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
