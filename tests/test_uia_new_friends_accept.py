"""自动通过好友申请在微信 4.1.13 上一个都不点的根因回归（2026-09-17 真机 4.1.13.12 dump 坐实）。

现场：候选人扫名片加 HR，「新的朋友」一直挂着，客户端日志一个字都没有。vendor 的 ``check_new_friends``
按 ``class_name == 'mmui::XTableCell'`` 挑申请条目，而 4.1.13 的条目是 ``mmui::ContactsCellNewFriendView``
——整趟拿到空列表，既不点、也不记。现在 ``uia.py`` 自己走这条路，判据全是正向证据：

★ 两种条目 class 都认（4.1.13 / 更早）；
★ 「通过」按钮按**标题**在详情面板里找，我方发出去的「等待对方验证」不能被当成通过；
★ 已添加 / 已过期的老条目不点进去；``accepted`` 只有真点了「通过」的；
★ 通过者的名字取详情面板顶上的昵称，不是列表那行「昵称+验证语+状态」；
★ 收尾点回左侧「微信」、不按 ESC；``verify=False`` 一个条目都不点。
"""

from __future__ import annotations

import itertools
import unittest
from typing import Any, ClassVar
from unittest import mock

from structlog.testing import capture_logs

from wxclient.wechat_api import uia


class _Node:
    """记录自己被怎么摆弄过的假控件（形状抄 `test_add_friend_humanized.py`）。"""

    def __init__(
        self,
        name: str,
        log: list[tuple[str, str]],
        *,
        class_name: str = "",
        text: str = "",
        exists: bool = True,
        children: list[Any] | None = None,
        lookup: dict[str, Any] | None = None,
    ) -> None:
        self._name, self._log = name, log
        self._class_name, self._text, self._exists = class_name, text, exists
        self._children = children or []
        self._lookup = lookup or {}

    # pywinauto 那一侧会用到的几个动作
    def click_input(self) -> None:
        self._log.append((self._name, "click"))

    def type_keys(self, keys: str, **_: Any) -> None:
        self._log.append((self._name, f"type_keys:{keys}"))

    def exists(self, timeout: float = 0) -> bool:
        return self._exists

    def class_name(self) -> str:
        return self._class_name

    def window_text(self) -> str:
        return self._text

    def children(self) -> list[Any]:
        return list(self._children)

    def descendants(self, **kwargs: Any) -> list[Any]:
        if kwargs.get("control_type") == "Button":
            return [v for v in self._lookup.values() if isinstance(v, _Node) and v._is_button]
        return list(self._lookup.values())

    _is_button = False

    def child_window(self, **kwargs: Any) -> Any:
        key = _key(kwargs)
        if key in self._lookup:
            return self._lookup[key]
        return _Node(f"{self._name}/{key}", self._log, exists=False)


def _key(kwargs: dict[str, Any]) -> str:
    return "|".join(f"{k}={kwargs[k]}" for k in sorted(kwargs))


class _Elements:
    class ListItems:
        NewFriendListItem: ClassVar[dict[str, str]] = {"control_type": "ListItem", "title": "新的朋友"}

    class Windows:
        VerifyFriendWindow2: ClassVar[dict[str, str]] = {"title": "通过朋友验证"}

    class Buttons:
        ConfirmButton: ClassVar[dict[str, str]] = {"control_type": "Button", "title": "确定"}

    class SideBar:
        Weixin: ClassVar[dict[str, str]] = {"title": "微信", "control_type": "Button"}


