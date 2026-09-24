"""对方发来的图片/文件：右键「另存为」，但**存到哪由我们填**（0.1.88）。

2026-09-24 用户：「需要我们保存，但是保存到我们的地址，我们控制选择什么」。真机 4.1.13.12 查出的：
1. 右键原来点在消息行中心——行横跨整个聊天区、中间是空白，菜单根本不弹；现在截行图按颜色找气泡。
2. 保存对话框（``#32770``）挂在微信主窗口下面，不是桌面顶层；原来只在桌面顶层里等，永远等不到。
3. 文件名框里是不带扩展名的默认名，我们换成「我们的目录 + 默认名」的完整路径；扩展名由微信补。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from wxclient.wechat_api import uia
from wxclient.wechat_api.errors import WeChatApiError

BG = (250, 250, 250)


class _Pixels:
    """PIL ``Image.load()`` 的替身：按 ``pixels[x, y]`` 取色。"""

    def __init__(self, boxes: list[tuple[int, int, int, int, tuple[int, int, int]]]) -> None:
        self.boxes = boxes

    def __getitem__(self, xy: tuple[int, int]) -> tuple[int, int, int]:
        x, y = xy
        for left, top, right, bottom, color in self.boxes:
            if left <= x < right and top <= y < bottom:
                return color
        return BG


class FindBubbleSpanTests(unittest.TestCase):
    def test_对方发的图片在左边_点气泡中心不点行中心(self) -> None:
        # 真机那一行：988x267，头像在左上，图片气泡 70..209
        pixels = _Pixels([(16, 0, 56, 40, (90, 120, 160)), (70, 0, 209, 267, (40, 60, 80))])
        self.assertEqual(uia.find_bubble_span(pixels, 988, 267), (139, 133))

    def test_自己发的在右边也认得出(self) -> None:
        pixels = _Pixels([(700, 10, 930, 80, (149, 236, 105)), (940, 0, 980, 40, (90, 120, 160))])
        x, _ = uia.find_bubble_span(pixels, 988, 90)  # type: ignore[misc]
        self.assertEqual(x, 815)

    def test_白色文件气泡在近白背景上_也不能点到头像上(self) -> None:
        # 短行（文件气泡）：中线正好穿过头像；气泡纯白、背景 (250,250,250)，色差只有 15
        pixels = _Pixels([(16, 0, 56, 60, (90, 120, 160)), (70, 5, 330, 55, (255, 255, 255))])
        x, _ = uia.find_bubble_span(pixels, 988, 60)  # type: ignore[misc]
        self.assertEqual(x, 200)

    def test_整行都是背景_认不出就回None(self) -> None:
        self.assertIsNone(uia.find_bubble_span(_Pixels([]), 988, 60))


class _Info:
    def __init__(self, class_name: str, name: str) -> None:
        self.class_name, self.name = class_name, name


class _Win:
    def __init__(self, class_name: str, name: str) -> None:
        self.element_info = _Info(class_name, name)


class FindSaveDialogTests(unittest.TestCase):
    def test_主窗口子节点里的另存为对话框认得出(self) -> None:
        # 真机 4.1.13.12 主窗口的直接子节点
        children = [
            _Win("#32770", "另存为..."),
            _Win("MMUIRenderSubWindow", "MMUIRenderSubWindow"),
            _Win("QWidget", ""),
        ]
        self.assertIs(uia.find_save_dialog(children), children[0])

    def test_别的32770不算(self) -> None:
        self.assertIsNone(uia.find_save_dialog([_Win("#32770", "打开"), _Win("Chrome_WidgetWin_1", "另存为")]))


class SavePathTests(unittest.TestCase):
    def test_我们的目录加微信给的默认名(self) -> None:
        target = Path("/data/attachments/acc/abc")
        self.assertEqual(
            uia.save_path_for(target, "微信图片_20260924171455_3963_4"),
            target / "微信图片_20260924171455_3963_4",
        )

    def test_默认名里带路径_只取文件名(self) -> None:
        target = Path("/data/x")
        self.assertEqual(uia.save_path_for(target, r"C:\Users\ZK\Desktop\简历.pdf"), target / "简历.pdf")

    def test_没有默认名_如实报错(self) -> None:
        with self.assertRaises(WeChatApiError):
            uia.save_path_for(Path("/data/x"), "  ")


class _Ctl:
    def __init__(self, control_type: str, automation_id: str) -> None:
        self.element_info = type("I", (), {"control_type": control_type, "automation_id": automation_id})()


class _Edit(_Ctl):
    def __init__(self, value: str, *, swallow: bool = False) -> None:
        super().__init__("Edit", "1001")
        self.value, self.swallow = value, swallow

    def exists(self, timeout: float = 0) -> bool:
        return True

    def get_value(self) -> str:
        return self.value

    def set_edit_text(self, text: str) -> None:
        if not self.swallow:
            self.value = text


class _Button(_Ctl):
    def __init__(self, automation_id: str) -> None:
        super().__init__("Button", automation_id)
        self.pressed = 0

    def invoke(self) -> None:
        self.pressed += 1


class _Dialog:
    """wrapper 形状的对话框：只有 ``descendants()``，**没有** ``child_window``（真机就是这样）。"""

    def __init__(self, edit: _Edit) -> None:
        self.edit, self.save, self.cancel = edit, _Button("1"), _Button("2")
        self.keys: list[str] = []

    def descendants(self) -> list[Any]:
        # 真机的列表里有好几个 Edit（名称/修改日期…都是 Edit），文件名框靠 AutomationId 认
        return [_Ctl("Edit", "System.ItemNameDisplay"), _Button("DropDown"), self.edit, self.save, self.cancel]

    def exists(self, timeout: float = 0) -> bool:
        return True

    def type_keys(self, keys: str, **_: Any) -> None:
        self.keys.append(keys)


class FillSaveDialogTests(unittest.TestCase):
    def test_文件名框填成我们的完整路径再点保存(self) -> None:
        dialog = _Dialog(_Edit("微信图片_1"))
        target = Path(tempfile.gettempdir()) / "ours"
        uia.UiaWeChatDriver._fill_save_dialog(dialog, target)
        self.assertEqual(dialog.edit.value, str(target / "微信图片_1"))
        self.assertEqual(dialog.save.pressed, 1)

    def test_读回来对不上就不点保存(self) -> None:
        dialog = _Dialog(_Edit("微信图片_1", swallow=True))
        with self.assertRaises(WeChatApiError):
            uia.UiaWeChatDriver._fill_save_dialog(dialog, Path("/data/ours"))
        self.assertEqual(dialog.save.pressed, 0, "路径没填进去就点保存 = 存到微信默认的地方")

    def test_出错收场只点取消_绝不按ESC(self) -> None:
        dialog = _Dialog(_Edit("x"))
        uia._cancel_save_dialog(dialog)
        self.assertEqual(dialog.cancel.pressed, 1)
        self.assertEqual(dialog.keys, [])


class AwaitSavedFileTests(unittest.TestCase):
    def test_微信补了扩展名_按目录里多出来的文件认(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "旧的.jpg").write_bytes(b"old")
            before = {entry.name for entry in target.iterdir()}
            (target / "微信图片_1.jpg").write_bytes(b"jpeg-bytes")
            saved = uia._await_saved_file(target, before, timeout=3)
            self.assertEqual(saved.name, "微信图片_1.jpg")

    def test_什么都没落下_超时如实报错(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            with self.assertRaises(WeChatApiError):
                uia._await_saved_file(Path(tmp), set(), timeout=0.6)
            self.assertLess(time.monotonic() - started, 3)


class SaveFlowSourceTests(unittest.TestCase):
    def test_保存附件走另存为_对话框从主窗口下找(self) -> None:
        source = Path(uia.__file__).read_text(encoding="utf-8")
        body = source[source.index("    def save_message_attachment("):source.index("    def _bubble_click_point(")]
        self.assertIn("SaveMenuItem", body)
        self.assertIn("find_save_dialog(window.children())", body)
        self.assertNotIn('"{ESC}"', body)


if __name__ == "__main__":
    unittest.main()
