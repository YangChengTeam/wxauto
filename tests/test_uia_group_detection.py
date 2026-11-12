"""是不是群：只认微信自己标群人数的那个控件，不看聊天内容长什么样。

原来窗口里任何一行形如 ``xxx(12)`` 的文字都算群——对方发一句「我想报名(2)」，这个单聊就被判成
群：多行消息的第一行被当成发送人丢掉，这个人还被记成群（`gateway_peer_kinds`）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from wxclient.wechat_api.uia import _window_has_group_count_label

_PREFIX = "content_view.top_content_view.title_h_view.left_v_view.left_content_v_view.left_ui_.big_title_line_h_view."


def _node(text: str, auto_id: str = "") -> SimpleNamespace:
    return SimpleNamespace(window_text=lambda: text, element_info=SimpleNamespace(automation_id=auto_id))


def _window(title: str, *nodes: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(window_text=lambda: title, descendants=lambda: list(nodes))


class GroupDetectionTests(unittest.TestCase):
    def test_a_message_that_looks_like_a_count_does_not_make_it_a_group(self) -> None:
        window = _window(
            "微信",
            _node("张三", _PREFIX + "current_chat_name_label"),
            _node("我想报名(2)"),
            _node("还有名额吗(3)", "chat_message_list.item"),
        )
        self.assertFalse(_window_has_group_count_label(window))

    def test_the_member_count_label_makes_it_a_group(self) -> None:
        window = _window(
            "微信",
            _node("招聘交流群", _PREFIX + "current_chat_name_label"),
            _node("(128)", _PREFIX + "current_chat_count_label"),
        )
        self.assertTrue(_window_has_group_count_label(window))

    def test_the_count_label_is_recognised_under_any_prefix(self) -> None:
        # 独立聊天窗口里同一个控件的 auto_id 前缀不一样，按后缀认。
        window = _window("招聘交流群", _node("(9)", "separate_window.title.current_chat_count_label"))
        self.assertTrue(_window_has_group_count_label(window))

    def test_a_count_label_with_other_text_is_ignored(self) -> None:
        window = _window("微信", _node("张三", _PREFIX + "current_chat_count_label"))
        self.assertFalse(_window_has_group_count_label(window))


if __name__ == "__main__":
    unittest.main()
