"""调试会话这一页的界面约束（照 `docs/chat.png`）。

**为什么单独一个文件**：要真建 Tk，和 `tests/test_ui_window.py` 同一个路数——没有 Tk 的
地方整体跳过，Windows 真机上 `python -m unittest discover -s tests` 会照常带上。

盯的是三件用户看得见、而且已经错过一次的事（2026-08-17 真机截图 + 用户反馈）：
① 对面那位**不叫「微信助手」**——那是本机这个程序自己的名字，两边同名就分不清谁在说话；
② 头像不能拿品牌图标缩小了顶——细线条橙色标志缩到 30px 只剩几道红杠（「图像是红线」）；
③ 状态**只跟着真回执走**：自己那句话的下场、指令卡的下场，都不许提前画成功。
"""

from __future__ import annotations

import unittest
from unittest import mock

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    HAS_TK = True
except Exception:  # noqa: BLE001 - 没有 Tk / 没有显示环境都算「跑不了」
    HAS_TK = False

if HAS_TK:
    from wxclient.ui import debug_page as debug_page_module
    from wxclient.ui import theme
    from wxclient.ui.debug_page import AI_NAME, PLACEHOLDER, DebugPage, _SlimScroll
    from wxclient.ui.service import GatewayService

#: 建过的窗口留到进程结束——理由和 test_ui_window.py 顶上那段一样（Tcl_AsyncDelete）。
_KEEP_ALIVE: list = []


def setUpModule() -> None:
    import gc

    gc.disable()


def tearDownModule() -> None:
    import gc

    gc.enable()


class _Receipt:
    """`debugchat.DebugReceipt` 的形状，测试里现造一个免得跟真流程绕。"""

    def __init__(self, ok: bool | None, label: str, reason: str = "") -> None:
        self.op = "send_message"
        self.ok = ok
        self.label = label
        self.reason = reason
        self.settled = ok is not None


