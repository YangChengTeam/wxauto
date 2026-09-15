"""发朋友圈按微信版本挑版式（4.1.13 起朋友圈停靠进主窗口）。纯假控件，不碰微信。"""
from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from wxclient.wechat_api.errors import MomentsDockedLayoutError, WeChatApiError
from wxclient.wechat_api.uia import (
    UiaWeChatDriver,
    find_gray_tile,
    moments_layout,
    parse_wechat_version,
)


class VersionTableTests(unittest.TestCase):
    def test_版本号解析_读不出来就空(self) -> None:
        self.assertEqual(parse_wechat_version("4.1.13.65"), (4, 1, 13, 65))
        self.assertEqual(parse_wechat_version("4.1.12"), (4, 1, 12))
        self.assertEqual(parse_wechat_version(""), ())
        self.assertEqual(parse_wechat_version(None), ())
        self.assertEqual(parse_wechat_version("4.1.x"), (4, 1))

    def test_4_1_13起走停靠版式_更早走独立窗口(self) -> None:
        self.assertEqual(moments_layout("4.1.13.12"), "docked")
        self.assertEqual(moments_layout("4.1.13.65"), "docked")
        self.assertEqual(moments_layout("4.2.0.1"), "docked")
        self.assertEqual(moments_layout("5.0.0.0"), "docked")
        self.assertEqual(moments_layout("4.1.12.20"), "window")
        self.assertEqual(moments_layout("4.0.1.15"), "window")

    def test_版本读不到按老版式走(self) -> None:
        self.assertEqual(moments_layout(""), "window")
        self.assertEqual(moments_layout(None), "window")


class GrayTileTests(unittest.TestCase):
    """真机 4.1.13.12：格子 358×84，背景 (250,250,250)，相机方块 (225,225,227) 在左起 92–166。"""

    @staticmethod
    def _cell(tile: tuple[int, int, int, int] | None, tile_color=(225, 225, 227)):
        width, height = 358, 84

        class _Pixels:
            """长得像 PIL 的 ``Image.load()``：按 ``pixels[x, y]`` 取色。"""

            def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int]:
                x, y = xy
                if tile and tile[0] <= x < tile[2] and tile[1] <= y < tile[3]:
                    return tile_color
                return (250, 250, 250)

        return _Pixels(), width, height

    def test_认出相机方块的中心(self) -> None:
        pixels, w, h = self._cell((92, 5, 166, 79))
        x, y = find_gray_tile(pixels, w, h)
        self.assertAlmostEqual(x, 129, delta=3)
        self.assertAlmostEqual(y, 42, delta=3)

    def test_悬停变深一点也认得(self) -> None:
        pixels, w, h = self._cell((92, 5, 166, 79), tile_color=(213, 213, 215))
        self.assertIsNotNone(find_gray_tile(pixels, w, h))

    def test_没有方块就None_零星噪点不算(self) -> None:
        pixels, w, h = self._cell(None)
        self.assertIsNone(find_gray_tile(pixels, w, h))
        pixels, w, h = self._cell((10, 10, 14, 14))     # 4×4 的噪点
        self.assertIsNone(find_gray_tile(pixels, w, h))

    def test_彩色块不算(self) -> None:
        pixels, w, h = self._cell((92, 5, 166, 79), tile_color=(225, 200, 180))
        self.assertIsNone(find_gray_tile(pixels, w, h))


class RealPillowTests(unittest.TestCase):
    def test_拿真的PIL图也认得(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:  # pragma: no cover
            self.skipTest("没装 Pillow")
        img = Image.new("RGB", (358, 84), (250, 250, 250))
        ImageDraw.Draw(img).rectangle((92, 5, 165, 78), fill=(225, 225, 227))
        x, y = find_gray_tile(img.load(), *img.size)
        self.assertAlmostEqual(x, 129, delta=3)
        self.assertAlmostEqual(y, 42, delta=3)


class _Untouchable:
    """碰一下就炸：断言那条路根本没去碰微信。"""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"不该碰到 {name}")


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = UiaWeChatDriver(dry_run=False)

    def test_新版微信直接走停靠流程(self) -> None:
        self.driver._wechat_version = "4.1.13.65"
        with mock.patch.object(self.driver, "_post_moments_docked", return_value={"layout": "docked"}) as docked, \
                mock.patch.object(self.driver, "_open_moments_timeline") as old_path:
            out = self.driver._post_moments_impl("文案", ["a.jpg"])
        docked.assert_called_once_with("文案", ["a.jpg"])
        old_path.assert_not_called()
        self.assertEqual(out, {"layout": "docked"})

    def test_版本没读到_老路开出停靠面板就就地改走新路(self) -> None:
        self.driver._wechat_version = ""
        fake_desktop = object()
        with mock.patch.object(self.driver, "_pyweixin_class", return_value=mock.MagicMock()), \
                mock.patch.object(self.driver, "_pyweixin_submodule", return_value=mock.MagicMock()), \
                mock.patch.object(self.driver, "_normalize_moments_media_paths", return_value=["a.jpg"]), \
                mock.patch.dict("sys.modules", {"pyautogui": mock.MagicMock(), "pywinauto": mock.MagicMock(Desktop=lambda backend: fake_desktop)}), \
                mock.patch.object(self.driver, "_open_moments_timeline", side_effect=MomentsDockedLayoutError("停靠")), \
                mock.patch.object(self.driver, "_post_moments_docked", return_value={"layout": "docked"}) as docked:
            out = self.driver._post_moments_impl("文案", ["a.jpg"])
        docked.assert_called_once_with("文案", ["a.jpg"], desktop=fake_desktop)
        self.assertEqual(out["layout"], "docked")

    def test_新版没有纯文字入口_没图直接拒_一下微信都不碰(self) -> None:
        self.driver._wechat_version = "4.1.13.65"
        with mock.patch.object(self.driver, "_open_docked_moments", side_effect=AssertionError("不该开朋友圈")), \
                self.assertRaises(WeChatApiError) as caught:
            self.driver._post_moments_docked("只有文字", [])
        self.assertIn("4.1.12", str(caught.exception))
        self.assertIn("4.1.13.65", str(caught.exception))

    def test_点赞评论那条老路在新版上直接报清楚(self) -> None:
        self.driver._wechat_version = "4.1.13.65"
        with self.assertRaises(MomentsDockedLayoutError) as caught:
            self.driver._open_moments_timeline(
                _Untouchable(), _Untouchable(), _Untouchable(), _Untouchable(), _Untouchable(),
            )
        self.assertIn("4.1.12", str(caught.exception))
        self.assertIn("4.1.13.65", str(caught.exception))

    def test_探测结果里的进程版本记下来(self) -> None:
        probe = {"uia_exposed": True, "selected_window": {"process_version": "4.1.13.65", "handle": 1}}
        with mock.patch("wxclient.wechat_api.uia.probe_wechat_uia", return_value=probe), \
                mock.patch.object(UiaWeChatDriver, "_restore_if_minimized", return_value=False):
            self.driver._ensure_ready()
        self.assertEqual(self.driver._wechat_version, "4.1.13.65")
        self.assertEqual(self.driver._moments_layout(), "docked")


