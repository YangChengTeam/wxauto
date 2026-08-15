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

    def test_scheduling_rows_sit_two_to_a_line(self) -> None:
        """★ 调度那四项在设计稿上是两列。四行一列会把这一页拉得老长，还得滚。"""
        sched = settings_store.fields_of("sched")

        self.assertEqual([field.label for field in sched],
                         ["读消息间隔", "查新好友间隔", "工作时间窗", "每小时发送上限"])
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
        self.assertEqual(len(switches), 2, "开机即启动 + 启动后缩到托盘")

        var = self.page._vars["WXCLIENT_UI_AUTOSTART"]
        before = bool(var.get())
        switches[0].toggle()
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

        var = self.page._vars["WXCLIENT_UI_ACCOUNTS"]
        original = str(var.get())
        var.set("zk_weixin_改了")
        self.root.update()
        self.assertIn("有未保存的更改", self._labels(self.bar))

        var.set(original)
        self.root.update()
        self.assertIn("所有更改均已保存", self._labels(self.bar))

    def test_the_optional_field_placeholder_never_becomes_a_value(self) -> None:
        """★ 占位文字要是塞进变量，「业务端地址」就会被存成「用于换票，可选」这句话本身。"""
        field = next(f for f in settings_store.FIELDS if f.key.endswith("BUSINESS_API_URL"))

        self.assertTrue(field.placeholder)
        self.assertEqual(str(self.page._vars[field.key].get()), "")
        self.assertNotIn(field.placeholder, self.page._collect().values())

    def test_a_secret_is_still_masked_and_not_written_back_as_dots(self) -> None:
        """★ 版式换了，这条红线不能跟着换：没动过的密钥不许被掩码覆盖掉。"""
        key = "WXCLIENT_GATEWAY_INTERNAL_KEY"
        settings_store.save(self.dir, {key: "real-secret"})
        page = SettingsPage(self.root, self.dir)
        _KEEP_ALIVE.append(page)

        self.assertNotEqual(str(page._vars[key].get()), "real-secret", "界面上只显示掩码")
        self.assertEqual(page._collect().get(key), "real-secret", "没动过就保留原值")


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
