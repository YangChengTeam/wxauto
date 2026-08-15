from __future__ import annotations

import unittest

from wxclient.wechat_api.service import _collect_contact_search_matches
from wxclient.wechat_api.uia import _pick_profile_name, _profile_text_lines


class _FakeItem:
    def __init__(self, text: str, class_name: str, descendants: list[str] | None = None) -> None:
        self._text = text
        self._class_name = class_name
        self._descendants = [_FakeText(value) for value in descendants or []]

    def window_text(self) -> str:
        return self._text

    def class_name(self) -> str:
        return self._class_name

    def descendants(self, **_: object) -> list[_FakeText]:
        return self._descendants


class _FakeText:
    def __init__(self, text: str) -> None:
        self._text = text

    def window_text(self) -> str:
        return self._text


class SearchContactResultTests(unittest.TestCase):
    def test_keeps_contact_cells_and_excludes_network_suggestions(self) -> None:
        matches = _collect_contact_search_matches(
            [
                _FakeItem("联系人", "mmui::XTableCell"),
                _FakeItem("大月", "mmui::SearchContentCellView"),
                _FakeItem("群聊", "mmui::XTableCell"),
                _FakeItem("大月说书", "mmui::XTableCell"),
            ],
            "大月",
            10,
        )

        self.assertEqual(
            matches,
            [
                {
                    "name": "大月",
                    "type": "contact",
                    "section": "contacts",
                    "exact": True,
                }
            ],
        )


class ProfileNamePickTests(unittest.TestCase):
    """加好友名片上挑昵称。真机那张卡片没法在 CI 里模拟，所以判定逻辑抽成纯函数在这儿钉住。

    ★ 昵称只适合给人看。后续 `send_message(to=...)` 该用的是**备注**——
    昵称会变、会重名、可能带 emoji，拿它当锚迟早对不上（见 add_friend 的返回说明）。
    """

    def test_picks_the_nickname_above_the_field_rows(self) -> None:
        lines = ["张三", "微信号：wxid_abc123", "地区：广东 深圳", "添加到通讯录"]

        self.assertEqual(_pick_profile_name(lines, "13800138000"), "张三")

    def test_an_explicit_nickname_row_wins(self) -> None:
        """有「昵称：X」就用它——明写的比猜的可信。"""
        lines = ["13800138000", "微信号：wxid_x", "昵称：李四"]

        self.assertEqual(_pick_profile_name(lines, "13800138000"), "李四")

    def test_the_searched_number_is_never_the_name(self) -> None:
        for line in ("13800138000", "+86 138-0013-8000", "手机号 13800138000"):
            with self.subTest(line=line):
                self.assertIsNone(_pick_profile_name([line, "添加到通讯录"], "13800138000"))

    def test_buttons_are_not_names(self) -> None:
        lines = ["添加到通讯录", "发消息", "搜索"]

        self.assertIsNone(_pick_profile_name(lines, "13800138000"))

    def test_only_field_rows_means_no_name_rather_than_a_wrong_one(self) -> None:
        """★ 宁可返回 None，也别把「微信号：wxid_x」当成人家的名字回给 AI 端。"""
        lines = ["微信号：wxid_x", "地区：北京", "来源：手机号搜索"]

        self.assertIsNone(_pick_profile_name(lines, "13800138000"))

    def test_emoji_and_spaces_survive(self) -> None:
        lines = ["  小王 🐰  ", "微信号：wxid_w"]

        self.assertEqual(_pick_profile_name(lines, "13800138000"), "小王 🐰")

    def test_english_client_field_labels_are_recognised(self) -> None:
        lines = ["John Smith", "WeChat ID: wxid_js", "Region: Singapore", "Add to Contacts"]

        self.assertEqual(_pick_profile_name(lines, "6598765432"), "John Smith")


class ProfileTextLinesTests(unittest.TestCase):
    def test_collects_pane_and_descendant_texts_without_duplicates(self) -> None:
        pane = _FakeItem("张三\n微信号：wxid_abc", "mmui::ContactProfileView",
                         ["张三", "地区：深圳", "地区：深圳"])

        self.assertEqual(
            _profile_text_lines(pane),
            ["张三", "微信号：wxid_abc", "地区：深圳"],
        )

    def test_a_pane_that_throws_yields_nothing_instead_of_blowing_up(self) -> None:
        class Hostile:
            def window_text(self) -> str:
                raise RuntimeError("控件没了")

            def descendants(self, **_: object) -> list[object]:
                raise RuntimeError("控件树没了")

        self.assertEqual(_profile_text_lines(Hostile()), [])
if __name__ == "__main__":
    unittest.main()
