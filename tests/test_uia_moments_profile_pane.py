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
    """真机 4.1.12 的左侧工具栏：L546 T160 R606 B849。"""

    top = 160
    left = 546

    @staticmethod
    def width() -> int:
        return 60

    @staticmethod
    def mid_point() -> Any:
        return type("P", (), {"x": 576})


class _Clicker:
    """记录点了哪儿的公共部分。开关语义：面板开着就收走，没开就照剧本决定灵不灵。"""

    def __init__(self, desktop: _Desktop, opens: list[bool]) -> None:
        self._desktop = desktop
        self._opens = opens
        self.clicks: list[Any] = []

    def _fire(self, where: Any) -> None:
        self.clicks.append(where)
        if self._desktop.open:
            self._desktop.open.clear()
            return
        if self._opens and self._opens.pop(0):
            self._desktop.open.append(_Window(PANE_CLASS))


class _Toolbar(_Clicker):
    """工具栏控件本体。`click_input` 会先把窗口提到前台——这正是要的那一下。"""

    def __init__(self, desktop: _Desktop, opens: list[bool], *, broken: bool = False) -> None:
        super().__init__(desktop, opens)
        self._broken = broken

    @staticmethod
    def rectangle() -> Any:
        return _Rect()

    def click_input(self, coords: tuple[int, int]) -> None:
        if self._broken:
            raise RuntimeError("控件没了")
        self._fire(("控件", coords))


class _Mouse(_Clicker):
    """屏幕绝对坐标那条兜底路。"""

    def click(self, coords: tuple[int, int]) -> None:
        self._fire(("屏幕", coords))


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

    def _open(self, opens: list[bool], *, broken: bool = False) -> tuple[Any, _Clicker]:
        """两条路共用一份剧本：谁点的都算一次，`clicks` 里带着是哪条路。"""
        script = list(opens)
        toolbar = _Toolbar(self.desktop, script, broken=broken)
        mouse = _Mouse(self.desktop, script)
        pane = self.driver._open_profile_pane(self.desktop, toolbar, mouse)
        toolbar.clicks.extend(mouse.clicks)
        return pane, toolbar

    def test_一下就开出来时只点一次(self) -> None:
        pane, mouse = self._open([True])

        self.assertEqual(pane.element_info.class_name, PANE_CLASS)
        self.assertEqual(len(mouse.clicks), 1)
        self.assertEqual(mouse.clicks[0], ("控件", (30, 52)), "走控件坐标，顺带把微信提到前台")

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


class ClickingTheAvatarTests(unittest.TestCase):
    """★ 点头像**不能盲点屏幕坐标**。

    2026-08-29 真机：那台机器上 Chrome 最大化盖在微信上面，`mouse.click(coords=…)`
    那一下点进了 Chrome —— 按微信窗口矩形截出来的图是一个 Chrome 页面，鼠标最后停在
    (901,1071)。同一轮里点「通讯录」是好的，因为 `click_input()` 会先把窗口提到前台。
    这就是线上「WeChat profile pane did not appear」的真因。
    """

    def setUp(self) -> None:
        self.driver = UiaWeChatDriver(dry_run=False)
        self.desktop = _Desktop()

    def test_优先走控件那条_它会把微信提到前台(self) -> None:
        toolbar = _Toolbar(self.desktop, [True])
        mouse = _Mouse(self.desktop, [])

        self.driver._click_avatar(toolbar, mouse)

        self.assertEqual(toolbar.clicks, [("控件", (30, 52))])
        self.assertEqual(mouse.clicks, [], "别再盲点屏幕坐标——盖住微信就点进别人家了")

    def test_控件那条不行才退回屏幕坐标(self) -> None:
        toolbar = _Toolbar(self.desktop, [True], broken=True)
        mouse = _Mouse(self.desktop, [True])

        self.driver._click_avatar(toolbar, mouse)

        self.assertEqual(mouse.clicks, [("屏幕", (576, 212))], "兜底还在，坐标也没变")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
