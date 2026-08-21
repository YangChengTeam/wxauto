"""出错的时候**不许把人家的微信关掉**。

2026-08-21 用户实报：「搜索到一个不存在的用户，微信就会有问题了」。查下来「最后那一下」
有两个来源，两个都会把微信收进右下角托盘（微信 4.x 里「关窗口」=进托盘），而托盘一进去
UIA 子树就被收走，之后每条指令、每次读消息全失败，`_restore_if_minimized` 也救不回来
（那只救最小化），只能人去点托盘图标：

1. **vendor 自己按的**：`pyweixin` 的 `Navigator.search_friend()` 在「查无此人」两条分支里
   写着 `main_window.close()` 再 `raise NoSuchFriendError`——它假设自己独占微信，用完关掉；
   可我们是常驻网关，这一关就是整条链路停摆。
2. **pywinauto 替我们按的**：`UIAWrapper.close()` 关不掉（控件不支持 Window 模式）时会
   `type_keys("{ESC}")` 兜底，而 ESC 在微信 4.x 里就是关窗口。

这个文件钉的是「收场动作」这条规矩本身，不是「搜到了谁」（那在 test_search_contacts.py）。
姊妹文件 `test_uia_search_cleanup.py` 钉的是我们**自己**代码里那条路径。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import wxclient.wechat_api.uia as uia_module

VENDOR = Path(__file__).resolve().parents[1] / "third_party" / "pywechat" / "pyweixin" / "WeChatTools.py"


class VendorNeverClosesWeChatTests(unittest.TestCase):
    """vendor 里每一处 `main_window.close()` 都必须由 `close_weixin` 开关守着。

    我们全程传 `close_weixin=False`（`_pyweixin_class` 里还把 `GlobalConfig.close_weixin`
    也按成 False），所以「有闸」= 永远不会关。裸着的那些是出错分支里顺手关的，已摘掉。
    """

    def test_每一处关主窗口都得有_close_weixin_闸(self) -> None:
        lines = VENDOR.read_text(encoding="utf-8").split("\n")
        naked: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            code = line.split("#", 1)[0]      # 注释里提到它不算数（我们摘掉时留了说明）
            if not re.search(r"\bmain_window\.close\(\)", code):
                continue
            guarded = "close_weixin" in line or "close_weixin" in lines[i - 1]
            if not guarded:
                naked.append((i + 1, line.strip()))
        self.assertEqual(
            naked, [],
            "vendor 里有没被 close_weixin 守住的 main_window.close()——"
            "它会把微信收进托盘，整条网关链路停摆且要人去点托盘才恢复",
        )

    def test_查无此人那两条分支仍然照常抛异常(self) -> None:
        """摘掉 close 不能顺手把错误也吞了——查不到就得抛，调用方靠它判断。"""
        source = VENDOR.read_text(encoding="utf-8")
        search_friend = source.split("def search_friend(")[1].split("\n    @staticmethod")[0]
        self.assertEqual(
            search_friend.count("raise NoSuchFriendError"), 2,
            "search_friend 的两条『查无此人』分支都得照旧 raise",
        )


class _FakeNoPatternInterfaceError(Exception):
    pass


class _FakeIfaceMissing:
    @property
    def iface_window(self) -> Any:
        raise _FakeNoPatternInterfaceError("no window pattern")


class EscCloseFallbackTests(unittest.TestCase):
    """`close()` 关不掉时必须**什么都不做**，绝不能回退去按 ESC。"""

    def setUp(self) -> None:
        uia_defines = mock.Mock()
        uia_defines.NoPatternInterfaceError = _FakeNoPatternInterfaceError

        class UIAWrapper:
            """假的 pywinauto 包装器：`close` 就是原版那套「关不掉就按 ESC」。"""

            def __init__(self) -> None:
                self.keys: list[str] = []
                self.closed = False

            def type_keys(self, keys: str, **_: object) -> None:
                self.keys.append(keys)

            def close(self) -> None:
                try:
                    self.iface_window.Close()
                except _FakeNoPatternInterfaceError:
                    self.type_keys("{ESC}")

        uiawrapper = mock.Mock()
        uiawrapper.UIAWrapper = UIAWrapper
        controls = mock.Mock()
        controls.uiawrapper = uiawrapper
        pywinauto = mock.Mock()
        pywinauto.uia_defines = uia_defines
        pywinauto.controls = controls

        self._patcher = mock.patch.dict(sys.modules, {
            "pywinauto": pywinauto,
            "pywinauto.uia_defines": uia_defines,
            "pywinauto.controls": controls,
            "pywinauto.controls.uiawrapper": uiawrapper,
        })
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.UIAWrapper = UIAWrapper
        uia_module._ESC_CLOSE_FALLBACK_DISABLED = False
        self.addCleanup(setattr, uia_module, "_ESC_CLOSE_FALLBACK_DISABLED", False)

    def test_没打补丁之前_关不掉就会按ESC(self) -> None:
        """先证明这个兜底真的在——不然下面那条测的是空气。"""
        node = self.UIAWrapper()
        node.iface_window = _FakeIfaceMissing().iface_window if False else None
        type(node).iface_window = property(lambda _self: (_ for _ in ()).throw(_FakeNoPatternInterfaceError()))
        try:
            node.close()
            self.assertEqual(node.keys, ["{ESC}"])
        finally:
            del type(node).iface_window

    def test_打了补丁之后_关不掉就什么都不做(self) -> None:
        uia_module._disable_esc_close_fallback()
        node = self.UIAWrapper()
        type(node).iface_window = property(lambda _self: (_ for _ in ()).throw(_FakeNoPatternInterfaceError()))
        try:
            node.close()
            self.assertEqual(node.keys, [], "关不掉就该什么都不做，绝不能按 ESC——ESC 会把微信收进托盘")
        finally:
            del type(node).iface_window

    def test_关得掉的窗口照旧关掉(self) -> None:
        """摘掉的只有 ESC 兜底，正常能关的（独立窗口）不能跟着失效。"""
        uia_module._disable_esc_close_fallback()
        node = self.UIAWrapper()
        iface = mock.Mock()
        type(node).iface_window = property(lambda _self: iface)
        try:
            node.close()
            iface.Close.assert_called_once_with()
            self.assertEqual(node.keys, [])
        finally:
            del type(node).iface_window

    def test_装不上pywinauto就静默跳过(self) -> None:
        """Linux 上跑单测没有 pywinauto，这条不该炸。"""
        self._patcher.stop()
        with mock.patch.dict(sys.modules, {"pywinauto": None}):
            uia_module._disable_esc_close_fallback()
        self._patcher.start()


if __name__ == "__main__":
    unittest.main()