def _button(name: str, log: list, title: str) -> _Node:
    node = _Node(name, log, text=title)
    node._is_button = True
    return node


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.sleep_patch = mock.patch.object(uia.time, "sleep", lambda *_: None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        # 确认框等待用 monotonic 定 8 秒期限：假时钟每问一次走 1 秒，等不到时几轮就到期，测试不用真等。
        clock = itertools.count(0.0, 1.0)
        self.clock_patch = mock.patch.object(uia.time, "monotonic", lambda: next(clock))
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.confirm = _Node("confirm", self.log)

    def _driver(
        self,
        *,
        cells: list[_Node],
        verify_title: str | None,
        nickname: str = "小张",
        cell_class: str = "mmui::ContactsCellNewFriendView",
        windows: list[_Node] | None = None,
    ) -> uia.UiaWeChatDriver:
        log = self.log
        for cell in cells:
            cell._class_name = cell._class_name or cell_class
        contact_list = _Node("contact_list", log, children=cells)
        section = _Node("section", log)
        detail_lookup: dict[str, Any] = {
            _key({"control_type": "Text", "auto_id": "TextViewCanCopy"}): _Node("nickname", log, text=nickname),
        }
        if verify_title is not None:
            detail_lookup[_key({"title": verify_title, "control_type": "Button"})] = _button("verify", log, verify_title)
        detail = _Node("detail", log, lookup=detail_lookup)
        weixin_button = _Node("weixin_tab", log)
        main_window = _Node("main", log, lookup={
            _key(_Elements.ListItems.NewFriendListItem): section,
            _key({"control_type": "Group", "auto_id": "FMessageDetailView"}): detail,
            _key(_Elements.SideBar.Weixin): weixin_button,
        })
        self.detail, self.main_window = detail, main_window
        if windows is None:
            windows = [self.confirm_window(title="通过朋友验证", class_name="mmui::VerifyFriendWindow")]
        self.windows = windows

        navigator = mock.Mock()
        navigator.open_contacts.return_value = (contact_list, main_window)
        tools = mock.Mock()

        driver = uia.UiaWeChatDriver(dry_run=False)
        driver._ensure_ready = lambda: None  # type: ignore[method-assign]
        driver._pyweixin_class = lambda name: navigator if name == "Navigator" else tools  # type: ignore[method-assign]
        driver._pyweixin_submodule = lambda name: _Elements  # type: ignore[method-assign]
        driver._desktop_windows = lambda: list(self.windows)  # type: ignore[method-assign]
        self.tools = tools
        return driver

    def confirm_window(self, *, title: str, class_name: str) -> _Node:
        return _Node("verify_window", self.log, text=title, class_name=class_name,
                     lookup={_key(_Elements.Buttons.ConfirmButton): self.confirm})

    def _clicks(self) -> list[str]:
        return [name for name, action in self.log if action == "click"]


class NewFriendsAcceptTests(_Harness):
    def test_4113_entries_are_recognized_and_pending_one_gets_accepted(self) -> None:
        cells = [
            _Node("old1", self.log, text="奥利给我: 你好已过期"),
            _Node("old2", self.log, text="周彪我: 我是Kk已添加"),
            _Node("pending", self.log, text="小张我是小张等待验证"),   # 4.1.13 真机：待处理条目末尾是「等待验证」
        ]
        driver = self._driver(cells=cells, verify_title="前往验证")

        result = driver.check_new_friends(verify=True, limit=5)

        self.assertEqual(result["items"], ["奥利给我: 你好已过期", "周彪我: 我是Kk已添加", "小张我是小张等待验证"])
        self.assertEqual(result["accepted"], ["小张"], "名字来自详情面板的昵称，不是列表那一行")
        clicks = self._clicks()
        self.assertNotIn("old1", clicks)
        self.assertNotIn("old2", clicks)
        self.assertEqual(clicks[-4:], ["pending", "verify", "confirm", "weixin_tab"],
                         "点条目 → 点「通过」→ 确认框「确定」→ 收尾回「微信」页")
        self.assertNotIn(("contact_list", "type_keys:{ESC}"), self.log)
        self.tools.move_window_to_center.assert_not_called()

    def test_alternative_button_title_is_also_accepted(self) -> None:
        driver = self._driver(cells=[_Node("pending", self.log, text="小张想找活")], verify_title="通过验证")
        result = driver.check_new_friends(verify=True, limit=1)
        self.assertEqual(result["accepted"], ["小张"])

    def test_outbound_waiting_entry_is_not_treated_as_accept(self) -> None:
        """我方发出去、对方还没通过的条目：右侧是「等待对方验证」，不是「通过」，一个都不能点。"""
        driver = self._driver(cells=[_Node("waiting", self.log, text="老王我: 你好")], verify_title="等待对方验证")
        with capture_logs() as logs:
            result = driver.check_new_friends(verify=True, limit=5)
        self.assertEqual(result["accepted"], [])
        self.assertNotIn("verify", self._clicks())
        self.assertNotIn("confirm", self._clicks())
        missing = [entry for entry in logs if entry.get("event") == "friend.check.verify_button_missing"]
        self.assertEqual(len(missing), 1, "认不出「通过」按钮要留证：这一条右侧有哪些按钮")
        self.assertIn("等待对方验证", missing[0].get("buttons", ""))

    def test_old_xtablecell_entries_still_work(self) -> None:
        driver = self._driver(
            cells=[_Node("pending", self.log, text="小张")], verify_title="前往验证", cell_class="mmui::XTableCell",
        )
        result = driver.check_new_friends(verify=True, limit=1)
        self.assertEqual(result["accepted"], ["小张"])

    def test_unknown_cell_class_is_ignored(self) -> None:
        driver = self._driver(cells=[_Node("group", self.log, text="群聊1", class_name="mmui::ContactsCellGroupView")],
                              verify_title="前往验证")
        result = driver.check_new_friends(verify=True, limit=5)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["accepted"], [])

    def test_limit_caps_how_many_get_accepted_per_pass(self) -> None:
        cells = [_Node(f"p{i}", self.log, text=f"候选人{i}") for i in range(4)]
        driver = self._driver(cells=cells, verify_title="前往验证")
        result = driver.check_new_friends(verify=True, limit=2)
        self.assertEqual(len(result["accepted"]), 2)
        self.assertEqual(self._clicks().count("confirm"), 2)

    def test_verify_false_only_reads(self) -> None:
        driver = self._driver(cells=[_Node("pending", self.log, text="小张")], verify_title="前往验证")
        result = driver.check_new_friends(verify=False)
        self.assertEqual(result["items"], ["小张"])
        self.assertEqual(result["accepted"], [])
        self.assertNotIn("pending", self._clicks())
        self.assertNotIn("verify", self._clicks())

    def test_nickname_falls_back_to_list_text_without_status(self) -> None:
        driver = self._driver(cells=[_Node("pending", self.log, text="小张我: 我是Kk")], verify_title="前往验证",
                              nickname="")
        result = driver.check_new_friends(verify=True, limit=1)
        self.assertEqual(result["accepted"], ["小张"])

    def test_confirm_window_missing_raises_instead_of_moving_on(self) -> None:
        driver = self._driver(cells=[_Node("p1", self.log, text="甲"), _Node("p2", self.log, text="乙")],
                              verify_title="前往验证", windows=[])
        with self.assertRaises(uia.WeChatApiError):
            driver.check_new_friends(verify=True, limit=5)
        self.assertNotIn("p2", self._clicks(), "第一条的确认框没等到就停手，不再往下点别人")
        self.assertEqual(self._clicks()[-1], "weixin_tab", "抛错也要收尾回「微信」页")


