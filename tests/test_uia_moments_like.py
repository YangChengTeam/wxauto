"""朋友圈点赞（时间线形态）：只点今天的、已点过的不再点、翻到底就停。

用户 2026-09-07 定的形态：**刷自己的朋友圈时间线**，不逐个好友翻。逐个好友那条路是
搜人 → 开资料 → 进他的相册，和加好友前搜号同形，正是风控最盯的动作。

这里只测 `_like_timeline` 这段纯 UIA 逻辑，控件全是假的；真机上「省略号点开弹的是
『赞』还是『取消』」这一判据要另行核对（vendor `Buttons.LikeButton` 按标题认）。
"""

from __future__ import annotations

import re
import unittest
from typing import Any
from unittest import mock

from wxclient.wechat_api.uia import UiaWeChatDriver

TIMESTAMP = re.compile(r"\d+分钟前|\d+小时前|昨天|\d+天前")
OLDER = ("昨天", "天前")


class _Rect:
    right = 800
    bottom = 600


class _Item:
    def __init__(self, text: str, class_name: str = "mmui::TimelineContentCell", *, liked: bool = False) -> None:
        self._text = text
        self._class = class_name
        self.liked = liked
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
    """按方向键移动焦点；到底了焦点就不动——真机上就是这样。"""

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


class _LikeButton:
    """省略号弹出来的那颗「赞」。已经点过的动态弹的是「取消」，这颗就**不存在**。"""

    def __init__(self, lst: _List) -> None:
        self._list = lst
        self.clicked: list[str] = []

    def exists(self, **_: object) -> bool:
        item = self._list.current()
        return item is not None and not item.liked

    def click_input(self) -> None:
        item = self._list.current()
        assert item is not None
        item.liked = True
        self.clicked.append(item.window_text())


class LikeTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = UiaWeChatDriver(dry_run=False)
        patcher = mock.patch("wxclient.wechat_api.uia._human_pause", lambda _what: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ellipsis_clicks: list[Any] = []

    def _run(self, items: list[_Item], limit: int = 20) -> tuple[dict[str, Any], _LikeButton]:
        lst = _List(items)
        button = _LikeButton(lst)
        outcome = self.driver._like_timeline(
            lst, button,
            timestamp=TIMESTAMP, older_marks=OLDER,
            click_ellipsis=lambda rect: self.ellipsis_clicks.append(rect),
            limit=limit,
        )
        return outcome, button

    def test_只点今天的_碰到昨天就停(self) -> None:
        outcome, button = self._run([
            _Item("张三 今天招工 包含3张图片 5分钟前"),
            _Item("李四 新到一批岗位 2小时前"),
            _Item("王五 上周的事 昨天"),
            _Item("赵六 更早 3天前"),
        ])
        self.assertEqual(button.clicked, ["张三 今天招工 包含3张图片 5分钟前", "李四 新到一批岗位 2小时前"])
        self.assertEqual(outcome["liked"], 2)
        self.assertEqual(outcome["stopped"], "older", "时间线按时间排，碰到昨天就该收手")

    def test_已经点过的不再点(self) -> None:
        """★ 再点一下就是**取消赞**：对方手机先弹「XX 赞了你」再消失，比不点还糟。"""
        outcome, button = self._run([
            _Item("张三 5分钟前", liked=True),
            _Item("李四 10分钟前"),
        ])
        self.assertEqual(button.clicked, ["李四 10分钟前"])
        self.assertEqual(outcome["already_liked"], 1)
        self.assertEqual(outcome["scanned"], 2)

    def test_评论区_广告_余下x条都跳过(self) -> None:
        _outcome, button = self._run([
            _Item("张三 5分钟前"),
            _Item("李四：好的 5分钟前", "mmui::TimelineCommentCell"),
            _Item("余下 3 条", "mmui::TimelineCell"),
            _Item("广告 1小时前", "mmui::TimelineAdGridImageCell"),
            _Item("王五 30分钟前"),
        ])
        self.assertEqual(button.clicked, ["张三 5分钟前", "王五 30分钟前"])
        self.assertEqual(len(self.ellipsis_clicks), 2, "不是动态的项连省略号都不该点")

    def test_认不出时间的项不碰(self) -> None:
        _outcome, button = self._run([
            _Item("封面/头像那一块，没有时间"),
            _Item("张三 5分钟前"),
        ])
        self.assertEqual(button.clicked, ["张三 5分钟前"])

    def test_点满上限就停(self) -> None:
        items = [_Item(f"好友{i} {i + 1}分钟前") for i in range(10)]
        outcome, button = self._run(items, limit=3)
        self.assertEqual(len(button.clicked), 3)
        self.assertEqual(outcome["stopped"], "limit")

    def test_翻到底就停_不会死循环(self) -> None:
        outcome, button = self._run([
            _Item("张三 5分钟前"),
            _Item("李四 10分钟前"),
        ])
        self.assertEqual(len(button.clicked), 2)
        self.assertEqual(outcome["stopped"], "end")
        self.assertEqual(outcome["liked"], 2, "到底后焦点不动，同一条不许再点第二次")

    def test_空时间线什么都不做(self) -> None:
        outcome, button = self._run([])
        self.assertEqual(button.clicked, [])
        self.assertEqual(outcome, {"liked": 0, "scanned": 0, "already_liked": 0, "stopped": "end"})


class DryRunTests(unittest.TestCase):
    def test_只走流程时一下微信都不碰(self) -> None:
        driver = UiaWeChatDriver(dry_run=True)
        with mock.patch.object(driver, "_ensure_ready", side_effect=AssertionError("不该探微信")):
            result = driver.like_recent_moments(limit=5)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["liked"], 0)


if __name__ == "__main__":
    unittest.main()
