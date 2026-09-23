from __future__ import annotations

import unittest

from wxclient.wechat_api import (
    CompositeWeChatDriver,
    UiaWeChatDriver,
    WeChatApi,
    WeChatApiError,
)
from wxclient.wechat_api.api import CompositeWeChatDriver as SplitCompositeDriver
from wxclient.wechat_api.api import WeChatApi as SplitWeChatApi
from wxclient.wechat_api.errors import WeChatApiError as SplitWeChatApiError
from wxclient.wechat_api.uia import UiaWeChatDriver as SplitUiaWeChatDriver


class WeChatApiStructureTests(unittest.TestCase):
    def test_public_exports_are_backed_by_responsibility_modules(self) -> None:
        self.assertIs(WeChatApi, SplitWeChatApi)
        self.assertIs(CompositeWeChatDriver, SplitCompositeDriver)
        self.assertIs(UiaWeChatDriver, SplitUiaWeChatDriver)
        self.assertIs(WeChatApiError, SplitWeChatApiError)

    def test_facade_composes_uia_driver_without_inheriting_it(self) -> None:
        api = WeChatApi(dry_run=True)

        self.assertNotIsInstance(api, UiaWeChatDriver)
        self.assertIsInstance(api._uia_driver, UiaWeChatDriver)
        self.assertIs(api._composite_driver.drivers[0], api._uia_driver)
        self.assertTrue(api._is_dry_run(None))


class RestoreMinimizedWindowTests(unittest.TestCase):
    """微信最小化时 connect 直接找不到窗口，动作前必须按 hwnd 恢复（2026-08-12 真机实测）。"""

    def test_minimized_window_yields_its_handle(self) -> None:
        self.assertEqual(
            UiaWeChatDriver._minimized_handle({"handle": 199668, "is_iconic": True}),
            199668,
        )

    def test_normal_window_is_left_alone(self) -> None:
        self.assertIsNone(
            UiaWeChatDriver._minimized_handle({"handle": 199668, "is_iconic": False})
        )

    def test_missing_or_malformed_probe_result_is_tolerated(self) -> None:
        for window_info in (None, {}, {"is_iconic": True}, {"is_iconic": True, "handle": "x"}):
            with self.subTest(window_info=window_info):
                self.assertIsNone(UiaWeChatDriver._minimized_handle(window_info))

    def test_forced_restore_ignores_iconic_flag(self) -> None:
        # UIA 子树塌掉时探测未必还报 is_iconic，但只要有句柄就该恢复一次再复探
        self.assertEqual(
            UiaWeChatDriver._minimized_handle({"handle": 199668, "is_iconic": False}, force=True),
            199668,
        )
        self.assertIsNone(UiaWeChatDriver._minimized_handle({}, force=True))

    def test_restore_without_handle_is_a_noop(self) -> None:
        # 探测结果里没有可用句柄 → 不动窗口、不抛、如实返回 False（不能骗调用方去复探）
        # 故意不拿真句柄做用例：Windows 上跑测试会真的把那个窗口弹出来。
        for window_info in (None, {}, {"is_iconic": True}, {"is_iconic": True, "handle": "x"}):
            with self.subTest(window_info=window_info):
                self.assertFalse(UiaWeChatDriver._restore_if_minimized(window_info))
                self.assertFalse(UiaWeChatDriver._restore_if_minimized(window_info, force=True))


