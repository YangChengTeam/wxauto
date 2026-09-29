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

    def test_文件卡片盖住行中心_也点在卡片上不点右边空白(self) -> None:
        # 2026-09-28 真机那一行：598x151，文件卡片 (238,238,240) 从 88 到 397，盖住行中心 299。
        # 原来拿行中心当背景 → 最长「不一样」的段成了右边空白 396..598，右键点空、菜单不弹。
        pixels = _Pixels([(16, 0, 56, 40, (90, 120, 160)), (88, 0, 397, 151, (238, 238, 240))])
        self.assertEqual(uia.find_bubble_span(pixels, 598, 151), (242, 75))

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

    def test_目录里已有同名文件_不填不点保存_返回那份(self) -> None:
        """2026-09-29 用户实报：同名点保存会弹「是否替换」，整步失败。名字相同就算同一份。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "简历.pdf").write_bytes(b"x")
            dialog = _Dialog(_Edit("简历.pdf"))
            got = uia.UiaWeChatDriver._fill_save_dialog(dialog, target)
            self.assertEqual(got, target / "简历.pdf")
            self.assertEqual(dialog.save.pressed, 0)
            self.assertEqual(dialog.edit.value, "简历.pdf", "认出来就不再动文件名框")

    def test_默认名不带扩展名_认同名唯一的那份(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "微信图片_1.jpg").write_bytes(b"x")
            self.assertEqual(uia.existing_same_name(target, "微信图片_1"), target / "微信图片_1.jpg")
            (target / "微信图片_1.png").write_bytes(b"x")
            self.assertIsNone(uia.existing_same_name(target, "微信图片_1"), "两份说不清是哪份就当没有")
            self.assertIsNone(uia.existing_same_name(target, "别的"))

    def test_目录里没有才填路径点保存(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dialog = _Dialog(_Edit("简历.pdf"))
            self.assertIsNone(uia.UiaWeChatDriver._fill_save_dialog(dialog, Path(tmp)))
            self.assertEqual(dialog.save.pressed, 1)

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


class _Named:
    def __init__(self, control_type: str, name: str, automation_id: str = "", class_name: str = "") -> None:
        self.element_info = type("I", (), {"control_type": control_type, "name": name,
                                           "automation_id": automation_id, "class_name": class_name})()
        self.pressed = 0

    def invoke(self) -> None:
        self.pressed += 1


class _Confirm(_Named):
    """真机 2026-09-29 dump：「确认另存为」#32770，正文 ContentText，是/否 = CommandButton_6/7。"""

    def __init__(self, name: str) -> None:
        super().__init__("Window", "确认另存为", class_name="#32770")
        self.text = _Named("Text", f"{name} 已存在。\r\n要替换它吗?", "ContentText")
        self.yes = _Named("Button", "是(Y)", "CommandButton_6")
        self.no = _Named("Button", "否(N)", "CommandButton_7")

    def descendants(self) -> list[Any]:
        return [_Named("Image", "MainInstructionIcon", "MainIcon"), self.text, self.yes, self.no]


class _DialogWithConfirm(_Dialog):
    def __init__(self, confirm: _Confirm | None) -> None:
        super().__init__(_Edit("x"))
        self.confirm = confirm

    def children(self) -> list[Any]:
        return [self.confirm] if self.confirm is not None else []


