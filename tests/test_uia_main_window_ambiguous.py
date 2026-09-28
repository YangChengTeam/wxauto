"""两个标题都叫「微信」的顶层窗口：别整条失败，挑有会话列表的那个按句柄连（0.1.93）。

2026-09-28 用户机：「读取会话列表」报「无法定位当前微信主窗口：There are 2 elements that match the criteria」。
"""

from __future__ import annotations

import sys
import types
import unittest
from typing import Any, ClassVar
from unittest import mock

from wxclient.wechat_api import uia
from wxclient.wechat_api.errors import WeChatApiError


class _Win:
    def __init__(self, handle: int, score: int, title: str = "微信") -> None:
        self.handle, self.score, self.title = handle, score, title

    def window_text(self) -> str:
        return self.title


def _driver() -> Any:
    driver = uia.UiaWeChatDriver.__new__(uia.UiaWeChatDriver)
    driver._current_window_score = lambda w: w.score  # type: ignore[method-assign]
    return driver


class PickMainWindowTests(unittest.TestCase):
    def test_挑有会话列表的那个_不挑先枚举到的(self) -> None:
        self.assertEqual(_driver()._pick_main_window_handle([_Win(111, 0), _Win(222, 15)]), 222)

    def test_同分取先枚举到的(self) -> None:
        self.assertEqual(_driver()._pick_main_window_handle([_Win(111, 10), _Win(222, 10)]), 111)

    def test_一个都没有就回None(self) -> None:
        self.assertIsNone(_driver()._pick_main_window_handle([]))


class ElementAmbiguousError(Exception):
    pass


class _Spec:
    def __init__(self, **criteria: Any) -> None:
        self.criteria = criteria

    def wait(self, *_: Any, **__: Any) -> None:
        if "title" in self.criteria:
            raise ElementAmbiguousError("There are 2 elements that match the criteria {'title': '微信'}")


class _App:
    connected: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, backend: str) -> None:
        pass

    def connect(self, **criteria: Any) -> _App:
        _App.connected.append(criteria)
        return self

    def window(self, **criteria: Any) -> _Spec:
        return _Spec(**criteria)


class CurrentMainWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        _App.connected = []
        fake = types.ModuleType("pywinauto")
        fake.Application = _App  # type: ignore[attr-defined]
        patcher = mock.patch.dict(sys.modules, {"pywinauto": fake})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_标题歧义时按挑中的句柄连(self) -> None:
        driver = _driver()
        with mock.patch.object(uia.UiaWeChatDriver, "_titled_wechat_windows", return_value=[_Win(111, 0), _Win(222, 15)]):
            window = driver._current_main_window()
        self.assertEqual(window.criteria, {"handle": 222})
        self.assertEqual(_App.connected[-1], {"handle": 222})

    def test_挑不出来_照旧报无法定位(self) -> None:
        driver = _driver()
        with (
            mock.patch.object(uia.UiaWeChatDriver, "_titled_wechat_windows", return_value=[]),
            self.assertRaises(WeChatApiError) as ctx,
        ):
            driver._current_main_window()
        self.assertIn("无法定位当前微信主窗口", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