class StripStatusTests(unittest.TestCase):
    def test_strip(self) -> None:
        self.assertEqual(uia._strip_new_friend_status("周彪我: 我是Kk已添加"), "周彪")
        self.assertEqual(uia._strip_new_friend_status("奥利给我: 你好已过期"), "奥利给")
        self.assertEqual(uia._strip_new_friend_status("小张"), "小张")


class VerifyConfirmFormTests(_Harness):
    """点了「通过」之后的「通过朋友验证」确认框（2026-09-17 微信 4.1.13.65 现场：vendor 判据 5 秒没等到，整趟停手）。

    vendor 要求顶层窗口的标题「通过朋友验证」**和** class `mmui::VerifyFriendWindow` 同时对上；这里任一命中都算，
    还认停靠在主窗口里的形态，以及「一点就通过、根本没有确认框」的形态。三种都没等到才停手，且必须留证。
    """

    def _pending(self) -> list[_Node]:
        return [_Node("pending", self.log, text="早芳我是早芳等待验证")]

    def test_confirm_window_found_by_class_when_title_changed(self) -> None:
        driver = self._driver(cells=self._pending(), verify_title="前往验证",
                              windows=[self.confirm_window(title="添加朋友", class_name="mmui::VerifyFriendWindow")])
        result = driver.check_new_friends(verify=True, limit=1)
        self.assertEqual(result["accepted"], ["小张"])
        self.assertIn("confirm", self._clicks())

    def test_confirm_window_found_by_title_when_class_changed(self) -> None:
        driver = self._driver(cells=self._pending(), verify_title="前往验证",
                              windows=[_Node("other", self.log, text="微信", class_name="mmui::MainWindow"),
                                       self.confirm_window(title="通过朋友验证", class_name="mmui::XDialog")])
        result = driver.check_new_friends(verify=True, limit=1)
        self.assertEqual(result["accepted"], ["小张"])
        self.assertEqual(self._clicks().count("confirm"), 1)

    def test_confirm_form_docked_inside_main_window(self) -> None:
        driver = self._driver(cells=self._pending(), verify_title="前往验证", windows=[])
        self.main_window._lookup[_key({"title": "通过朋友验证", "control_type": "Text"})] = _Node("docked_title", self.log)
        self.main_window._lookup[_key(_Elements.Buttons.ConfirmButton)] = self.confirm
        with capture_logs() as logs:
            result = driver.check_new_friends(verify=True, limit=1)
        self.assertEqual(result["accepted"], ["小张"])
        self.assertIn("confirm", self._clicks())
        accepted = [entry for entry in logs if entry.get("event") == "friend.check.accepted"]
        self.assertEqual(accepted[0].get("via"), "主窗口内确认框")

    def test_accept_without_any_confirm_form_counts_when_panel_shows_send_message(self) -> None:
        driver = self._driver(cells=self._pending(), verify_title="前往验证", windows=[])
        detail = self.detail
        verify = detail._lookup[_key({"title": "前往验证", "control_type": "Button"})]
        send_message = _button("send_message", self.log, "发消息")

        def click_verify() -> None:
            self.log.append(("verify", "click"))
            verify._exists = False
            detail._lookup[_key({"title": "发消息", "control_type": "Button"})] = send_message

        verify.click_input = click_verify  # type: ignore[method-assign]
        result = driver.check_new_friends(verify=True, limit=1)
        self.assertEqual(result["accepted"], ["小张"])
        self.assertNotIn("confirm", self._clicks())

    def test_no_confirm_anywhere_stops_and_leaves_evidence(self) -> None:
        driver = self._driver(cells=self._pending(), verify_title="前往验证",
                              windows=[_Node("main", self.log, text="微信", class_name="mmui::MainWindow")])
        with capture_logs() as logs, self.assertRaises(uia.WeChatApiError) as ctx:
            driver.check_new_friends(verify=True, limit=1)
        self.assertIn("前往验证", str(ctx.exception))
        self.assertNotIn("confirm", self._clicks())
        missing = [entry for entry in logs if entry.get("event") == "friend.check.confirm_missing"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].get("button"), "前往验证")
        self.assertIn("mmui::MainWindow|微信", missing[0].get("windows", ""))
        self.assertIn("前往验证", missing[0].get("buttons", ""))


if __name__ == "__main__":
    unittest.main()
