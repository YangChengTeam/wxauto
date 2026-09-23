"""加好友那一趟交互的**形状**：像人在操作，不像脚本在填表。

2026-08-27 真机弹了安全验证。当天日志里加好友只有两次，所以问题不在量，在两处：
① 两次只隔 81 秒，而且都是「没搜到这个号」——节拍那层已经补了
   （`guard.WRITE_PACING_BUCKETS`，用例在 `test_gateway_commands.py::AddFriendPacingTests`）；
② **单趟交互 6~7 秒、方差为零**——号码整串糊进搜索框、名片一出来立刻点添加、
   验证语和备注也是整串粘贴、点击之间零停顿。这个文件盯的是第二条。

判据只看「有没有停、停得随不随机」，不锁具体秒数：把区间写死进断言，
以后调参数就得连测试一起改，而这些数本来就该能调。
"""

from __future__ import annotations

import unittest
from typing import Any, ClassVar
from unittest import mock

from wxclient.wechat_api import uia


class HumanPauseTests(unittest.TestCase):
    def test_every_step_actually_waits(self) -> None:
        for step in uia._PAUSES:
            with mock.patch.object(uia.time, "sleep") as slept:
                uia._human_pause(step)
            self.assertEqual(slept.call_count, 1, f"{step} 没停")
            self.assertGreater(slept.call_args[0][0], 0, f"{step} 停了 0 秒")

    def test_an_unknown_step_does_not_invent_a_delay(self) -> None:
        """表里没有的名字不停——比静默猜一个值好：写错名字要看得出来。"""
        with mock.patch.object(uia.time, "sleep") as slept:
            uia._human_pause("这一步不存在")
        slept.assert_not_called()

    def test_each_pause_lands_inside_its_declared_range(self) -> None:
        for step, (lo, hi) in uia._PAUSES.items():
            self.assertLess(lo, hi, f"{step} 的区间是空的")
            for _ in range(20):
                with mock.patch.object(uia.time, "sleep") as slept:
                    uia._human_pause(step)
                self.assertTrue(lo <= slept.call_args[0][0] <= hi, step)

    def test_pauses_are_random_not_a_constant(self) -> None:
        """★ 固定 2.0 秒和固定 0.0 秒一样是机器特征，只是慢一点。"""
        seen = set()
        for _ in range(30):
            with mock.patch.object(uia.time, "sleep") as slept:
                uia._human_pause("read_profile")
            seen.add(slept.call_args[0][0])
        self.assertGreater(len(seen), 5, "每次都停一样久 = 换了个节奏的机器")


