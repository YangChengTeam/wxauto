from __future__ import annotations

import unittest

from wxclient.wechat_api.service import _collect_contact_search_matches


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


if __name__ == "__main__":
    unittest.main()
