"""搜索完怎么收场——**绝不能把人家的微信关进托盘**。

2026-08-17 用户实报：「用搜索去找的话，微信就直接关闭到右下角了」。
真凶是收尾那一下 `{ESC}`：先 `set_text("")` 把搜索框清空，再按 ESC——而微信 4.x 里
搜索框空着时 ESC 就是**关窗口**。收进托盘之后微信会把 UIA 子树收走（见
`UiaWeChatDriver._ensure_ready` 的注释），下一条指令、下一次读消息都得先把窗口捞回来。

所以这个文件盯的不是「搜到了谁」（那在 test_search_contacts.py），而是**收场动作本身**。
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from wxclient.wechat_api.uia import UiaWeChatDriver


class _Node:
    """记录自己被怎么摆弄过的假控件。所有交互都记进共享的 `log`。"""

    def __init__(self, name: str, log: list[tuple[str, str]], children: dict[str, Any] | None = None) -> None:
        self._name = name
        self._log = log
        self._children = children or {}

    def click_input(self) -> None:
        self._log.append((self._name, "click"))

    def set_text(self, text: str) -> None:
        self._log.append((self._name, f"set_text:{text}"))

    def type_keys(self, keys: str, **_: object) -> None:
        self._log.append((self._name, f"type_keys:{keys}"))

    def exists(self, **_: object) -> bool:
        return True

    def descendants(self, **_: object) -> list[Any]:
        return []

    def child_window(self, **kwargs: object) -> Any:
        return self._children.get(str(kwargs.get("role")), _Node("unknown", self._log))


class _Elements:
    """`Uielements` 的形状：本用例只需要这三处定位符，用 role 标出来好认。"""

    class SideBar:
        Weixin = {"role": "chat_button"}

    class Edits:
        SearchEdit = {"role": "search_edit"}

    class Lists:
        SearchResult = {"role": "search_results"}


class SearchCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.search_edit = _Node("search_edit", self.log)
        self.chat_button = _Node("chat_button", self.log)
        self.results = _Node("search_results", self.log)
        main_window = _Node("main", self.log, {
            "chat_button": self.chat_button,
            "search_results": self.results,
        })
        main_window.descendants = lambda **_: [self.search_edit]  # type: ignore[method-assign]

        navigator = mock.Mock()
        navigator.open_weixin.return_value = main_window
        self.driver = UiaWeChatDriver(dry_run=False)
        self.driver._ensure_ready = lambda: None  # type: ignore[method-assign]
        self.driver._pyweixin_class = lambda name: navigator  # type: ignore[method-assign]
        self.driver._pyweixin_submodule = lambda name: _Elements  # type: ignore[method-assign]

    def test_it_never_presses_escape(self) -> None:
        """★ ESC = 关窗口。搜完人微信就没了，这是用户看到的那一幕。"""
        self.driver.search_contacts("大月")

        typed = [action for _, action in self.log if action.startswith("type_keys")]
        self.assertEqual(typed, [], f"搜索路径上不该有任何按键收尾，实际：{typed}")

    def test_it_leaves_the_search_box_empty_and_goes_back_to_the_chat_list(self) -> None:
        """收场 = 清空搜索框 + 点回「微信」（搜索面板自然收起，窗口留在原地）。"""
        self.driver.search_contacts("大月")

        self.assertIn(("search_edit", "set_text:"), self.log, "搜索框要清空，别留着上次搜的词")
        self.assertEqual(self.log[-1], ("chat_button", "click"), "最后一下是回会话列表")

    def test_a_broken_cleanup_does_not_fail_the_search(self) -> None:
        """收拾现场失败不该把这次搜索判成失败——结果已经拿到手了。

        （开头那一下点「微信」是搜索本身的一步，失败就是真失败；这里让它第二次才炸。）
        """
        clicks = {"n": 0}

        def flaky() -> None:
            clicks["n"] += 1
            if clicks["n"] > 1:
                raise RuntimeError("控件没了")

        self.chat_button.click_input = flaky  # type: ignore[method-assign]

        result = self.driver.search_contacts("大月")

        self.assertEqual(result["query"], "大月")
        self.assertEqual(clicks["n"], 2, "收场那一下确实试过了")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
