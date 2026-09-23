"""微信驱动首次 import 的 COM 套间：必须是 MTA，否则别的线程上的 UIA 根枚举全空。

2026-09-23 客户机：0.1.60 把 comtypes 排到 pywinauto 前面 import，COM 被定成 STA，
网关队列线程上 `Desktop(uia).window(添加朋友面板)` 找不到——面板弹出来了，号码没输进去。
同一台机实测：pywinauto 先 import → 另一线程枚举到 9 个顶层窗；comtypes 先 → 0 个。
这里不需要 Windows：钉的是「import 顺序」和「import 之前 coinit_flags 已经是 MTA」这两条。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from wxclient import heavy_import


class WechatDriverComModeTests(unittest.TestCase):
    def _run_import(self) -> list[tuple[str, object]]:
        seen: list[tuple[str, object]] = []

        def fake_import(name: str) -> None:
            seen.append((name, getattr(sys, "coinit_flags", None)))

        before = heavy_import._driver_imported
        had_flags = hasattr(sys, "coinit_flags")
        saved = getattr(sys, "coinit_flags", None)
        if had_flags:
            del sys.coinit_flags
        try:
            with mock.patch.object(heavy_import.platform, "system", return_value="Windows"), \
                    mock.patch.object(heavy_import.importlib, "import_module", side_effect=fake_import):
                heavy_import._driver_imported = False
                heavy_import.import_wechat_driver()
        finally:
            heavy_import._driver_imported = before
            if had_flags:
                sys.coinit_flags = saved
            elif hasattr(sys, "coinit_flags"):
                del sys.coinit_flags
        return seen

    def test_pywinauto_is_imported_before_comtypes(self) -> None:
        names = [name for name, _ in self._run_import()]
        self.assertLess(names.index("pywinauto"), names.index("comtypes"),
                        "comtypes 先 import 会把 COM 定成 STA，别的线程上 UIA 根枚举就全空了")

    def test_com_is_multithreaded_before_anything_is_imported(self) -> None:
        for name, flags in self._run_import():
            self.assertEqual(flags, heavy_import.COM_MULTITHREADED, f"import {name} 时 COM 套间还不是 MTA")


if __name__ == "__main__":
    unittest.main()