@unittest.skipUnless(HAS_TK, "没有 Tk")
class DebugPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        _KEEP_ALIVE.append(self.root)
        self.page = DebugPage(self.root, GatewayService(lambda: None))
        self.addCleanup(self.root.destroy)

    # ── 名字 ──────────────────────────────────────────────────────

    def test_the_other_side_is_not_called_the_client_itself(self) -> None:
        """★ 对面是 AI 端，不是这台电脑上的「微信助手」（用户 2026-08-17）。"""
        self.assertNotIn("微信助手", AI_NAME)
        self.assertIn(AI_NAME, PLACEHOLDER)

        texts = [child.cget("text") for child in _walk(self.page._list)
                 if isinstance(child, tk.Label)]
        self.assertIn(AI_NAME, texts, "开场白那一行要署 AI 端的名字")
        self.assertNotIn("微信助手", " ".join(texts))

    def test_the_opening_has_no_try_this_line(self) -> None:
        """★ 开场只留一句话——底下那行「试试：给张三…」用户砍了（2026-08-17）。"""
        texts = " ".join(child.cget("text") for child in _walk(self.page._list)
                         if isinstance(child, tk.Label))
        self.assertNotIn("试试", texts)
        self.assertNotIn("张三", texts)

    # ── 头像 ──────────────────────────────────────────────────────

    def test_the_avatar_is_the_brand_logo_at_full_size(self) -> None:
        """★ 头像 = 品牌 logo，而且**摆得下整张图**（用户 2026-08-17：「显示不全，一条线」）。

        真因是那一格 `pack_propagate(False)` 只给了 width：高度退回默认 1px，
        图被裁成一条横线。所以这里断言的是**那一格的高度**，不是「调了哪个函数」。
        """
        with mock.patch.object(debug_page_module.icons, "logo_photo") as logo:
            logo.return_value = None
            self.page._say_ai("在的")

        logo.assert_called_once()
        self.assertEqual(logo.call_args.args[1], debug_page_module._AVATAR)

        row = self.page._list.winfo_children()[-1]
        holder = row.winfo_children()[0]
        self.assertGreaterEqual(holder.cget("height"), debug_page_module._AVATAR,
                                "头像那一格没给高度 → 图会被裁成一条线")

    # ── 「说真话」──────────────────────────────────────────────────

    def test_my_bubble_stays_unmarked_until_the_turn_settles(self) -> None:
        """★ 发出去 ≠ 到了。勾要等这一轮真收口才画。"""
        mark = self.page._say_me("给张三发条消息")
        self.assertEqual(mark.cget("text").strip(), "", "在途的时候不画任何下场")

        self.page._my_mark = mark
        self.page._mark_turn("✓", theme.GREEN)
        self.assertEqual(mark.cget("text"), "✓")
        self.assertEqual(str(mark.cget("fg")), theme.GREEN)

    def test_a_failed_turn_is_marked_with_a_cross(self) -> None:
        mark = self.page._say_me("发一条朋友圈")
        self.page._my_mark = mark
        self.page._events.put(("receipt", {}))  # 无关事件不该干扰
        self.page._mark_turn("×", theme.RED)

        self.assertEqual(mark.cget("text"), "×")
        self.assertEqual(str(mark.cget("fg")), theme.RED)

    def test_a_command_card_starts_pending_and_only_a_receipt_moves_it(self) -> None:
        """★ 卡里有指令只代表 AI 打算做，不代表下发了（2026-08-17 事故）。"""
        card = self.page._add_command_card("send_message（to=张三）")

        self.assertIn("待下发", card._status.cget("text"))
        self.assertIsNone(card._icon._ok, "起手是空心圈，不涂绿")

        card.apply(_Receipt(False, "没有下发", "微信端没有在线的网关"))

        self.assertIn("没有下发", card._status.cget("text"))
        self.assertIs(card._icon._ok, False)
        self.assertIn("没有在线的网关", card._reason.cget("text"))
        self.assertTrue(card._reason.winfo_manager(), "原因要真摆出来")

    def test_an_unknown_status_never_gets_painted_as_success(self) -> None:
        card = self.page._add_command_card("send_message")
        card.apply(_Receipt(None, "已收到，排队中"))

        self.assertIsNone(card._icon._ok)
        self.assertEqual(str(card._op.cget("fg")), theme.TEXT, "没成没败，名字不上色")


@unittest.skipUnless(HAS_TK, "没有 Tk")
class SlimScrollTests(unittest.TestCase):
    """细滚动条：内容没超出就一根线都不画（设计稿里也没有）。"""

    def setUp(self) -> None:
        self.root = tk.Tk()
        _KEEP_ALIVE.append(self.root)
        self.moved: list[float] = []
        self.scroll = _SlimScroll(self.root, self.moved.append)
        self.scroll.pack(fill=tk.Y, expand=True)
        self.root.update_idletasks()
        self.addCleanup(self.root.destroy)

    def test_nothing_is_drawn_when_everything_fits(self) -> None:
        self.scroll.set(0.0, 1.0)
        self.assertEqual(self.scroll.find_all(), ())

    def test_a_thumb_shows_up_once_it_overflows(self) -> None:
        self.scroll.set(0.0, 0.4)
        self.assertTrue(self.scroll.find_all())

    def test_dragging_asks_the_canvas_to_move(self) -> None:
        self.scroll.set(0.2, 0.6)
        self.scroll._jump(_Event(y=0))

        self.assertEqual(len(self.moved), 1)
        self.assertGreaterEqual(self.moved[0], 0.0)
        self.assertLessEqual(self.moved[0], 1.0)


class _Event:
    def __init__(self, y: int) -> None:
        self.y = y


def _walk(widget: object):
    yield widget
    for child in widget.winfo_children():  # type: ignore[attr-defined]
        yield from _walk(child)


if __name__ == "__main__":  # pragma: no cover - 手动跑单个文件时用
    unittest.main()
