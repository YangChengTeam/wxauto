"""本机朋友圈巡看（`_scan_timeline`）：只收今天的、翻到上次看过的那条就停、顺手点赞受 like_limit 管。

游标是「上一趟第一条动态的键」——正文去空白，**时间戳不进键**（「5分钟前」下一趟就变「20分钟前」）。
"""

from __future__ import annotations

import re
import unittest
from typing import Any
from unittest import mock

from tests.test_uia_moments_like import OLDER, TIMESTAMP, _Item, _LikeButton, _List
from wxclient.wechat_api.uia import UiaWeChatDriver, _parse_post_text

IMAGES = re.compile(r"\s包含(\d+)张图片\s")


class ParsePostTextTests(unittest.TestCase):
    def test_正文去掉图数与时间戳_键去空白(self) -> None:
        post = _parse_post_text("张三 今天 招工 包含3张图片 5分钟前", "5分钟前", TIMESTAMP, IMAGES)
        self.assertEqual(post["content"], "张三 今天 招工")
        self.assertEqual(post["photos"], 3)
        self.assertEqual(post["videos"], 0)
        self.assertEqual(post["posted_at"], "5分钟前")
        self.assertEqual(post["key"], "张三今天招工")

    def test_同一条动态时间戳变了键不变(self) -> None:
        a = _parse_post_text("李四 新岗位 5分钟前", "5分钟前", TIMESTAMP, IMAGES)
        b = _parse_post_text("李四 新岗位 20分钟前", "20分钟前", TIMESTAMP, IMAGES)
        self.assertEqual(a["key"], b["key"])

    def test_纯图动态没正文_键取去掉时间戳的整段(self) -> None:
        post = _parse_post_text(" 包含9张图片 1小时前", "1小时前", TIMESTAMP, IMAGES)
        self.assertEqual(post["content"], "")
        self.assertEqual(post["photos"], 9)
        self.assertEqual(post["key"], "包含9张图片")

    def test_视频动态(self) -> None:
        post = _parse_post_text("王五 车间实拍 视频 3小时前", "3小时前", TIMESTAMP, IMAGES)
        self.assertEqual(post["videos"], 1)
        self.assertEqual(post["content"], "王五 车间实拍")


class ScanTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = UiaWeChatDriver(dry_run=False)
        patcher = mock.patch("wxclient.wechat_api.uia._human_pause", lambda _what: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(
        self, items: list[_Item], *, limit: int = 10, like_limit: int = 0, until_key: str = "",
    ) -> tuple[dict[str, Any], _LikeButton]:
        lst = _List(items)
        button = _LikeButton(lst)
        outcome = self.driver._scan_timeline(
            lst, button,
            timestamp=TIMESTAMP, images_pattern=IMAGES, older_marks=OLDER,
            click_ellipsis=lambda _rect: None,
            limit=limit, like_limit=like_limit, until_key=until_key,
        )
        return outcome, button

    def test_第一趟没有游标_收今天的全部_碰到昨天停(self) -> None:
        outcome, button = self._run([
            _Item("张三 今天招工 包含3张图片 5分钟前"),
            _Item("李四 新到一批岗位 2小时前"),
            _Item("王五 上周的事 昨天"),
        ])
        self.assertEqual([p["content"] for p in outcome["posts"]], ["张三 今天招工", "李四 新到一批岗位"])
        self.assertEqual(outcome["first_key"], "张三今天招工")
        self.assertEqual(outcome["stopped"], "older")
        self.assertEqual(outcome["liked"], 0, "like_limit=0 一个都不点")
        self.assertEqual(button.clicked, [])

    def test_翻到上次看过的那条就停_只收新的(self) -> None:
        outcome, _button = self._run([
            _Item("赵六 刚发的 3分钟前"),
            _Item("张三 今天招工 包含3张图片 40分钟前"),  # 上一趟的第一条，时间戳已经变了
            _Item("李四 新到一批岗位 2小时前"),
        ], until_key="张三今天招工")
        self.assertEqual([p["content"] for p in outcome["posts"]], ["赵六 刚发的"])
        self.assertEqual(outcome["stopped"], "cursor")
        self.assertEqual(outcome["first_key"], "赵六刚发的", "游标要前移到这一趟的第一条")

    def test_没有新动态_第一条就是游标(self) -> None:
        outcome, _button = self._run([
            _Item("张三 今天招工 5分钟前"),
        ], until_key="张三今天招工")
        self.assertEqual(outcome["posts"], [])
        self.assertEqual(outcome["stopped"], "cursor")
        self.assertEqual(outcome["first_key"], "张三今天招工")

    def test_顺手点赞只点新的_受like_limit管_已点过的跳过(self) -> None:
        outcome, button = self._run([
            _Item("A 1分钟前"),
            _Item("B 2分钟前", liked=True),
            _Item("C 3分钟前"),
            _Item("D 4分钟前"),
            _Item("旧的 10分钟前"),
        ], like_limit=2, until_key="旧的")
        self.assertEqual(button.clicked, ["A 1分钟前", "C 3分钟前"])
        self.assertEqual(outcome["liked"], 2)
        self.assertEqual(outcome["already_liked"], 1)
        self.assertEqual(len(outcome["posts"]), 4, "点赞额度用完了仍然继续收动态")
        self.assertEqual(outcome["stopped"], "cursor")

    def test_评论区_广告_没时间戳的都跳过(self) -> None:
        outcome, _button = self._run([
            _Item("评论", "mmui::TimelineCommentCell"),
            _Item("广告", "mmui::TimelineAdGridImageCell"),
            _Item("没有时间戳的杂项"),
            _Item("真动态 8分钟前"),
        ])
        self.assertEqual([p["content"] for p in outcome["posts"]], ["真动态"])

    def test_收满limit就停(self) -> None:
        outcome, _button = self._run([_Item(f"P{i} {i + 1}分钟前") for i in range(6)], limit=3)
        self.assertEqual(len(outcome["posts"]), 3)
        self.assertEqual(outcome["stopped"], "limit")

    def test_dry_run空壳不开窗(self) -> None:
        driver = UiaWeChatDriver(dry_run=True)
        outcome = driver.scan_recent_moments(limit=10, like_limit=5, until_key="x")
        self.assertTrue(outcome["dry_run"])
        self.assertEqual(outcome["posts"], [])
        self.assertEqual(outcome["stopped"], "dry_run")


if __name__ == "__main__":
    unittest.main()
