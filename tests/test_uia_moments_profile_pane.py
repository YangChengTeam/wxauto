"""发朋友圈第一步：点头像把资料面板叫出来。

2026-08-29 线上实报：`微信任务执行失败:发布朋友圈#b390a5ee，原因:WeChat profile pane
did not appear`。真机做了三轮干净复现（点之前先确认桌面上没有面板）：

| 轮次 | 点完顶层枚举看得到面板 | 原来那条判据 | 耗时 |
|---|---|---|---|
| 1 | 有 | True | 3.19s |
| 2 | 有 | True | 3.04s |
| 3 | **没有** | False | 3.19s |

结论有三条，这个文件一条一条钉住：

1. **判据没错，是查得太慢**。原来 `desktop.window(**判据).exists(timeout=2)` 带
   `top_level_only=False`，要走一遍整个桌面的控件树——实测**一次 3.0～3.2 秒**，
   而预算只给了 2 秒：等于只查一次、没有重试。面板本身就是顶层窗口，枚举顶层一瞬间就回来。
2. **点头像是个开关**。上一趟死在后面某一步、把面板留在屏幕上，这一趟一点就把它关了，
   于是「did not appear」，而且会自己反复下去。所以先看在不在。
3. **点一下未必生效**（第 3 轮）。同一坐标的连续点击会被当成双击吞掉一次，要能再点一次。
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from wxclient.wechat_api.uia import UiaWeChatDriver
from wxclient.wechat_api.errors import WeChatApiError

PANE_CLASS = UiaWeChatDriver.PROFILE_PANE_CLASS


class _Info:
    def __init__(self, class_name: str) -> None:
        self.class_name = class_name


class _Window:
    def __init__(self, class_name: str) -> None:
        self.element_info = _Info(class_name)


class _Desktop:
    """只认顶层枚举的假桌面。`window()` 一旦被碰就记一笔——生产代码不该再走那条慢路。"""

    def __init__(self) -> None:
        self.open: list[_Window] = []
        self.slow_lookups = 0

    def windows(self) -> list[_Window]:
        return [_Window("Chrome_WidgetWin_1"), *self.open, _Window("Shell_TrayWnd")]

    def window(self, **_: object) -> Any:  # pragma: no cover - 走到就是回归
        self.slow_lookups += 1
        raise AssertionError("不该再用会走全树的 desktop.window() 判据")


class _Rect:
    top = 160

    @staticmethod
    def mid_point() -> Any:
        return type("P", (), {"x": 576})


class _Toolbar:
    @staticmethod
    def rectangle() -> Any:
        return _Rect()


class _Mouse:
    """点一下 = 开关：面板开着就收走，没开就按 `opens` 给的剧本决定这一下灵不灵。"""

    def __init__(self, desktop: _Desktop, opens: list[bool]) -> None:
        self._desktop = desktop
        self._opens = list(opens)
        self.clicks: list[tuple[int, int]] = []

    def click(self, coords: tuple[int, int]) -> None:
        self.clicks.append(coords)
        if self._desktop.open:
            self._desktop.open.clear()
            return
        if self._opens and self._opens.pop(0):
            self._desktop.open.append(_Window(PANE_CLASS))


class OpeningTheProfilePaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = UiaWeChatDriver(dry_run=False)
        self.desktop = _Desktop()
        # 时间空跑：`sleep` 只推进假时钟，等待逻辑照走，测试不真等 8 秒。
        self.clock = [0.0]
        patcher = mock.patch.multiple(
            "wxclient.wechat_api.uia.time",
            sleep=lambda s: self.clock.__setitem__(0, self.clock[0] + s),
            monotonic=lambda: self.clock[0],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _open(self, opens: list[bool]) -> tuple[Any, _Mouse]:
        mouse = _Mouse(self.desktop, opens)
        pane = self.driver._open_profile_pane(self.desktop, _Toolbar(), mouse)
        return pane, mouse

    def test_一下就开出来时只点一次(self) -> None:
        pane, mouse = self._open([True])

        self.assertEqual(pane.element_info.class_name, PANE_CLASS)
        self.assertEqual(len(mouse.clicks), 1)
        self.assertEqual(mouse.clicks[0], (576, 212), "还是头像那个坐标，路没换")

    def test_第一下没生效就再点一次(self) -> None:
        """★ 真机第 3 轮的那一下。同坐标连点会被合成双击吞掉一次。"""
        pane, mouse = self._open([False, True])

        self.assertEqual(pane.element_info.class_name, PANE_CLASS)
        self.assertEqual(len(mouse.clicks), 2)

    def test_面板已经开着就不点它(self) -> None:
        """★ 点头像是开关：上一趟留下的面板，再点一下就是关掉它。"""
        self.desktop.open.append(_Window(PANE_CLASS))

        pane, mouse = self._open([])

        self.assertEqual(pane.element_info.class_name, PANE_CLASS)
        self.assertEqual(mouse.clicks, [], "面板就在眼前，一下都不该点")

    def test_两下都没开出来才报错(self) -> None:
        with self.assertRaises(WeChatApiError) as caught:
            self._open([False, False])

        self.assertIn("profile pane", str(caught.exception))

    def test_从头到尾不碰那条会走全树的判据(self) -> None:
        """慢的是查询本身（3 秒/次），不是面板。顶层枚举够用。"""
        self._open([True])

        self.assertEqual(self.desktop.slow_lookups, 0)

    def test_等面板的预算大于一次查询的耗时(self) -> None:
        """原来的 `timeout=2` 比一次查询（实测 3.0～3.2 秒）还短，等于一次都不重试。"""
        self.desktop.open.append(_Window(PANE_CLASS))

        started = self.clock[0]
        self.driver._wait_profile_pane(self.desktop, None)
        self.assertEqual(self.clock[0], started, "面板在的时候立刻回来")

        self.desktop.open.clear()
        with self.assertRaises(WeChatApiError):
            self.driver._wait_profile_pane(self.desktop, None)
        self.assertGreaterEqual(self.clock[0] - started, 3.2, "至少要等过一次查询的耗时")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