class _Edit:
    def __init__(self, *, accepts_pause: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._accepts_pause = accepts_pause

    def type_keys(self, text: str, **kwargs: Any) -> None:
        if "pause" in kwargs and not self._accepts_pause:
            raise TypeError("type_keys() got an unexpected keyword argument 'pause'")
        self.calls.append((text, kwargs))


class TypeLikeHumanTests(unittest.TestCase):
    def test_it_types_with_a_gap_between_keys(self) -> None:
        edit = _Edit()
        uia._type_like_human(edit, "13800000001")
        (text, kwargs), = edit.calls
        self.assertEqual(text, "13800000001")
        lo, hi = uia._KEYSTROKE_PAUSE
        self.assertTrue(lo <= kwargs["pause"] <= hi)

    def test_old_pywinauto_without_pause_still_types(self) -> None:
        """★ 认不出 `pause=` 就退回整串输入。

        输不进去比慢一点严重得多——这一层不该新添一个能让加好友失败的失败点。
        """
        edit = _Edit(accepts_pause=False)
        uia._type_like_human(edit, "13800000001")
        (text, kwargs), = edit.calls
        self.assertEqual(text, "13800000001")
        self.assertNotIn("pause", kwargs)

    def test_the_gap_varies_between_calls(self) -> None:
        edit = _Edit()
        for _ in range(20):
            uia._type_like_human(edit, "13800000001")
        self.assertGreater(len({kw["pause"] for _, kw in edit.calls}), 5)


class _Node:
    """记录自己被怎么摆弄过的假控件（形状抄 `test_uia_search_cleanup.py`）。"""

    def __init__(self, name: str, log: list[tuple[str, str]], children: dict[str, Any] | None = None) -> None:
        self._name, self._log, self._children = name, log, children or {}

    def click_input(self) -> None:
        self._log.append((self._name, "click"))

    def set_text(self, text: str) -> None:
        self._log.append((self._name, f"set_text:{text}"))

    def type_keys(self, keys: str, **kwargs: Any) -> None:
        self._log.append((self._name, f"type_keys:{keys}"))

    def close(self) -> None:
        self._log.append((self._name, "close"))

    def descendants(self, **_: Any) -> list[Any]:
        return []

    def child_window(self, **kwargs: Any) -> Any:
        key = str(kwargs.get("role") or kwargs.get("found_index"))
        return self._children.get(key, _Node(f"{self._name}/{key}", self._log))

    def window_text(self) -> str:
        return ""

    def exists(self, **_: Any) -> bool:
        return True


class _Elements:
    class Windows:
        VerifyFriendWindow: ClassVar[dict] = {"role": "verify"}
        # 面板窗口的 title 同时也是快捷操作菜单里那一项的文案——`_open_add_friend_panel`
        # 靠这一句**按名字**点，不数第几项（2026-09-22 客户机事故）。
        AddFriendWindow: ClassVar[dict] = {"title": "添加朋友", "class_name": "mmui::AddFriendWindow",
                           "control_type": "Window"}

    class Edits:
        ChangeRemarkEdit: ClassVar[dict] = {"role": "remark_edit"}

    class Buttons:
        ConfirmButton: ClassVar[dict] = {"role": "confirm"}
        QuickActionsButton: ClassVar[dict] = {"role": "quick_actions"}

    class Lists:
        QuickActionsList: ClassVar[dict] = {"role": "quick_actions_list"}

    class SideBar:
        # 「快捷操作」只挂在会话页上，所以开面板前要先点亮它。
        Weixin: ClassVar[dict] = {"role": "chat_tab"}


class AddFriendFlowTests(unittest.TestCase):
    """停顿真的接在那趟流程里了——不是定义了一组常量却没人调。

    这里跑的是真的 `_add_friend_reading_profile`，只把 pywinauto 那一侧换成假控件，
    并把 `time.sleep` 换成记账用的假货：断言的是**这趟流程停了几次、在哪几步停**。
    """

    def setUp(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.slept: list[float] = []

        self.search_edit = _Node("search_edit", self.log)
        self.add_button = _Node("add_button", self.log)
        pane = _Node("pane", self.log)
        pane.child_window = lambda **_: self.search_edit  # type: ignore[method-assign]
        self.pane = pane

        # 主窗 → 「快捷操作」按钮 → 菜单列表 → 「添加朋友」那一项，每一层都记账，
        # 这样「是按名字点的还是敲键盘蒙的」在断言里看得见。
        self.entry = _Node("add_friend_entry", self.log)
        menu = _Node("quick_actions_list", self.log)
        menu.child_window = lambda **kw: (  # type: ignore[method-assign]
            self.entry if kw.get("title") == "添加朋友" else _Node("其他项", self.log)
        )
        self.quick_actions_button = _Node("quick_actions_button", self.log)
        main = _Node("main", self.log, {
            "quick_actions": self.quick_actions_button,
            "quick_actions_list": menu,
        })

        navigator = mock.Mock()
        navigator.open_weixin.return_value = main
        tools = mock.Mock()
        # 面板和验证窗都走 move_window_to_center，按 criteria 分派，别让两者串味。
        tools.move_window_to_center.side_effect = lambda Window: (
            pane if Window.get("class_name") == "mmui::AddFriendWindow"
            else _Node("verify", self.log)
        )

        self.driver = uia.UiaWeChatDriver(dry_run=False)
        self.driver._ensure_ready = lambda: None  # type: ignore[method-assign]
        self.driver._pyweixin_class = lambda name: (  # type: ignore[method-assign]
            navigator if name == "Navigator" else tools
        )
        self.driver._pyweixin_submodule = lambda name: _Elements  # type: ignore[method-assign]
        self.driver._await_contact_profile = (  # type: ignore[method-assign]
            lambda *a, **k: (_Node("profile", self.log), self.add_button)
        )

    def _run(self) -> None:
        with mock.patch.object(uia.time, "sleep", self.slept.append), \
             mock.patch.object(uia, "_profile_text_lines", lambda _p: ["龚从林"]):
            self.driver._add_friend_reading_profile("13800000001", "你好", "张三")

    def test_the_whole_trip_is_no_longer_instant(self) -> None:
        """★ 改造前整趟 6~7 秒且方差为零。现在每一步之间都有随机停顿。"""
        self._run()
        self.assertEqual(len(self.slept), len(uia._PAUSES),
                         f"该停 {len(uia._PAUSES)} 次，实际停了 {len(self.slept)} 次")
        self.assertGreater(sum(self.slept), 5, "加起来还不到 5 秒，等于没停")

    def test_the_panel_is_opened_by_clicking_the_entry_by_name(self) -> None:
        """★ 2026-09-22 客户机事故：面板是**按名字点**开的，不是敲 {UP} 数第几项。

        vendor 的 `open_add_friend_panel` 赌菜单项顺序（`{UP}`×2 + `{ENTER}`），
        微信 4.1.12.26 上那个菜单是「发起群聊 / 添加朋友 / 写笔记」三项，两下 UP 落在别处，
        面板压根没开 —— 号码于是**永远没机会输进去**，用户看到的就是「添加框出来了但没查询」。
        """
        self._run()
        self.assertIn(("quick_actions_button", "click"), self.log)
        self.assertIn(("add_friend_entry", "click"), self.log)
        self.assertNotIn("add_friend_entry", [n for n, _ in self.log if _.startswith("type_keys")])
        self.assertFalse([a for _, a in self.log if "{UP}" in a],
                         "又在靠敲方向键数第几项了——项序跟微信版本走，名字才跟语言走")

    def test_the_number_is_typed_key_by_key_not_pasted(self) -> None:
        self._run()
        self.assertIn(("search_edit", "type_keys:13800000001"), self.log)

    def test_it_still_does_the_same_things_in_the_same_order(self) -> None:
        """★ 只加停顿，不动任何判据：该点的还是照点，顺序也没变。"""
        self._run()
        actions = [a for n, a in self.log if n in {"search_edit", "add_button"}]
        self.assertEqual(actions, [
            "set_text:", "type_keys:13800000001", "type_keys:{ENTER}", "click",
        ])

    def test_two_trips_do_not_take_the_same_time(self) -> None:
        self._run()
        first = list(self.slept)
        self.slept.clear()
        self._run()
        self.assertNotEqual(first, self.slept, "两趟一模一样 = 只是换了个固定节奏")


if __name__ == "__main__":
    unittest.main()
