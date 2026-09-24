"""配置中心的版式与行为（`docs/setting.png`）。

和 `tests/test_ui_window.py` 一样：要真建 Tk 控件，没有 tkinter 的机器整体跳过；
像素级好不好看只能人在真机上看，这里断言的是**结构和行为**——
分段在不在、调度那段是不是两列、开关是不是开关、按钮有没有被滚动区吃掉、
「有未保存的更改」说的是不是实话、「测试连接」有没有把话说满。
"""

from __future__ import annotations

import gc
import tempfile
import unittest
from pathlib import Path

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    HAS_TK = True
except Exception:  # noqa: BLE001 - 没有 Tk / 没有显示环境都算「跑不了」
    HAS_TK = False

#: 见 `test_ui_window.py` 的同名清单：建过的 Tk 对象留到进程结束，
#: 免得析构落到别的线程上把整个进程 abort 掉。
_KEEP_ALIVE: list = []


def setUpModule() -> None:
    gc.disable()


def tearDownModule() -> None:
    gc.enable()


if HAS_TK:
    from wxclient.ui import settings_store, theme, widgets
    from wxclient.ui.logbuffer import LogBuffer
    from wxclient.ui.service import GatewayService
    from wxclient.ui.settings_page import SettingsPage
    from wxclient.ui.window import MainWindow


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，配置中心测不了")
class ConfigLookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.withdraw()
        self.page = SettingsPage(self.root, self.dir)
        self.page.pack(fill=tk.BOTH, expand=True)
        self.bar = self.page.build_action_bar(self.root)
        self.bar.pack(fill=tk.X)
        self.root.update()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.extend([self.root, self.page, self.bar])
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _labels(self, widget: tk.Misc | None = None) -> list[str]:
        found: list[str] = []
        for child in (widget or self.page).winfo_children():
            if isinstance(child, tk.Label):
                found.append(str(child.cget("text")))
            found.extend(self._labels(child))
        return found

    def test_the_parameters_are_split_into_the_three_sections_of_the_design(self) -> None:
        texts = self._labels()

        for title, desc in (("连接配置", "配置与 AI 端及业务端的连接信息"),
                            ("调度配置", "配置消息读取与任务调度规则"),
                            ("启动设置", "配置服务启动后的行为")):
            self.assertIn(title, texts)
            self.assertIn(desc, texts, "★ 段标题旁边那句说明也是设计稿的一部分")

    def test_about_shows_the_version_and_a_check_update_button(self) -> None:
        """「关于」一段：当前版本摆在明处，「检查更新」是这一页唯一的手动入口。"""
        clicks: list[str] = []
        page = SettingsPage(self.root, self.dir, version="0.1.53",
                            on_check_update=lambda: clicks.append("check"))
        _KEEP_ALIVE.append(page)
        texts = self._labels(page)

        self.assertIn("关于", texts)
        self.assertIn("当前版本 v0.1.53", texts)
        self.assertIn("检查更新", texts)

        page._check_update()
        self.assertEqual(clicks, ["check"])
        page.show_update_note("已是最新版本 v0.1.53", theme.GREEN)
        self.assertIn("已是最新版本 v0.1.53", self._labels(page))


    def test_scheduling_rows_sit_two_to_a_line(self) -> None:
        """★ 调度这几项在设计稿上是两列。一行一项会把这一页拉得老长，还得滚。"""
        sched = settings_store.fields_of("sched")

        self.assertEqual([field.label for field in sched],
                         ["读消息间隔", "查新好友间隔",
                          # 093（2026-09-01）：我们加的人通过了没有——多久搜一次、搜多久。
                          # 和上面「查新好友间隔」不是一回事：那条是**别人申请加我们**。
                          "查好友通过间隔", "查好友通过期限",
                          # 用户 2026-09-15：私聊档单独一格；总闸改名，免得和它混淆
                          "工作时间窗", "发送总上限", "私聊上限",
                          # 用户 2026-08-30 把这两条的标题改短了（两列排版，长标题会挤）
                          "加好友上限", "加好友最小间隔",
                          # 用户 2026-08-30：朋友圈一天几条也要能在界面上改
                          "朋友圈上限",
                          # 用户 2026-09-07：朋友圈自动点赞，一天几个 + 两趟隔多久
                          "朋友圈点赞上限", "朋友圈点赞最小间隔",
                          # 用户 2026-09-07：朋友圈评论一天几条「做成可配置」
                          "朋友圈评论上限", "朋友圈评论最小间隔",
                          # 用户 2026-09-08：本机自己巡看朋友圈，只碰新动态
                          "朋友圈巡看间隔", "巡看时自动点赞",
                          "巡看时自动评论", "朋友圈评论素材"])
        # 「来问陌生人」2026-09-24 已拆（监听名单只认同步来的），不许回来
        self.assertNotIn("来问陌生人", [field.label for field in settings_store.FIELDS])
        self.assertEqual(sched[0].unit, "秒", "单位贴在框里，不写进标签")
        self.assertNotIn("（秒）", sched[0].label)

        columns = next(s.columns for s in settings_store.SECTIONS if s.key == "sched")
        self.assertEqual(columns, 2)

    def test_the_boot_switches_are_real_switches(self) -> None:
        """★ 设计稿上是绿色胶囊开关；tk 的 Checkbutton 是个方框，差得远。"""
        switches = []

        def walk(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if isinstance(child, widgets.ToggleSwitch):
                    switches.append(child)
                self.assertNotIsInstance(child, tk.Checkbutton, "别再用方框复选框")
                walk(child)

        walk(self.page)
        # ★ 数目对着**布尔字段**数，别写死一个数字：加一个开关就要来改一次测试，
        #   而改的人多半只会把 2 改成 3，顺手把下面那条「点的是哪一个」也改错。
        bool_keys = [f.key for f in settings_store.FIELDS if f.kind == "bool"]
        self.assertEqual(len(switches), len(bool_keys), "每个布尔参数一枚胶囊开关")

        autostart = switches[bool_keys.index("WXCLIENT_UI_AUTOSTART")]
        var = self.page._vars["WXCLIENT_UI_AUTOSTART"]
        before = bool(var.get())
        autostart.toggle()
        self.assertNotEqual(bool(var.get()), before, "点一下要真的翻过来")

    def test_the_save_buttons_are_pinned_outside_the_scroll_area(self) -> None:
        """★ 动作条要钉在页面底部。跟着滚的话，参数一多就得先滚到底才找得到「保存」。"""
        window = MainWindow(tk.Toplevel(self.root), LogBuffer(), GatewayService(lambda: None),
                            data_dir=self.dir, probe_wechat=False)
        _KEEP_ALIVE.append(window)
        page = window._ensure_settings_page()

        bar = next(child for child in page.pack_slaves()
                   if "保存并重启服务" in "".join(self._labels(child)))

        self.assertEqual(str(bar.pack_info()["side"]), "bottom")
        self.assertIs(bar.master, page, "★ 它必须挂在页面上，不能挂在滚动区里面")
        self.assertIn("仅保存", self._labels(bar))

    def test_saving_keeps_you_on_the_page_you_were_editing(self) -> None:
        """★ 保存完不许把人踹到实时日志页（用户 2026-08-15）。

        接着改第二项还得自己点回来，还得再确认一遍刚填的存没存住。重启进度侧栏在报，
        想看日志点一下就到，不必替人做这个决定。
        """
        window = MainWindow(tk.Toplevel(self.root), LogBuffer(), GatewayService(lambda: None),
                            data_dir=self.dir, probe_wechat=False)
        _KEEP_ALIVE.append(window)
        window._show_page("settings")

        window._on_settings_saved(False)
        self.assertEqual(window._page, "settings", "「仅保存」之后还在配置中心")

        window._on_settings_saved(True)
        self.assertEqual(window._page, "settings", "「保存并重启服务」之后也是")

    def test_the_footer_says_saved_only_when_it_really_is(self) -> None:
        """★ 「所有更改均已保存」是句承诺。碰过就报「未保存」、改回原样还赖着不认，
        人就会开始不信这行字，那它还不如不写。
        """
        self.assertIn("所有更改均已保存", self._labels(self.bar))

        var = self.page._vars["WXCLIENT_GATEWAY_AI_WS_URL"]
        original = str(var.get())
        var.set("wss://改过的/v1/ws")
        self.root.update()
        self.assertIn("有未保存的更改", self._labels(self.bar))

        var.set(original)
        self.root.update()
        self.assertIn("所有更改均已保存", self._labels(self.bar))

    def test_a_number_below_the_gateway_floor_is_refused_and_not_saved(self) -> None:
        """读消息间隔填 10：不写文件、弹一句带地板的话（用户 2026-09-18 「配置改了没反应」）。"""
        from unittest import mock

        from wxclient.ui import dialog

        self.page._vars["WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS"].set("10")
        with mock.patch.object(dialog, "tell") as tell:
            self.page._save(restart=False)
        self.assertFalse(settings_store.settings_path(self.dir).exists(), "拒了就不该落盘")
        title, message = tell.call_args.args[1], tell.call_args.args[2]
        self.assertIn("不能这么填", title)
        self.assertIn("最低 30", message)
        self.assertEqual(tell.call_args.kwargs.get("tone"), "error")

    def test_a_placeholder_never_becomes_a_value(self) -> None:
        """★ 占位文字要是塞进变量，那一格就会被存成提示语本身。

        现在这一页没有带占位的字段了（登录相关的几格都撤了），所以这条守的是**机制**：
        `attach_placeholder` 挂的是一个盖在上面的 Label，不是往变量里写字。
        """
        from wxclient.ui import widgets

        entry = tk.Entry(self.root)
        var = tk.StringVar()
        widgets.attach_placeholder(entry, var, "请输入邮箱")
        self.root.update()

        self.assertEqual(var.get(), "", "占位只是画上去的，不进值")

    def test_no_credential_input_is_left_on_this_page(self) -> None:
        """★ 2026-08-15：内部 key 那一格撤了，换票改用登录签发的可撤销 Token。

        这一页再出现 secret 字段，就意味着又有一把凭证要人手抄进十台机器。
        """
        self.assertEqual([f.key for f in settings_store.FIELDS if f.secret], [])
        self.assertNotIn("WXCLIENT_GATEWAY_INTERNAL_KEY",
                         [f.key for f in settings_store.FIELDS])


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，配置中心测不了")
class ConnectionTestTests(unittest.TestCase):
    """「测试连接」只测得到网络层，文案就不许说成「连接正常」。"""

    def test_a_closed_port_is_reported_as_unreachable(self) -> None:
        # 1 号端口没人听，本机直接拒绝——不出网、不看运气
        self.assertIn("连不上", SettingsPage._probe("wss://127.0.0.1:1/v1/ws", timeout=2))

    def test_a_hostname_that_does_not_resolve_says_so_instead_of_blaming_the_network(self) -> None:
        """★ 「找不到这台主机」和「连不上」是两种毛病：前者是地址打错了，
        后者才该去查防火墙。报同一句话会把人支到错误的方向上。
        """
        self.assertEqual(SettingsPage._probe("这不是网址"), "找不到这台主机")
        self.assertEqual(SettingsPage._probe("http://"), "地址不像个网址")


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，配置中心测不了")
class PageSubtitleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = MainWindow(self.root, LogBuffer(), GatewayService(lambda: None),
                                 data_dir=Path(tempfile.mkdtemp()), probe_wechat=False)
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.window)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def test_the_subtitle_shows_up_on_the_config_page_and_nowhere_else(self) -> None:
        self.window._show_page("settings")

        self.assertEqual(self.window._subtitle.cget("text"), "管理连接、调度与启动设置")
        self.assertTrue(self.window._subtitle.winfo_manager(), "配置中心要摆出副标题")

        self.window._show_page("log")
        self.assertFalse(self.window._subtitle.winfo_manager(),
                         "★ 日志页没有副标题——留个空位会把标题顶歪")
        self.assertEqual(self.window._heading.cget("text"), "实时日志")
        self.assertEqual(str(self.window._heading.cget("fg")), theme.TEXT)


if __name__ == "__main__":
    unittest.main()