class _Win32Control:
    def __init__(self, class_name: str, text: str = "") -> None:
        self._class = class_name
        self._text = text
        self.clicked = False
        self.focused = False

    def class_name(self) -> str:
        return self._class

    def window_text(self) -> str:
        return self._text

    def set_edit_text(self, value: str) -> None:
        self._text = value

    def set_focus(self) -> None:
        self.focused = True

    def click(self) -> None:
        self.clicked = True


class _Win32Dialog(_Win32Control):
    class backend:   # 模仿 pywinauto wrapper 的 backend 属性
        name = "win32"

    def __init__(self, kids: list[_Win32Control]) -> None:
        super().__init__("#32770", "选择文件")
        self.kids = kids

    def children(self) -> list[_Win32Control]:
        return self.kids


class Win32FileDialogTests(unittest.TestCase):
    def test_填文件名再点打开(self) -> None:
        driver = UiaWeChatDriver(dry_run=False)
        edit = _Win32Control("Edit", "素材")
        open_button = _Win32Control("Button", "打开(&O)")
        cancel = _Win32Control("Button", "取消")
        dialog = _Win32Dialog([_Win32Control("Static", "文件名(&N):"), edit, cancel, open_button])
        pyautogui = mock.MagicMock()
        with mock.patch("wxclient.wechat_api.uia.time.sleep"):
            driver._choose_native_files(dialog, [r"D:\wxauto\poster.jpg"], pyautogui)
        self.assertEqual(edit.window_text(), r"D:\wxauto\poster.jpg")
        self.assertTrue(open_button.clicked)
        self.assertFalse(cancel.clicked)
        pyautogui.hotkey.assert_not_called()

    def test_多张图按选文件框的多选写法(self) -> None:
        driver = UiaWeChatDriver(dry_run=False)
        edit = _Win32Control("Edit")
        dialog = _Win32Dialog([edit])
        with mock.patch("wxclient.wechat_api.uia.time.sleep"):
            driver._choose_native_files(dialog, [r"D:\a.jpg", r"D:\b.jpg"], mock.MagicMock())
        self.assertEqual(edit.window_text(), '"D:\\a.jpg" "D:\\b.jpg"')

    def test_没有打开按钮就Alt_O(self) -> None:
        driver = UiaWeChatDriver(dry_run=False)
        dialog = _Win32Dialog([_Win32Control("Edit")])
        pyautogui = mock.MagicMock()
        with mock.patch("wxclient.wechat_api.uia.time.sleep"):
            driver._choose_native_files(dialog, [r"D:\a.jpg"], pyautogui)
        pyautogui.hotkey.assert_called_once_with("alt", "o", _pause=False)


class _Panel:
    def __init__(self, gone_after: int) -> None:
        self.gone_after = gone_after
        self.polls = 0

    def exists(self, timeout: float = 0) -> bool:
        self.polls += 1
        return self.polls <= self.gone_after


class PublishSettleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = UiaWeChatDriver(dry_run=False)
        self.clock = [0.0]
        patcher = mock.patch.multiple(
            "wxclient.wechat_api.uia.time",
            sleep=lambda s: self.clock.__setitem__(0, self.clock[0] + s),
            monotonic=lambda: self.clock[0],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_面板收起了就True(self) -> None:
        self.assertTrue(self.driver._wait_publish_panel_gone(_Panel(gone_after=3)))

    def test_一直不收起就False_不抛_免得上层重发(self) -> None:
        self.assertFalse(self.driver._wait_publish_panel_gone(_Panel(gone_after=10_000)))
        self.assertGreaterEqual(self.clock[0], self.driver.MOMENTS_PUBLISH_SETTLE_SECONDS)


if __name__ == "__main__":
    unittest.main()
