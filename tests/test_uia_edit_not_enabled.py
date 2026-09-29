"""输入框「还没 enabled」时的重来一次（2026-08-30 用户实报：发消息报 ElementNotEnabled，第二次才成）。

钉的是**该不该重来**这件事，不是微信真机行为——所以这里不碰 pywinauto，也不需要图形环境。
"""

from __future__ import annotations

import unittest
from unittest import mock

from wxclient.wechat_api.uia import (
    UiaWeChatDriver,
    _is_element_not_enabled,
    _is_not_friend_error,
)


class ElementNotEnabled(Exception):
    """冒充 pywinauto 的那个异常。判据是**类名**，所以这里同名即可（见 `_is_element_not_enabled`）。"""


class 判据(unittest.TestCase):
    def test_只认这一个类名(self) -> None:
        self.assertTrue(_is_element_not_enabled(ElementNotEnabled("输入框还不能用")))
        self.assertFalse(_is_element_not_enabled(RuntimeError("别的错")))
        self.assertFalse(_is_element_not_enabled(Exception("ElementNotEnabled")))  # 消息里带也不算


class 重来一次(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = UiaWeChatDriver.__new__(UiaWeChatDriver)   # 不跑 __init__，这里只测这一段
        # 等待那步整个换掉：真机上它要轮询几秒，单测不该为此空等
        self.waited: list[str] = []
        self.driver._wait_chat_input_enabled = lambda friend: (self.waited.append(friend), True)[1]

    def test_撞上就重来一次并成功(self) -> None:
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise ElementNotEnabled("输入框还不能用")
            return "发出去了"

        with mock.patch("wxclient.wechat_api.uia.logger"):
            result = self.driver._with_edit_retry("张三", "send_message", flaky)

        self.assertEqual(result, "发出去了")
        self.assertEqual(len(calls), 2, "★ 只重来一次")
        self.assertEqual(self.waited, ["张三"], "重来之前要先等输入框可用")

    def test_一次就成的不重来(self) -> None:
        calls = []
        with mock.patch("wxclient.wechat_api.uia.logger"):
            self.driver._with_edit_retry("张三", "send_message", lambda: calls.append(1))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.waited, [], "没出错就别去等")

    def test_别的异常原样上抛绝不重来(self) -> None:
        """★ 这条是防线：只有「还没 enabled」能证明一个字都没发出去，别的错重来就可能重复骚扰对方。"""
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("别的错")

        with mock.patch("wxclient.wechat_api.uia.logger"), self.assertRaises(RuntimeError):
            self.driver._with_edit_retry("张三", "send_message", boom)
        self.assertEqual(len(calls), 1, "★ 一次都不许重来")

    def test_重来那次再炸就照实上抛(self) -> None:
        """重试不是无限的：第二次还不行就认了，让上游记失败、下一轮重投。"""
        def always():
            raise ElementNotEnabled("一直不能用")

        with mock.patch("wxclient.wechat_api.uia.logger"), self.assertRaises(ElementNotEnabled):
            self.driver._with_edit_retry("张三", "send_message", always)


class NotFriendError(Exception):
    """冒充 pyweixin 的那个异常（「非正常好友,无法发送消息」）。同样按类名认。"""


class 非正常好友也重来一次(unittest.TestCase):
    """2026-09-17 用户实报：好友是真的，前后两条都发成了，刚读完他的回复立刻回的那条报
    「非正常好友,无法发送消息」。它和「还没 enabled」是同一道闸、同样在一个字都没粘进去前抛。"""

    def setUp(self) -> None:
        self.driver = UiaWeChatDriver.__new__(UiaWeChatDriver)
        self.waited: list[str] = []
        self.driver._wait_chat_input_enabled = lambda friend: (self.waited.append(friend), True)[1]
        self.closed: list[object] = []
        self.separate_window: object | None = None
        self.driver._find_separate_dialog_window = lambda friend: self.separate_window
        self.driver._try_close_window = lambda window: self.closed.append(window)

    def test_判据只认类名(self) -> None:
        self.assertTrue(_is_not_friend_error(NotFriendError("非正常好友,无法发送消息")))
        self.assertFalse(_is_not_friend_error(RuntimeError("非正常好友,无法发送消息")))

    def test_撞上就等输入框回来再试一次(self) -> None:
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise NotFriendError("非正常好友,无法发送消息")
            return "发出去了"

        with mock.patch("wxclient.wechat_api.uia.logger"), mock.patch("wxclient.wechat_api.uia.time"):
            result = self.driver._with_edit_retry("Hope Of The Whole Village", "send_message", flaky)
        self.assertEqual(result, "发出去了")
        self.assertEqual(len(calls), 2, "★ 只重来一次")
        self.assertEqual(self.waited, ["Hope Of The Whole Village"])
        self.assertEqual(self.closed, [], "没有独立窗口就什么都别关")

    def test_会话被弹成独立窗口时先关掉再试(self) -> None:
        self.separate_window = object()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise NotFriendError("非正常好友,无法发送消息")
            return "发出去了"

        with mock.patch("wxclient.wechat_api.uia.logger"), mock.patch("wxclient.wechat_api.uia.time"):
            self.driver._with_edit_retry("Hope Of The Whole Village", "send_message", flaky)
        self.assertEqual(self.closed, [self.separate_window], "★ 重来之前要把独立窗口关掉")
        self.assertEqual(len(calls), 2)

    def test_真不是好友时重来那次照样抛(self) -> None:
        def always():
            raise NotFriendError("非正常好友,无法发送消息")

        with mock.patch("wxclient.wechat_api.uia.logger"), mock.patch("wxclient.wechat_api.uia.time"), \
                self.assertRaises(NotFriendError):
            self.driver._with_edit_retry("张三", "send_message", always)


class 两处发送都接上了(unittest.TestCase):
    """★ 改成 lambda 之后最容易犯的错是「接错一处」——两条路都得走外壳。"""

    def test_发消息和发文件都过重试外壳(self) -> None:
        import inspect

        # 发消息的底座调用收在 `_send_text_once`（09-29：输入框内容不对时要清空重发一次，两处共用）
        for name in ("_send_text_once", "send_files"):
            src = inspect.getsource(getattr(UiaWeChatDriver, name))
            self.assertIn("_with_edit_retry", src, f"{name} 没接上重试外壳")


if __name__ == "__main__":
    unittest.main()
