"""朋友圈评论（时间线形态）：按正文认动态、只评今天的、认不到的一条都不评。

用户 2026-09-07：评论由业务端计划触发，文字由 AI 端按计划素材现写；本机只管
「认出那条动态、把字贴上去、按量」。这里只测 `_comment_timeline` 这段纯 UIA 逻辑，
控件全是假的；真机上「省略号点开有没有『评论』」「发送键那块绿」要另行核对。
"""

from __future__ import annotations

import re
import unittest
from typing import Any
from unittest import mock

from wxclient.wechat_api.uia import UiaWeChatDriver, _normalize_comment_requests

TIMESTAMP = re.compile(r"\d+分钟前|\d+小时前|昨天|\d+天前")
OLDER = ("昨天", "天前")


class _Rect:
    right = 800
    bottom = 600


class _Item:
    def __init__(self, text: str, class_name: str = "mmui::TimelineContentCell", *, commentable: bool = True) -> None:
        self._text = text
        self._class = class_name
        self.commentable = commentable
        self.focused = False

    def has_keyboard_focus(self) -> bool:
        return self.focused

    def class_name(self) -> str:
        return self._class

    def window_text(self) -> str:
        return self._text

    @staticmethod
    def rectangle() -> _Rect:
        return _Rect()


class _List:
    def __init__(self, items: list[_Item]) -> None:
        self.items = items
        self.index = -1

    def type_keys(self, keys: str, **_: object) -> None:
        if keys == "{HOME}":
            self.index = -1
        elif keys == "{DOWN}":
            self.index = min(self.index + 1, len(self.items) - 1)
        for i, item in enumerate(self.items):
            item.focused = i == self.index

    def children(self, control_type: str = "") -> list[_Item]:
        return list(self.items)

    def current(self) -> _Item | None:
        return self.items[self.index] if 0 <= self.index < len(self.items) else None


class _CommentButton:
    """省略号弹出来的那颗「评论」。不让评的动态这颗就**不存在**。"""

    def __init__(self, lst: _List) -> None:
        self._list = lst
        self.clicked: list[str] = []

    def exists(self, **_: object) -> bool:
        item = self._list.current()
        return item is not None and item.commentable

    def click_input(self) -> None:
        item = self._list.current()
        assert item is not None
        self.clicked.append(item.window_text())


class CommentTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = UiaWeChatDriver(dry_run=False)
        patcher = mock.patch("wxclient.wechat_api.uia._human_pause", lambda _what: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.typed: list[str] = []
        self.sent: list[str] = []
        self.ellipsis_clicks: list[Any] = []

    def _run(self, items: list[_Item], comments: list[dict[str, str]]) -> tuple[dict[str, Any], _CommentButton]:
        lst = _List(items)
        button = _CommentButton(lst)
        outcome = self.driver._comment_timeline(
            lst, button,
            timestamp=TIMESTAMP, older_marks=OLDER,
            click_ellipsis=lambda rect: self.ellipsis_clicks.append(rect),
            type_text=self.typed.append,
            click_send=lambda item: self.sent.append(item.window_text()),
            comments=_normalize_comment_requests(comments, 5),
        )
        return outcome, button

    def test_按正文认动态_只评认到的那几条(self) -> None:
        outcome, button = self._run(
            [
                _Item("张三 今天招工 包含3张图片 5分钟前"),
                _Item("李四 新到一批岗位，包吃住 2小时前"),
                _Item("王五 周末去爬山 3小时前"),
            ],
            [
                {"content": "新到一批岗位，包吃住", "text": "包吃住太香了，求岗位详情"},
                {"content": "周末去爬山", "text": "好地方，注意安全"},
            ],
        )
        self.assertEqual(button.clicked, ["李四 新到一批岗位，包吃住 2小时前", "王五 周末去爬山 3小时前"])
        self.assertEqual(self.typed, ["包吃住太香了，求岗位详情", "好地方，注意安全"])
        self.assertEqual(self.sent, button.clicked, "每条评论贴完都要按一次发送")
        self.assertEqual(outcome["commented"], 2)
        self.assertEqual(outcome["commented_contents"], ["新到一批岗位，包吃住", "周末去爬山"])
        self.assertEqual(outcome["missed"], [])
        self.assertEqual(outcome["stopped"], "all", "要评的都评完了就收手，不把时间线翻到底")
        self.assertEqual(len(self.ellipsis_clicks), 2, "没认到的动态连省略号都不该点")

    def test_正文里空白位置不一样也认得出(self) -> None:
        outcome, _button = self._run(
            [_Item("张三 新到一批\n岗位，包 吃住 5分钟前")],
            [{"content": "新到一批岗位，包吃住", "text": "求详情"}],
        )
        self.assertEqual(outcome["commented"], 1)

    def test_认不到的一条都不评_列在_missed(self) -> None:
        """★ 评错一条动态比漏评十条难看：宁可漏。"""
        outcome, button = self._run(
            [_Item("张三 今天招工 5分钟前")],
            [{"content": "这段正文时间线上根本没有", "text": "评论"}],
        )
        self.assertEqual(button.clicked, [])
        self.assertEqual(self.typed, [])
        self.assertEqual(outcome["commented"], 0)
        self.assertEqual(outcome["missed"], ["这段正文时间线上根本没有"])
        self.assertEqual(outcome["stopped"], "end")

    def test_只评今天的_碰到昨天就停(self) -> None:
        outcome, _button = self._run(
            [
                _Item("张三 今天招工 5分钟前"),
                _Item("王五 上周的事 昨天"),
            ],
            [{"content": "上周的事", "text": "评论"}],
        )
        self.assertEqual(outcome["commented"], 0)
        self.assertEqual(outcome["stopped"], "older")
        self.assertEqual(outcome["missed"], ["上周的事"])

    def test_省略号点开没有评论按钮就跳过_记_missed(self) -> None:
        outcome, button = self._run(
            [_Item("张三 今天招工 5分钟前", commentable=False), _Item("李四 新岗位 10分钟前")],
            [{"content": "今天招工", "text": "a"}, {"content": "新岗位", "text": "b"}],
        )
        self.assertEqual(button.clicked, ["李四 新岗位 10分钟前"])
        self.assertEqual(outcome["commented"], 1)
        self.assertEqual(outcome["matched"], 2)
        self.assertEqual(outcome["missed"], ["今天招工"])

    def test_评论区_广告_余下x条都跳过(self) -> None:
        outcome, button = self._run(
            [
                _Item("张三 今天招工 5分钟前"),
                _Item("李四：今天招工 5分钟前", "mmui::TimelineCommentCell"),
                _Item("余下 3 条", "mmui::TimelineCell"),
            ],
            [{"content": "今天招工", "text": "评论"}],
        )
        self.assertEqual(button.clicked, ["张三 今天招工 5分钟前"])
        self.assertEqual(outcome["commented"], 1)

    def test_同一段正文只评一次(self) -> None:
        """两个好友转了同一条，只评先看到的那条；清单里重复的也只算一条。"""
        outcome, button = self._run(
            [_Item("张三 同一条广告 5分钟前"), _Item("李四 同一条广告 8分钟前")],
            [{"content": "同一条广告", "text": "a"}, {"content": "同一条广告", "text": "b"}],
        )
        self.assertEqual(button.clicked, ["张三 同一条广告 5分钟前"])
        self.assertEqual(outcome["commented"], 1)

    def test_翻到底就停_不会死循环(self) -> None:
        outcome, _button = self._run(
            [_Item("张三 5分钟前"), _Item("李四 10分钟前")],
            [{"content": "王五没发", "text": "x"}],
        )
        self.assertEqual(outcome["stopped"], "end")
        self.assertEqual(outcome["scanned"], 2)


class NormalizeTests(unittest.TestCase):
    def test_空的_太短的_重复的都丢_超过上限截断(self) -> None:
        got = _normalize_comment_requests([
            {"content": "  ", "text": "a"},
            {"content": "一", "text": "太短"},
            {"content": "好岗位", "text": ""},
            {"content": "好岗位", "text": "留下"},
            {"content": "好 岗位", "text": "重复"},
            "不是字典",
            {"content": "第二条", "text": "b"},
            {"content": "第三条", "text": "c"},
        ], 2)
        self.assertEqual([c["content"] for c in got], ["好岗位", "第二条"])
        self.assertEqual(got[0]["key"], "好岗位")

    def test_不是列表就当空(self) -> None:
        self.assertEqual(_normalize_comment_requests("x", 5), [])


class DryRunTests(unittest.TestCase):
    def test_只走流程时一下微信都不碰(self) -> None:
        driver = UiaWeChatDriver(dry_run=True)
        with mock.patch.object(driver, "_ensure_ready", side_effect=AssertionError("不该探微信")):
            result = driver.comment_recent_moments([{"content": "今天招工", "text": "求详情"}])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["commented"], 0)
        self.assertEqual(result["missed"], ["今天招工"])


if __name__ == "__main__":
    unittest.main()