class SendMessageVerificationTests(unittest.TestCase):
    """★ 075 发后核对（需求文档 §12.4）。

    底座 `send_messages_to_friend` 打完字按回车就返回成功，**没发出去也返回成功**——
    文字会留在输入框里。067 在文件那条路径上真机踩过一模一样的坑（文件躺在输入框里、
    回执却报 done）。宁可报失败也不谎报「已发送」：AI 会照着回执告诉用户「已经发过去了」。
    """

    def _driver(self, leftovers: list[int]) -> UiaWeChatDriver:
        driver = UiaWeChatDriver(dry_run=False)
        self.nudged = 0

        def fake_len(_friend: str) -> int:
            return leftovers.pop(0) if leftovers else 0

        def fake_nudge(_friend: str) -> None:
            self.nudged += 1

        driver._ensure_ready = lambda: None                     # type: ignore[method-assign]
        driver._pyweixin_class = lambda _name: _FakeMessages    # type: ignore[method-assign]
        driver._chat_input_text_len = fake_len                  # type: ignore[method-assign]
        driver._nudge_send = fake_nudge                         # type: ignore[method-assign]
        return driver

    def test_empty_input_box_means_it_really_went_out(self) -> None:
        result = self._driver([0]).send_message("王建国", "您好")
        self.assertTrue(result["verified"])
        self.assertEqual(self.nudged, 0)

    def test_stuck_text_gets_one_nudge_then_succeeds(self) -> None:
        """补按一次回车把停在框里的那条发走。这**不是**重发——不会产生第二条消息。"""
        result = self._driver([5, 0]).send_message("王建国", "您好")
        self.assertTrue(result["verified"])
        self.assertEqual(self.nudged, 1)

    def test_still_stuck_raises_instead_of_reporting_success(self) -> None:
        """★ 核心：发不出去就要报失败，不能让 AI 去跟用户说「已经发过去了」。"""
        driver = self._driver([5, 5])
        with self.assertRaises(WeChatApiError) as caught:
            driver.send_message("王建国", "您好")
        self.assertIn("没能发出去", str(caught.exception))

    def test_unprobeable_input_box_is_reported_as_unverified_not_failed(self) -> None:
        """探测不到输入框 = 「不知道」，据实说没核对过，别把不知道讲成已确认。"""
        result = self._driver([-1]).send_message("王建国", "您好")
        self.assertFalse(result["verified"])
        self.assertEqual(self.nudged, 0)


class _FakeMessages:
    @staticmethod
    def send_messages_to_friend(**_kwargs) -> None:
        return None


class _FakeControl:
    """够用的假控件：认得出「你要的是哪一个子控件」，并记下被点过没有。"""

    def __init__(self, name: str, text: str = "", children: dict | None = None,
                 exists_: bool = True, log: list | None = None) -> None:
        self.name = name
        self._text = text
        self._children = children or {}
        self._exists = exists_
        self.log = log if log is not None else []
        self.set_texts: list[str] = []

    def exists(self, timeout: float = 0) -> bool:
        return self._exists

    def window_text(self) -> str:
        return self._text

    def descendants(self, **_: object) -> list:
        return []

    def child_window(self, **criteria):
        key = criteria.get("_key") or criteria.get("control_type")
        if key == "Edit":
            key = f"Edit{criteria.get('found_index', '')}"
        child = self._children.get(key)
        if child is None:
            return _FakeControl(f"{self.name}/缺席", exists_=False, log=self.log)
        return child

    def set_text(self, value: str) -> None:
        self.set_texts.append(value)

    def type_keys(self, value: str, **_: object) -> None:
        self.set_texts.append(value)

    def click_input(self) -> None:
        self.log.append(f"click:{self.name}")

    def close(self) -> None:
        self.log.append("close:面板")


class _FakeUielements:
    Groups = type("G", (), {"ContactProfileViewGroup": {"_key": "profile"}})()
    Buttons = type("B", (), {"AddToContactsButton": {"_key": "add"},
                             "SendMessageButton": {"_key": "send"},
                             "ConfirmButton": {"_key": "confirm"},
                             "QuickActionsButton": {"_key": "quick_actions"}})()
    # AddFriendWindow 的 title 同时也是快捷操作菜单里那一项的文案——`_open_add_friend_panel`
    # 按这个名字点菜单项，不数第几项（2026-09-22 客户机事故）。
    Edits = type("E", (), {"ChangeRemarkEdit": {"_key": "remark_edit"}})()
    Windows = type("W", (), {"VerifyFriendWindow": {"_key": "verify"},
                             "AddFriendWindow": {"_key": "add_friend_window",
                                                 "title": "添加朋友",
                                                 "class_name": "mmui::AddFriendWindow"}})()
    Lists = type("L", (), {"QuickActionsList": {"_key": "quick_actions_list"}})()
    SideBar = type("S", (), {"Weixin": {"_key": "chat_tab"}})()


