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
