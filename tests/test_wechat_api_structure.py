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
    """最小化的窗口必须先恢复，否则 pywinauto 的 wait("ready") 必然超时（2026-08-12 真机实测）。"""

    class _Window:
        def __init__(self, minimized: bool, restore_error: Exception | None = None) -> None:
            self._minimized = minimized
            self._restore_error = restore_error
            self.restored = False

        def is_minimized(self) -> bool:
            return self._minimized

        def restore(self) -> None:
            if self._restore_error is not None:
                raise self._restore_error
            self.restored = True

    def test_minimized_window_is_restored(self) -> None:
        window = self._Window(minimized=True)

        UiaWeChatDriver._restore_if_minimized(window)

        self.assertTrue(window.restored)

    def test_normal_window_is_left_alone(self) -> None:
        window = self._Window(minimized=False)

        UiaWeChatDriver._restore_if_minimized(window)

        self.assertFalse(window.restored)

    def test_restore_failure_does_not_mask_the_real_error(self) -> None:
        window = self._Window(minimized=True, restore_error=RuntimeError("boom"))

        UiaWeChatDriver._restore_if_minimized(window)  # 不抛，交给后面的 wait 报真正的错


if __name__ == "__main__":
    unittest.main()