class AddFriendReturnsTheNameTests(unittest.TestCase):
    """★ 加好友要把「后续拿什么名字回消息」带回来（用户 2026-08-15）。

    三个名字别混：`name` 是对方昵称（给人看），`remark` 是我们设的备注，
    `session_name` = 备注真填上了就用备注、没有才退回昵称 —— 那才是 send_message(to=...) 该用的。
    """

    def setUp(self) -> None:
        # 等名片默认 12 秒（真机上搜陌生号是一次网络查找）。用例里把它压到近乎 0：
        # 这里测的是**判据**，不是耐心。
        from unittest import mock

        patcher = mock.patch("wxclient.wechat_api.uia._PROFILE_WAIT_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _driver(self, profile_exists: bool = True, add_button_exists: bool = True,
                send_button_exists: bool = False, profile_text: str | None = None,
                group_matches: bool = True, remark_edit_exists: bool = True):
        from unittest import mock

        from wxclient.wechat_api.uia import UiaWeChatDriver

        log: list[str] = []
        verify = _FakeControl("申请窗", children={
            "Edit0": _FakeControl("招呼语", log=log),
            # 序号 1 那格不是备注栏（2026-09-23 用户实报：候选人名字被填进了昵称那格）。
            "Edit1": _FakeControl("昵称", log=log),
            "remark_edit": _FakeControl("修改备注", exists_=remark_edit_exists, log=log),
            "confirm": _FakeControl("确定", log=log),
        }, log=log)
        profile = _FakeControl(
            "名片",
            text="张三\n微信号：wxid_abc\n地区：深圳" if profile_text is None else profile_text,
            children={
                "add": _FakeControl("添加到通讯录", exists_=add_button_exists, log=log),
                "send": _FakeControl("发消息", exists_=send_button_exists, log=log),
            },
            exists_=profile_exists, log=log,
        )
        pane_children = {"Edit": _FakeControl("搜索框", log=log)}
        if group_matches:
            pane_children["profile"] = profile
        else:
            # 「那层 Group 的 class_name 换了」：名片组认不出来，但名片本身就在面板里。
            pane_children.update(profile._children)
        pane = _FakeControl(
            "添加朋友面板",
            text="" if group_matches else (profile.window_text() or ""),
            children=pane_children, log=log,
        )
        vendor_calls: list[dict] = []

        # 主窗 → 会话页 → 「快捷操作」→ 菜单里按名字点「添加朋友」，
        # 就是 `_open_add_friend_panel` 真实走的那几步（不再有 open_add_friend_panel 这个桩）。
        main_window = _FakeControl("主窗口", children={
            "chat_tab": _FakeControl("会话页", log=log),
            "quick_actions": _FakeControl("快捷操作", log=log),
            "quick_actions_list": _FakeControl("快捷操作菜单", children={
                "ListItem": _FakeControl("添加朋友", log=log),
            }, log=log),
        }, log=log)

        class _Navigator:
            @staticmethod
            def open_weixin(**_: object):
                return main_window

        class _Tools:
            @staticmethod
            def move_window_to_center(**kwargs: object):
                # 面板和验证窗共用这一个入口，按 criteria 分派，别让两者串味。
                window = kwargs.get("Window") or {}
                key = window.get("_key") if isinstance(window, dict) else None
                return pane if key == "add_friend_window" else verify

        class _FriendSettings:
            @staticmethod
            def add_new_friend(**kwargs: object) -> None:
                vendor_calls.append(dict(kwargs))

        classes = {"Navigator": _Navigator, "Tools": _Tools, "FriendSettings": _FriendSettings}
        driver = UiaWeChatDriver(dry_run=False)
        stack = mock.patch.multiple(
            driver,
            _ensure_ready=mock.DEFAULT,
            _pyweixin_class=mock.DEFAULT,
            _pyweixin_submodule=mock.DEFAULT,
        )
        patched = stack.start()
        self.addCleanup(stack.stop)
        patched["_pyweixin_class"].side_effect = lambda name: classes[name]
        patched["_pyweixin_submodule"].side_effect = lambda name: _FakeUielements
        return driver, log, vendor_calls, verify

    def test_the_remark_goes_into_the_remark_box_not_the_nickname_box(self) -> None:
        """★ 用户 2026-09-23：「备注要填，只是不要填到昵称上去」——按名字找「修改备注」栏。"""
        driver, log, vendor_calls, verify = self._driver()

        result = driver.add_friend("13800138000", "你好", remark="张三-仓库工")

        self.assertEqual(result["name"], "张三", "对方昵称要读回来")
        self.assertEqual(result["remark"], "张三-仓库工")
        self.assertEqual(result["session_name"], "张三-仓库工", "★ 备注填上了就用备注")
        self.assertIn("click:添加到通讯录", log)
        self.assertIn("click:确定", log)
        self.assertEqual(verify.child_window(_key="remark_edit").set_texts,
                         ["张三-仓库工"], "★ 备注要真的填进备注栏")
        self.assertEqual(verify.child_window(control_type="Edit", found_index=1).set_texts,
                         [], "★ 序号 1 那格（昵称）一个字都不许填")
        self.assertEqual(verify.child_window(control_type="Edit", found_index=0).set_texts,
                         ["你好"], "验证语照填")
        self.assertEqual(vendor_calls, [], "自己走通了就不该再调 vendor")

    def test_no_remark_box_means_no_remark_and_the_nickname_is_the_session_name(self) -> None:
        """找不到备注栏就不填，绝不退回按序号猜；会话名如实退回昵称。"""
        driver, log, _vendor, verify = self._driver(remark_edit_exists=False)

        result = driver.add_friend("13800138000", "你好", remark="张三-仓库工")

        self.assertIn("click:确定", log, "申请照发")
        self.assertEqual(verify.child_window(control_type="Edit", found_index=1).set_texts, [])
        self.assertEqual(result["session_name"], "张三", "★ 备注没填上，会话名不能是 remark")

    def test_the_vendor_fallback_does_not_set_a_remark_either(self) -> None:
        from wxclient.wechat_api.uia import _AddFriendNotStarted

        driver, _log, vendor_calls, _verify = self._driver()

        def not_started(*_a: object, **_k: object):
            raise _AddFriendNotStarted("面板没开")

        driver._add_friend_reading_profile = not_started  # type: ignore[assignment]
        driver.add_friend("13800138000", "你好", remark="张三-仓库工")

        self.assertEqual(len(vendor_calls), 1)
        self.assertIsNone(vendor_calls[0]["remark"], "★ vendor 按序号填，会填进昵称那格——退化路不设备注")

    def test_without_a_remark_the_session_name_falls_back_to_the_nickname(self) -> None:
        driver, _log, _vendor, _verify = self._driver()

        result = driver.add_friend("13800138000", "你好")

        self.assertEqual(result["session_name"], "张三")

    def test_a_number_that_cannot_be_found_fails_loudly(self) -> None:
        """★ 原来查无此号是**静默返回成功**的，AI 端只会以为对方没通过而一直等。"""
        driver, log, vendor_calls, _verify = self._driver(profile_exists=False)

        with self.assertRaises(WeChatApiError) as caught:
            driver.add_friend("13800138000", "你好")

        self.assertIn("没搜到这个号", str(caught.exception))
        self.assertNotIn("click:添加到通讯录", log, "没搜到就绝不该点添加")
        self.assertEqual(vendor_calls, [], "这是明确失败，不是退化——别再让 vendor 加一遍")

    def test_an_existing_friend_comes_back_as_a_fact_not_an_error(self) -> None:
        """★ 已经是好友：名片这一趟已经搜完了，名字也读到了——别把它连同异常一起丢掉。

        （2026-08-26 用户场景：一个号早就加过了，还是要能把昵称同步回业务端。）
        """
        driver, log, vendor_calls, _verify = self._driver(
            add_button_exists=False, send_button_exists=True,
            profile_text="张三-仓库工\n昵称：阿三\n微信号：wxid_abc",
        )

        result = driver.add_friend("13800138000", "你好", remark="张三-叉车工")

        self.assertTrue(result["already_friend"])
        self.assertEqual(result["profile_name"], "阿三", "明写的「昵称：X」才是昵称")
        self.assertEqual(result["session_name"], "张三-仓库工",
                         "★ 这一轮没设过备注：会话名必须是微信里现在显示的那个，不是我们提议的")
        self.assertNotIn("click:添加到通讯录", log, "按钮都不在，更不该点")
        self.assertEqual(vendor_calls, [], "这是明确结论，不是退化——别再让 vendor 加一遍")

    def test_a_renamed_profile_group_no_longer_reads_as_not_found(self) -> None:
        """★ 2026-08-27 陈卓那次：弹框出来了、人也找到了，一下又关了，日志报「没搜到这个号」。

        真因是名片那层 Group 的 ``class_name`` **跟微信版本走**（vendor 表里只覆盖两个版本），
        表里没有的版本 → 名片「查无此控件」→ 报没搜到 → ``finally`` 把面板关掉。
        现在判据换成**按钮在不在**，那层 Group 叫什么已经不重要。
        """
        driver, log, vendor_calls, _verify = self._driver(group_matches=False)

        result = driver.add_friend("13800138000", "你好", remark="张三-仓库工")

        self.assertIn("click:添加到通讯录", log, "★ 名片就在面板里，必须加得成")
        self.assertEqual(result["session_name"], "张三-仓库工")
        self.assertEqual(result["name"], "张三", "退化到整个面板当名片根，昵称照样挑得出来")
        self.assertEqual(vendor_calls, [])

    def test_a_slow_profile_is_waited_for_instead_of_called_missing(self) -> None:
        """★ 慢 ≠ 没有。搜陌生号是一次网络查找，名片晚几秒才渲染出来是常事。"""
        from unittest import mock

        driver, log, _vendor, _verify = self._driver(add_button_exists=False)
        add_button = _FakeControl("添加到通讯录", exists_=False, log=log)

        polls = {"n": 0}

        def renders_on_the_third_poll(*_a: object, **_k: object) -> bool:
            polls["n"] += 1
            return polls["n"] >= 3            # 前两轮还没渲染好，第三轮才出来

        add_button.exists = renders_on_the_third_poll   # type: ignore[method-assign]
        profile = _FakeControl("名片", text="张三", children={"add": add_button}, log=log)

        with mock.patch("wxclient.wechat_api.uia._PROFILE_WAIT_SECONDS", 3.0):
            driver._contact_profile_roots = lambda pane, ui: [profile, pane]  # type: ignore[assignment]
            driver.add_friend("13800138000", "你好")

        self.assertGreaterEqual(polls["n"], 3, "★ 得真的多轮询了几次，不是一次就撞上")
        self.assertIn("click:添加到通讯录", log, "多等一轮就等到了，不该报没搜到")

    def test_no_add_button_and_no_send_button_is_still_a_failure(self) -> None:
        """★ 「没有添加按钮」只说明加不了。没有正向证据就不许断定「已经是好友」——

        断错的代价是 `wechatAdded` 被写成真，而那个人根本没加上。
        """
        driver, _log, vendor_calls, _verify = self._driver(
            add_button_exists=False, send_button_exists=False,
        )

        with self.assertRaises(WeChatApiError) as caught:
            driver.add_friend("13800138000", "你好")

        self.assertIn("加不了", str(caught.exception))
        self.assertEqual(vendor_calls, [])

    def test_a_stranger_card_has_no_remark_so_the_top_line_is_the_nickname(self) -> None:
        """陌生人名片上没有备注可言 → 顶上那行就是昵称，`profile_name` 这条路上拿得准。"""
        driver, _log, _vendor, _verify = self._driver()

        result = driver.add_friend("13800138000", "你好", remark="张三-仓库工")

        self.assertFalse(result["already_friend"])
        self.assertEqual(result["profile_name"], "张三")

    def test_dry_run_says_which_name_would_be_used(self) -> None:
        from wxclient.wechat_api.uia import UiaWeChatDriver

        result = UiaWeChatDriver(dry_run=True).add_friend("138", "你好", remark="李四-司机")

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["session_name"], "李四-司机")
        self.assertIsNone(result["name"])
if __name__ == "__main__":
    unittest.main()
