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


if __name__ == "__main__":
    unittest.main()
