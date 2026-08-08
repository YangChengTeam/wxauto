from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wxclient.wechat_api import WeChatApi


class _DeniedProcess:
    @property
    def info(self):  # type: ignore[no-untyped-def]
        module = sys.modules["pyweixin.WeChatTools"]
        raise module.psutil.AccessDenied(pid=1234)


class PyWeixinProcessDetectionTests(unittest.TestCase):
    def test_is_weixin_running_uses_psutil_and_skips_inaccessible_processes(self) -> None:
        tools = WeChatApi(dry_run=True)._pyweixin_class("Tools")
        module = sys.modules[tools.__module__]
        processes = [
            _DeniedProcess(),
            SimpleNamespace(info={"name": "explorer.exe"}),
            SimpleNamespace(info={"name": "Weixin.exe"}),
        ]

        with patch.object(module.psutil, "process_iter", return_value=processes) as process_iter:
            self.assertTrue(tools.is_weixin_running())

        process_iter.assert_called_once_with(["name"])

    def test_is_weixin_running_returns_false_when_weixin_is_absent(self) -> None:
        tools = WeChatApi(dry_run=True)._pyweixin_class("Tools")
        module = sys.modules[tools.__module__]
        processes = [SimpleNamespace(info={"name": "explorer.exe"})]

        with patch.object(module.psutil, "process_iter", return_value=processes):
            self.assertFalse(tools.is_weixin_running())


if __name__ == "__main__":
    unittest.main()