class OverwriteConfirmTests(unittest.TestCase):
    """2026-09-29 用户实报「X 已存在。要替换它吗?」弹着不动、整条卡住：点了保存才撞同名，要自己点掉。"""

    def test_弹了确认替换_点否_关对话框_用本机那份(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "李某-个人简历(9).docx").write_bytes(b"docx")
            before = {entry.name for entry in target.iterdir()}
            confirm = _Confirm("李某-个人简历(9).docx")
            dialog = _DialogWithConfirm(confirm)
            saved = uia._await_saved_file(target, before, timeout=3, dialog=dialog)
            self.assertEqual(saved, target / "李某-个人简历(9).docx")
            self.assertEqual((confirm.no.pressed, confirm.yes.pressed), (1, 0), "不覆盖：点「否」")
            self.assertEqual(dialog.cancel.pressed, 1)

    def test_说已存在但目录里找不到_如实报错不干等(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dialog = _DialogWithConfirm(_Confirm("不在这.pdf"))
            started = time.monotonic()
            with self.assertRaises(WeChatApiError):
                uia._await_saved_file(Path(tmp), set(), timeout=3, dialog=dialog)
            self.assertLess(time.monotonic() - started, 2)

    def test_没有确认框_照常等新文件(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "新.pdf").write_bytes(b"pdf")
            dialog = _DialogWithConfirm(None)
            self.assertEqual(uia._await_saved_file(target, set(), timeout=3, dialog=dialog).name, "新.pdf")
            self.assertEqual(dialog.cancel.pressed, 0)

    def test_英文系统也认(self) -> None:
        confirm = _Confirm("x")
        confirm.text = _Named("Text", "resume.pdf already exists.\r\nDo you want to replace it?", "ContentText")
        self.assertIs(uia.find_overwrite_confirm(_DialogWithConfirm(confirm)), confirm)
        self.assertEqual(uia._overwrite_name(confirm), "resume.pdf")


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def mid_point(self) -> Any:
        class _P:
            pass

        p = _P()
        p.x, p.y = (self.left + self.right) // 2, (self.top + self.bottom) // 2  # type: ignore[attr-defined]
        return p


class _Row:
    def __init__(self, rect: _Rect, text: str = "文件\n简历.docx") -> None:
        self._rect, self._text = rect, text

    def rectangle(self) -> _Rect:
        return self._rect

    def window_text(self) -> str:
        return self._text


class _ScrollingList:
    """每滚一格，行整体往下挪 60px（滚轮向上 = 看更早的消息 = 内容往下走）。"""

    def __init__(self, row_top: int) -> None:
        self.row_top = row_top

    def rectangle(self) -> _Rect:
        return _Rect(805, 272, 1403, 733)

    def children(self, control_type: str = "") -> list[_Row]:
        return [_Row(_Rect(805, self.row_top, 1403, self.row_top + 151))]


class _Mouse:
    def __init__(self, chat_list: _ScrollingList) -> None:
        self.chat_list, self.wheels = chat_list, []

    def scroll(self, coords: Any, wheel_dist: int) -> None:
        self.wheels.append(wheel_dist)
        self.chat_list.row_top += 60 * wheel_dist


class ScrollIntoViewTests(unittest.TestCase):
    def test_行半截露在列表上面_先往上滚到整条可见(self) -> None:
        chat_list = _ScrollingList(row_top=171)  # 真机：行顶 171、列表顶 272
        mouse = _Mouse(chat_list)
        row = uia.UiaWeChatDriver._scroll_row_into_view(
            chat_list, chat_list.children()[0], "文件\n简历.docx", mouse
        )
        self.assertEqual(mouse.wheels, [1, 1])
        self.assertGreaterEqual(row.rectangle().top, 272)

    def test_本来就整条可见_一格都不滚(self) -> None:
        chat_list = _ScrollingList(row_top=400)
        mouse = _Mouse(chat_list)
        uia.UiaWeChatDriver._scroll_row_into_view(chat_list, chat_list.children()[0], "文件\n简历.docx", mouse)
        self.assertEqual(mouse.wheels, [])


class SaveFlowSourceTests(unittest.TestCase):
    def test_保存附件走另存为_对话框从主窗口下找(self) -> None:
        source = Path(uia.__file__).read_text(encoding="utf-8")
        body = source[source.index("    def save_message_attachment("):source.index("    def _bubble_click_point(")]
        self.assertIn("SaveMenuItem", body)
        self.assertIn("_cancel_save_dialog(dialog)\n                progress.done(\"本机已有同名文件", body)
        self.assertIn("find_save_dialog(window.children())", body)
        self.assertNotIn('"{ESC}"', body)
        self.assertIn("_await_saved_file(target, before, timeout=timeout_seconds, dialog=dialog)", body)
        self.assertIn("_dismiss_overwrite_confirm(dialog)\n            _cancel_save_dialog(dialog)", body)


if __name__ == "__main__":
    unittest.main()
