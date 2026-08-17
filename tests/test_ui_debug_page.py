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
from concurrent.futures import Future
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
    from wxclient.ui.debug_page import AI_NAME, PLACEHOLDER, DebugPage, _CommandCard, _SlimScroll
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

    def test_the_header_has_no_new_session_button(self) -> None:
        """★ 没有「新建会话」这回事（用户 2026-08-17）：这一页就骑网关那条已连接的。"""
        header = self.page.build_header_actions(self.root)
        texts = " ".join(child.cget("text") for child in _walk(header)
                         if isinstance(child, tk.Label))

        self.assertNotIn("新会话", texts)
        self.assertFalse(hasattr(self.page, "reset"), "连入口都不该留着")

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

    # ── 回执与卡的先后 ────────────────────────────────────────────

    def _receipt(self, op: str = "post_moments", status: str = "done", command_id: str = "c1") -> dict:
        return {"kind": "wechat_command_result", "command_id": command_id, "op": op, "status": status}

    def test_a_receipt_that_beats_its_card_lands_on_that_card(self) -> None:
        """★ 一条指令一张卡（2026-08-17 用户反馈：「post 成功，下面还写着待下发」）。

        真因是先后：指令一 emit 出来 AI 端就下发了，网关十几秒就报回执，而这一轮的卡
        **要等整轮收口才画**。回执先到、找不到卡 → 另起一张写「执行成功」，随后真卡
        又停在「待下发」——同一条指令占了两张卡，还各说各话。
        """
        self.page._future = Future()  # 这一轮还在跑

        self.page._render_receipt(self._receipt())

        cards = [c for c in self.page._list.winfo_children() if isinstance(c, _CommandCard)]
        self.assertEqual(cards, [], "回执跑在前头时不该另起一张卡")

        card = self.page._add_command_card("post_moments（content=招 3 个普工）")

        self.assertIn("执行成功", card._status.cget("text"), "卡一画出来就该认领那条回执")
        self.assertIs(card._icon._ok, True)
        self.assertEqual(
            len([c for c in self.page._list.winfo_children() if isinstance(c, _CommandCard)]), 1,
            "全程只有一张卡",
        )

    def test_the_whole_receipt_run_lands_on_the_one_card(self) -> None:
        """★ 一条指令的回执是**一串**：received → started → done（真机日志逐条可见）。

        只领第一条的话，卡会停在「已收到，排队中」，后面 started/done 还各自另起一张——
        用户看到的是「待下发/排队中，其实早就发出去了」（2026-08-17）。
        """
        self.page._future = Future()
        for status in ("received", "started", "done"):
            self.page._render_receipt(self._receipt(op="send_message", status=status))

        card = self.page._add_command_card("send_message（to=张三，text=面试改到明天）")

        self.assertIn("执行成功", card._status.cget("text"), "落到的应该是**最后**那条")
        self.assertIs(card._icon._ok, True)
        self.assertEqual(
            len([c for c in self.page._list.winfo_children() if isinstance(c, _CommandCard)]), 1,
            "三条回执一张卡，不是三张",
        )

    def test_a_settled_card_stops_claiming_later_receipts(self) -> None:
        """收口过的卡要退出认领队列，别把下一条指令的回执抢过去。"""
        self.page._future = Future()
        self.page._render_receipt(self._receipt(op="send_message", status="done"))
        first = self.page._add_command_card("send_message（to=张三）")
        self.assertEqual(self.page._open_cards, [])

        self.page._future = None
        self.page._render_receipt(self._receipt(op="send_message", status="failed", command_id="c2"))

        self.assertIn("执行成功", first._status.cget("text"), "第一张卡不该被后来的回执改写")

    def test_a_stale_card_never_steals_the_next_turns_receipt(self) -> None:
        """★ 上一轮没收口的卡不许接下一轮的回执（2026-08-17 真机：新卡永远停在「待下发」）。

        老卡停在那儿是因为它那条指令的终态**永远不会来了**（网关掉线、根本没下发）。
        只按 op 配对的话，队列里排在前面的它会把后面每一条回执都截走。
        """
        self.page._turn_seq = 1
        stale = self.page._add_command_card("send_message（to=上一轮的人）")
        self.page._render_receipt(self._receipt(op="send_message", status="received", command_id="old"))
        self.assertIn("已收到", stale._status.cget("text"), "前置：老卡确实还开着、且已绑到 old")

        self.page._turn_seq = 2
        fresh = self.page._add_command_card("send_message（to=这一轮的人）")
        for status in ("received", "started", "done"):
            self.page._render_receipt(self._receipt(op="send_message", status=status, command_id="new"))

        self.assertIn("执行成功", fresh._status.cget("text"), "★ 这一轮的回执要落到这一轮的卡上")
        self.assertIn("已收到", stale._status.cget("text"), "老卡保持原样——它的结果确实没来过")

    def test_an_unclaimed_receipt_still_shows_up(self) -> None:
        """没人认领的回执（排期到点执行、上一轮的指令）照样摆出来——不能吞。"""
        self.page._future = Future()
        self.page._render_receipt(self._receipt(op="send_message"))

        self.page._future = None
        self.page._render_reply(_Turn())

        cards = [c for c in self.page._list.winfo_children() if isinstance(c, _CommandCard)]
        self.assertEqual(len(cards), 1)
        self.assertIn("执行成功", cards[0]._status.cget("text"))

    def test_a_receipt_with_no_turn_in_flight_gets_its_own_card(self) -> None:
        """不在轮次里的回执直接补一张卡（老行为，别回归）。"""
        self.page._render_receipt(self._receipt(op="send_message"))

        cards = [c for c in self.page._list.winfo_children() if isinstance(c, _CommandCard)]
        self.assertEqual(len(cards), 1)
        self.assertIn("执行成功", cards[0]._status.cget("text"))

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


class _Turn:
    """`TurnResult` 的形状：一轮什么都没产出（本文件只关心回执那半截）。"""

    error = None
    text = ""
    card = None


class _Event:
    def __init__(self, y: int) -> None:
        self.y = y


def _walk(widget: object):
    yield widget
    for child in widget.winfo_children():  # type: ignore[attr-defined]
        yield from _walk(child)


if __name__ == "__main__":  # pragma: no cover - 手动跑单个文件时用
    unittest.main()
