"""窗口层的对抗性测试：专挑恶意/极端序列打，看哪里会崩、会卡、会说谎。

**为什么单独一个文件**：这些用例要真建 Tk 窗口，Linux 打包机/CI 上没有 tkinter，
`tests/test_ui.py` 里那些「不含 GUI 的部分」才是到处都能跑的。这一份在没有 Tk 的地方
整体跳过，在 Windows 真机上 `python -m unittest discover -s tests` 会照常带上。

⚠️ 像素级的点击命中测不了：SSH 会话的窗口站从不真映射窗口（`winfo_ismapped()` 恒为 0，
所有几何都是 1x1）。所以「遮罩挡住了点击」这条断言的是 Tk 里可判定的两件事——遮罩铺满
整页 + 层叠在参数页之上（`winfo children` 对非 toplevel 是按层叠顺序返回的）。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

try:
    import tkinter as tk

    _root = tk.Tk()
    _root.destroy()
    HAS_TK = True
except Exception:  # noqa: BLE001 - 没有 Tk / 没有显示环境都算「跑不了」
    HAS_TK = False

#: 建过的窗口一律**留到进程结束**，不让它们被 GC 掉。
#:
#: ★ 为什么：`MainWindow` 会起一条 `wx-ui-probe` 后台线程去探微信，那条线程握着 window
#: （→ 握着 Tk 解释器）。用例结束后主线程一撒手，最后一个引用就落在探测线程上，等它跑完
#: 释放时，`Tk.__del__` 就发生在**非主线程**——Tcl 直接 abort 整个进程
#: （`Tcl_AsyncDelete: async handler deleted by the wrong thread`），连测试汇总都来不及打印。
#: 窗口本身已经 destroy 过了，留个引用只是不让析构落到别的线程上。
_KEEP_ALIVE: list = []


def setUpModule() -> None:
    """这一段关掉循环 GC。

    ★ 真机上实测的坑：只要有**任何**后台线程在跑（服务状态轮询、微信探测），它一旦触发一次
    循环回收，回收到的 tk 对象（控件之间全是引用环）就会在**非主线程**上析构，Tcl 当场
    `Tcl_AsyncDelete: async handler deleted by the wrong thread` abort 整个进程——测试跑到
    一半没了，连汇总都打不出来，看起来像「测试莫名其妙挂了」。
    引用计数照常工作，关掉的只是环的回收；一轮测试多留几个窗口在内存里，无所谓。
    """
    import gc

    gc.disable()


def tearDownModule() -> None:
    import gc

    gc.enable()

if HAS_TK:
    from wxclient.logging_config import configure_logging
    from wxclient.ui import theme
    from wxclient.ui.logbuffer import LogBuffer, LogPipeline
    from wxclient.ui.service import GatewayService, GatewayStatus, ServiceState
    from wxclient.ui.window import MainWindow

if True:  # 这几样不碰 Tk，没有显示环境也 import 得动
    from typing import Any

    from wxclient.ui import auth as auth_module
    from wxclient.ui import settings_store as settings_store_module
    from wxclient.ui.client_api import ApiError
    from wxclient.ui.token_store import Protector, TokenStore

LOGIN_OK = {
    "accessToken": "lxz_rc_token",
    "sessionId": "sess-1",
    "user": {"id": "u-1", "name": "陈敏", "email": "hr@example.com"},
    "agent": {"id": "agent-1", "name": "陈敏的招聘分身"},
}


class StubApi:
    """假的客户端接口。登录页只关心「登进去了没有、报了什么错」。"""

    def __init__(self) -> None:
        self.base_url = "https://business.test"
        self.error: Exception | None = None
        self.calls: list[str] = []

    def login(self, email: str, password: str, **_kwargs: Any) -> dict:
        self.calls.append("login")
        if self.error is not None:
            raise self.error
        return dict(LOGIN_OK)

    def logout(self, token: str) -> dict:
        self.calls.append("logout")
        return {"success": True}


class _NoDiskProtector(Protector):
    """测试里不往磁盘写 Token，也不需要真 DPAPI。"""

    available = False
    name = "test"


def make_auth(data_dir: Path, api: StubApi, *, base_url: str = "https://business.test"):
    api.base_url = base_url  # ClientAuth.configured 看的就是它
    return auth_module.ClientAuth(base_url, data_dir, client_version="test",
                                  api=api, store=TokenStore(data_dir, _NoDiskProtector()))


def settings_store_fields():
    return settings_store_module.FIELDS


class _EvilRunner:
    """每一个状态读法都炸。界面不许被它带崩，也不许因此整排清空。"""

    def connected_count(self) -> int:
        raise RuntimeError("炸")

    def pending_backlog(self) -> int:
        raise RuntimeError("炸")

    def watched_peers(self) -> list[str]:
        raise RuntimeError("炸")

    @property
    def connect_error(self) -> str:
        raise RuntimeError("炸")

    async def run(self) -> None:
        import asyncio

        await asyncio.Event().wait()

    async def stop(self) -> None:
        return None


class _EvilGateway:
    runtime = type("R", (), {"start": lambda self: None, "stop": lambda self: None})()
    settings = type("S", (), {"ai_ws_url": "wss://example/v1/ws"})()
    allow_real_actions = False

    def __init__(self) -> None:
        self.runner = _EvilRunner()

    @property
    def accounts(self) -> tuple[str, ...]:
        raise RuntimeError("连账号都炸")


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，窗口层测不了")
class WindowAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.service = GatewayService(lambda: None)
        self.window = MainWindow(
            self.root, LogBuffer(), self.service,
            data_dir=Path(tempfile.mkdtemp()), probe_wechat=False,
        )
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.window)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def test_hammering_the_nav_does_not_pile_up_widgets(self) -> None:
        """疯点切页 300 次：不许崩，也不许在页容器底下攒控件。"""
        for index in range(300):
            self.window._locked = bool(index % 3)
            self.window.open_settings()
            self.window.open_log()
            self.window._render_busy()

        # 只该有三个：日志页 + 配置中心 + 遮罩
        self.assertLessEqual(len(self.window._pages.winfo_children()), 3)

    def test_transition_covers_the_settings_page_instead_of_kicking_you_out(self) -> None:
        """★ 转场态停在参数页：盖住就行，别把人正在填的东西丢掉。"""
        self.window.open_settings()
        self.window._locked = True
        self.window._render_overlay()

        self.assertEqual(self.window._page, "settings", "不该把人赶走")
        info = self.window._overlay.place_info()
        self.assertEqual((info.get("relwidth"), info.get("relheight")), ("1", "1"))
        order = self.window._pages.winfo_children()
        self.assertGreater(
            order.index(self.window._overlay), order.index(self.window._config_page),
            "遮罩必须层叠在配置中心之上，否则点击照样落到「保存并重启服务」上",
        )
        self.assertTrue(self.window._overlay.bind("<Button-1>"), "遮罩要自己吞掉鼠标事件")

        self.window._locked = False
        self.window._render_overlay()
        self.assertEqual(self.window._overlay.winfo_manager(), "", "解锁后遮罩要收起")

    def test_the_settings_page_is_prebuilt_so_the_first_click_is_cheap(self) -> None:
        """★ 建控件是「很多次小 Tcl 调用」，网关忙着操作微信时主线程抢不到 GIL，最吃亏。

        所以启动后趁空闲先建好，用户点下去只剩 pack/unpack。
        """
        self.assertIsNone(self.window._config_page, "刚起来时还没建")

        self.window._prebuild_settings_page()

        page = self.window._config_page
        self.assertIsNotNone(page, "预热之后就该有了")
        self.assertEqual(page.winfo_manager(), "", "预热只建不显示")
        self.window.open_settings()
        self.assertIs(self.window._config_page, page, "真点的时候直接用预热那个")

    def test_the_nav_is_just_log_and_config(self) -> None:
        """★ 只剩两页（用户 2026-08-15：「计划中心不需要，是发给我们 agent 端的」）。

        参数设置→配置中心；监听名单本来就是 AI 端说了算、本机改不了，归进配置中心当
        只读的一节。计划归 AI 端排、经 WS 推给网关执行，桌面端不摆这一页。
        """
        self.assertEqual([item.cget("text") for item in self.window._nav.values()],
                         ["实时日志", "配置中心"])
        self.assertNotIn("plans", self.window._nav, "计划中心已删")
        self.assertNotIn("watch", self.window._nav, "监听名单不再是单独一项")
        self.assertFalse(hasattr(self.window, "_watch_label"), "侧栏不该再铺名单")

        self.window._show_page("settings")

        self.assertEqual(self.window._page, "settings")
        self.assertEqual(self.window._heading.cget("text"), "配置中心")
        self.assertEqual(self.window._log_page.winfo_manager(), "", "日志页要让位")
        # 日志页专属的那一排不该跟着跑出来
        self.assertEqual(self.window._filter_bar.winfo_manager(), "")
        self.assertEqual(self.window._live_row.winfo_manager(), "")

    def test_the_log_page_puts_live_status_under_the_title(self) -> None:
        """★ 用户 2026-08-15：日志页也照配置中心那样分两行，「实时更新」放到标题下面。

        断言的是「同一个纵向盒子里、排在标题后面」——横着挤在标题右边就不满足。
        """
        self.window._show_page("log")

        self.assertIs(self.window._live_row.master, self.window._heading.master)
        slaves = self.window._heading.master.pack_slaves()
        self.assertLess(slaves.index(self.window._heading), slaves.index(self.window._live_row))
        self.assertEqual(str(self.window._live_row.pack_info()["side"]), "top")
        self.assertIn("实时更新", self.window._live_text.cget("text"))

    def test_a_page_that_no_longer_exists_falls_back_to_the_log(self) -> None:
        """★ 托盘/快捷方式里可能还留着老入口，别把内容区留成一片白。"""
        self.window._show_page("plans")

        self.assertEqual(self.window._page, "log")
        self.assertEqual(self.window._log_page.winfo_manager(), "pack")

    def test_the_watch_list_lists_the_actual_names_inside_the_config_page(self) -> None:
        """名单是「谁会被自动回复」的唯一判据，配置中心里必须看得到具体是谁。"""
        from wxclient.ui.service import GatewayStatus

        self.window.service._snapshot = (ServiceState.RUNNING, None)
        self.window.service._status = GatewayStatus(watched=("张三", "李四", "王五"))
        self.window._show_page("settings")

        def texts(widget) -> list[str]:
            found = []
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    found.append(str(child.cget("text")))
                found.extend(texts(child))
            return found

        shown = texts(self.window._watch_card)
        for name in ("张三", "李四", "王五"):
            self.assertIn(name, shown)
        self.assertEqual(self.window._watch_count.cget("text"), "3 个会话")
        # 一人一行（`docs/setting2.png`）：名字 + 「微信会话」+ 右边「监听中」
        self.assertEqual(shown.count("微信会话"), 3)
        self.assertEqual(shown.count("监听中"), 3)

    def test_the_watch_list_counts_down_to_the_next_read(self) -> None:
        """★ 「下一次什么时候读消息」是这一页最常被问的一件事（用户 2026-08-15）。

        倒计时读的是网关**真实排的那一觉**（含抖动、含快慢档）；没排期就别编——
        窗外说窗外，正在读说正在读。
        """
        from wxclient.ui.service import GatewayStatus

        self.window.service._snapshot = (ServiceState.RUNNING, None)
        self.window.service._status = GatewayStatus(watched=("张三",), next_read_seconds=134.0)
        self.window._show_page("settings")
        self.assertEqual(self.window._watch_next.cget("text"), "下次读消息 02:14")

        self.window.service._status = GatewayStatus(watched=("张三",), quiet_hours=True,
                                                    next_read_seconds=None)
        self.window._render_watch_page()
        self.assertIn("不在工作时间窗", self.window._watch_next.cget("text"))

        self.window.service._status = GatewayStatus(watched=("张三",), next_read_seconds=None)
        self.window._render_watch_page()
        self.assertIn("正在读", self.window._watch_next.cget("text"))

        # 没起服务时一个字都不说——这会儿本来就不会读
        self.window.service._snapshot = (ServiceState.STOPPED, None)
        self.window._render_watch_page()
        self.assertEqual(self.window._watch_next.cget("text"), "")

    def test_the_sync_button_is_a_placeholder_that_admits_it(self) -> None:
        """★ 名单以后能从线上同步（用户 2026-08-15 要求先留位）。后端那头还没有，
        所以按钮是灰的，点了要说清「还没接上」——摆一个点了什么都不发生的按钮比没有更糟。
        """
        self.window._show_page("settings")

        self.window._sync_watchlist()

        hint = self.window._watch_hint.cget("text")
        self.assertIn("还没接", hint)
        self.assertNotIn("同步成功", hint, "没同步就别说同步过")

    def test_an_empty_watch_list_says_nothing_will_be_answered(self) -> None:
        """空名单分两种，别混为一谈：没起服务是正常的，**跑着却空**才是要命的。"""
        self.window._show_page("settings")
        self.assertIn("没在跑", self.window._watch_hint.cget("text"), "没启动时别吓人")

        # 跑起来了、名单却是空的：意味着一条消息都不会回，必须说出来
        self.window.service._snapshot = (ServiceState.RUNNING, None)
        self.window._render_watch_page()

        self.assertIn("一条消息都不会回", self.window._watch_hint.cget("text"))

    def test_the_selected_page_is_marked_with_a_green_bar(self) -> None:
        """★ 暗纹背景上「深一档的底色」几乎看不出来，绿条才是「我在这儿」（`docs/main.png`）。"""
        for page in ("log", "settings", "log"):
            self.window._show_page(page)

            lit = [key for key, mark in self.window._nav_marks.items()
                   if str(mark.cget("bg")) == theme.GREEN_BRIGHT]
            self.assertEqual(lit, [page], f"停在 {page} 时只有它亮绿条")

    def test_locked_nav_refuses_the_settings_page(self) -> None:
        """转场期间「参数设置」点不动——托盘那条路也一样。"""
        self.window._locked = True
        self.window.open_settings()
        self.assertEqual(self.window._page, "log")

    def test_an_evil_gateway_cannot_freeze_or_blank_the_sidebar(self) -> None:
        """状态读法全炸的网关：界面照旧刷得动，拿到的是缺省值而不是崩溃。"""
        service = GatewayService(lambda: _EvilGateway())
        window = MainWindow(
            tk.Tk(), LogBuffer(), service,
            data_dir=Path(tempfile.mkdtemp()), probe_wechat=False,
        )
        self.addCleanup(window.destroy)
        service.start()
        self.assertTrue(service.wait_ready(timeout=10))
        self.addCleanup(service.stop, 5)

        self.assertEqual(service.refresh_status_now(), GatewayStatus())
        begin = time.monotonic()
        for _ in range(100):
            window.refresh_state()
        self.assertLess(time.monotonic() - begin, 2.0)

    def test_a_corrupt_settings_file_still_opens_the_page(self) -> None:
        """参数文件被写坏：进得去、reload 也不炸。"""
        from wxclient.ui import settings_store

        data_dir = Path(tempfile.mkdtemp())
        settings_store.settings_path(data_dir).write_text(
            "这不是配置\n=没有键\nWXCLIENT_UI_ACCOUNTS=\n", encoding="utf-8",
        )
        window = MainWindow(
            tk.Tk(), LogBuffer(), GatewayService(lambda: None),
            data_dir=data_dir, probe_wechat=False,
        )
        self.addCleanup(window.destroy)

        window.open_settings()
        window.open_log()
        window.open_settings()  # 走 reload()

        self.assertEqual(window._page, "settings")

    def test_destroy_cancels_every_pending_callback(self) -> None:
        """★ 销毁后不许还有定时回调放炮，否则退出时 stderr 一串 invalid command name。"""
        self.root.update()
        handles = set(self.window._pending_after)
        self.assertTrue(handles, "销毁前本就该排着心跳")

        self.window.destroy()

        self.assertFalse(self.window._pending_after)
        for handle in handles:
            with self.assertRaises(tk.TclError, msg=f"{handle} 还留在 Tcl 里"):
                self.root.tk.call("after", "info", handle)


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，窗口层测不了")
class StallWatchdogTests(unittest.TestCase):
    """卡顿看门狗：真卡了要报，启动那段和正常心跳都不许误报。

    ★ 断言要看 **LogBuffer**（也就是实时日志页上真正显示的东西），不要自己往 root logger
    上挂 handler：微信探测线程首次 import pywinauto/comtypes 时，第三方会把整个 logging
    重置掉——挂上去的 handler 会凭空消失，"断言没有告警" 于是永远成立（假绿）。
    `LogPipeline.ensure()` 每拍会把通道接回来，这里就走这条真实链路。
    """

    def setUp(self) -> None:
        self.buffer = LogBuffer()
        self.pipeline = LogPipeline(self.buffer, configure_logging)
        self.pipeline.install()
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = MainWindow(
            self.root, self.buffer, GatewayService(lambda: None),
            data_dir=Path(tempfile.mkdtemp()), probe_wechat=False,
            log_pipeline=self.pipeline,
        )
        self.addCleanup(self.window.destroy)

    def _stalled(self) -> bool:
        return any("界面卡了一下" in line for line in self.buffer.snapshot())

    def test_the_startup_gap_is_not_reported_as_a_stall(self) -> None:
        """★ `__init__` 那一拍到 mainloop 之间隔着建托盘 + 装配网关（真机 4.5 秒）。

        那段「迟到」是启动本来的样子。照报的话每次启动都刷一条假警报，真警报就没人信了。
        """
        time.sleep(1.3)
        self.window._tick()

        self.assertFalse(self._stalled(), f"启动那段不该报：{self.buffer.snapshot()[-3:]}")

        # 阳性对照：证明这条链路真收得到告警，否则上面那句是「永远成立」的空断言。
        # 这里必须睡够——阈值是 0.6 秒，而「迟到」= 间隔 - 1 秒的心跳周期，睡 1.3 秒只迟到 0.3 秒。
        self.window._tick()
        time.sleep(2.0)
        self.window._tick()
        self.assertTrue(self._stalled(), f"宽限期过了之后就该报了：{self.buffer.snapshot()[-3:]}")

    def test_a_real_stall_is_reported_with_how_long(self) -> None:
        self.window._tick()   # 第 2 拍，仍在宽限期内
        self.window._tick()   # 第 3 拍起才开始盯
        time.sleep(2.0)
        self.window._tick()

        self.assertTrue(self._stalled(), "真卡了必须留痕")
        lines = self.buffer.snapshot()
        self.assertTrue(any("stalled_ms" in line for line in lines), f"要带上卡了多久：{lines[-3:]}")


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，窗口层测不了")
class LoginGateTests(unittest.TestCase):
    """★ 登录是一道真门（用户 2026-08-15：「用户需要登录 登录之后 获取参数」）。

    门的定义只有一条：**没登录就看不见东西**。所以这里测的不是「登录页画得好不好看」，
    而是「它有没有真的挡在前面」「密码错了会不会放人进来」「退出登录之后门有没有关回去」。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.withdraw()
        self.service = GatewayService(lambda: None)
        self.window = MainWindow(
            self.root, LogBuffer(), self.service, data_dir=self.dir, probe_wechat=False,
        )
        self.api = StubApi()
        self.auth = make_auth(self.dir, self.api)
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.window)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _pump(self, predicate, timeout: float = 5.0) -> bool:
        """跑**真的** mainloop 直到条件成立。

        ⚠️ 不能写成 ``while: root.update()``：登录结果是从后台线程 ``after`` 回来的，
        而 ``after`` 跨线程只有在 mainloop 真在跑的时候才成立，否则直接
        ``RuntimeError: main thread is not in main loop``——测出来的是测试自己的毛病。
        （同一个坑在量 GIL 那次也踩过，见 memory「量 GIL 必须用真 mainloop」。）
        """
        deadline = time.monotonic() + timeout
        done = {"ok": False}

        def check() -> None:
            if predicate():
                done["ok"] = True
                self.root.quit()
            elif time.monotonic() > deadline:
                self.root.quit()
            else:
                self.root.after(20, check)

        self.root.after(20, check)
        self.root.mainloop()
        return done["ok"]

    def test_the_door_covers_the_whole_window(self) -> None:
        self.window.require_login(self.auth)

        self.assertFalse(self.window.logged_in, "还没登录")
        login = self.window._login
        self.assertEqual(login._button.cget("text"), "登录")
        self.assertIs(self.root.winfo_children()[-1], login, "登录页要压在最上面")
        self.assertEqual(login.place_info().get("relwidth"), "1", "要盖满整个窗口")

    def test_a_machine_without_a_server_address_asks_for_one_on_the_login_page(self) -> None:
        """★ 全新的一台机器：地址没配 → 登录请求发不出去，而这时人还进不了配置中心。

        所以那一格必须摆在登录页上，填一次就落进 `gateway.env`（**同一个键**，
        不另开一份「登录用的地址」）。
        """
        from wxclient.ui import settings_store

        self.window.require_login(make_auth(self.dir, StubApi(), base_url=""))
        login = self.window._login
        self.assertTrue(login._needs_server, "没配地址就得当场问")

        login._server.set("https://business.test")
        login._account.set("hr@example.com")
        login._password.set("pw")
        login.submit()
        self._pump(lambda: self.window.logged_in)

        saved = settings_store.load(self.dir)
        self.assertEqual(saved["WXCLIENT_GATEWAY_BUSINESS_API_URL"], "https://business.test")

    def test_the_wrong_password_keeps_you_out_and_says_why(self) -> None:
        self.api.error = ApiError("邮箱或密码错误", code="INVALID_CREDENTIALS", status=401)
        self.window.require_login(self.auth)
        login = self.window._login
        login._account.set("hr@example.com")
        login._password.set("这不是密码")

        login.submit()

        self.assertTrue(self._pump(lambda: bool(login._error.cget("text"))), "错误要显示出来")
        self.assertEqual(login._error.cget("text"), "邮箱或密码错误")
        self.assertFalse(self.window.logged_in, "★ 密码错了绝不能放进来")
        self.assertEqual(login._account.get(), "hr@example.com", "邮箱别给人清掉，改密码再来就行")

    def test_logging_in_takes_the_door_away_and_shows_who_you_are(self) -> None:
        seen = []
        self.window.require_login(self.auth, on_authenticated=seen.append)
        login = self.window._login
        login._account.set("hr@example.com")
        login._password.set("pw")

        login.submit()

        self.assertTrue(self._pump(lambda: self.window.logged_in), "登录之后门要撤掉")
        self.assertEqual([identity.account for identity in seen], ["agent-1"],
                         "★ 记账用的是分身 ID，不是手抄的微信号")
        self.assertEqual(self.window._page, "log", "登进来落在实时日志")
        self.assertIn("陈敏", self.window._who.cget("text"), "侧栏要说清是谁在用")

    def test_logging_out_also_stops_the_service(self) -> None:
        """★ 「退出登录和退出服务是一个意思，只留一个退出登录」（用户 2026-08-15）。

        服务本来就只在登录之后才起，所以「不再用了」只有一种表达：退出登录 = 停网关 + 回登录页。
        """
        stopped: list[bool] = []
        revoked: list[bool] = []
        self.service.stop_async = lambda **_kwargs: stopped.append(True)  # type: ignore[method-assign]
        self.window.set_login_context(self.auth, on_logout=lambda: revoked.append(True))
        self.window.finish_login(self.auth.login("hr@example.com", "pw"))
        self.service._snapshot = (ServiceState.RUNNING, None)

        self.window.logout()

        self.assertTrue(stopped, "退出登录必须把网关一起停掉")
        self.assertFalse(self.window.logged_in, "门要关回去")
        self.assertEqual(self.window._who.cget("text"), "", "身份要抹掉")
        self.assertTrue(self._pump(lambda: bool(revoked)),
                        "★ 还要告诉服务端撤销这台设备的会话，否则企业端一直显示在线")

    def test_the_stopping_notice_goes_away_after_logout(self) -> None:
        """★ 2026-08-15 真机：退出登录后侧栏一直转圈「正在停止服务，请稍候」，停完也不消失。

        原因是 logout 显式 set_busy 了一句，而这条路没有「停完了」的回调去清它——显式提示
        优先级最高，于是它盖住了状态派生的提示，永远不落幕。
        """
        self.service.stop_async = lambda **_kwargs: None  # type: ignore[method-assign]
        self.window.set_login_context(self.auth)
        self.window.finish_login(self.auth.login("hr@example.com", "pw"))
        self.service._snapshot = (ServiceState.RUNNING, None)

        self.window.logout()
        self.service._snapshot = (ServiceState.STOPPING, None)
        self.window._render_busy()   # 真跑时是每秒一次的 _tick 在渲染
        self.assertIn("正在停止服务", self.window._busy_label.cget("text"),
                      "停机过程中该有提示——转场那几秒最像卡死")

        self.service._snapshot = (ServiceState.STOPPED, None)
        self.window._render_busy()
        self.assertFalse(self.window._busy_label.winfo_manager(),
                         "★ 停完了提示必须自己消失，不能一直挂在那儿")

    def test_the_sidebar_has_exactly_one_way_out(self) -> None:
        """摆两个按钮做同一件事，只会让人猜它们差在哪。"""
        labels = [child.cget("text") for child in self.window._sidebar.winfo_children()
                  if isinstance(child, tk.Label)]

        self.assertIn("退出登录", labels)
        self.assertNotIn("退出服务", labels)

    def test_remembering_the_account_never_remembers_the_password(self) -> None:
        from wxclient.ui import auth

        self.window.require_login(self.auth)
        login = self.window._login
        login._account.set("hr@example.com")
        login._password.set("hunter2")
        login._remember.set(True)

        login.submit()
        self.assertTrue(self._pump(lambda: self.window.logged_in))

        raw = (self.dir / auth.UI_STATE_FILE).read_text(encoding="utf-8")
        self.assertIn("hr@example.com", raw)
        self.assertNotIn("hunter2", raw)


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，窗口层测不了")
class ConfigCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.withdraw()
        self.service = GatewayService(lambda: None)
        self.window = MainWindow(
            self.root, LogBuffer(), self.service, data_dir=self.dir, probe_wechat=False,
        )
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.window)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def test_the_settings_page_has_exactly_one_source_of_truth(self) -> None:
        """★ 2026-08-15：正式的客户端接口**不下发配置**，所以「后端下发 + 本机覆盖」
        那一层整个删了。

        原来那层的坑还留在这条测试的记忆里：保存一次就把满屏的值全冻进本机文件，
        从此后端下发什么都不生效，而界面上一切正常。现在只有一份 `gateway.env`，
        再往回加第二份之前，先想清楚谁赢。
        """
        from wxclient.ui import settings_store

        self.assertFalse(hasattr(settings_store, "load_remote"), "第二份配置已经删了")
        page = self.window._ensure_settings_page() and self.window._settings_page
        self.assertFalse(hasattr(page, "_drop_same_as_remote"))
        self.assertFalse((self.dir / "gateway.remote.env").exists())

    def test_the_settings_page_no_longer_asks_for_hand_copied_credentials(self) -> None:
        """★ 微信号、内部 key、本地票据文件三格都撤了（2026-08-15）。

        微信号改用登录返回的分身 ID，内部 key 改用可撤销的客户端 Token。装十台机器抄十遍
        凭证这件事，就是靠这三格消失才真正结束的——留一格都会有人继续抄。
        """
        keys = [field.key for field in settings_store_fields()]

        self.assertNotIn("WXCLIENT_UI_ACCOUNTS", keys)
        self.assertNotIn("WXCLIENT_GATEWAY_INTERNAL_KEY", keys)
        self.assertNotIn("WXCLIENT_GATEWAY_LOCAL_TICKET_FILE", keys)
        self.assertIn("WXCLIENT_GATEWAY_BUSINESS_API_URL", keys, "登录和换票都要它")

    def test_connection_trouble_still_shows_up_after_the_banner_was_removed(self) -> None:
        """★ 顶部那条告警条按用户要求撤了（2026-08-15），但「连不上」不能跟着消失——
        侧栏那行仍要说，否则界面上一切正常、消息其实一条都没送出去。
        """
        self.service._snapshot = (ServiceState.RUNNING, None)
        self.service._status = GatewayStatus(connected=1, accounts=1)
        self.window.refresh_state()
        self.assertFalse(hasattr(self.window, "_banner"), "告警条已删")
        self.assertEqual(self.window._rows["gateway"].cget("text"), "已连接")

        self.service._status = GatewayStatus(connected=0, accounts=1, connect_error="认证失败")
        self.window.refresh_state()
        self.assertEqual(self.window._rows["gateway"].cget("text"), "连不上")

        self.service._status = GatewayStatus(connected=1, accounts=1, backlog=3)
        self.window.refresh_state()
        self.assertIn("欠 3 批", self.window._rows["gateway"].cget("text"))


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，窗口层测不了")
class LoginLookTests(unittest.TestCase):
    """登录页那两处被用户当场指出来的毛病（2026-08-15）：背景没铺满、输入框是乱的。"""

    def setUp(self) -> None:
        from wxclient.ui.login_page import LoginPage

        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.geometry("1180x720")
        self.page = LoginPage(self.root, make_auth(self.dir, StubApi()),
                              data_dir=self.dir, on_authenticated=lambda _i: None)
        self.page.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.root.update()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.extend([self.root, self.page])
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_the_background_covers_whatever_shape_the_panel_is(self) -> None:
        """★ background-size: cover —— 等比放大到盖住，多的居中裁掉，别留接缝。

        原来贴的是一张预先切死的图，面板宽高比和它对不上，左栏上方就是一道明显的横缝。
        """
        from wxclient.ui import icons

        for width, height in ((590, 720), (300, 900), (1200, 300)):
            photo = icons.cover_photo(self.root, icons.PANEL_BG_PATH, width, height)
            self.assertIsNotNone(photo, "PIL 在的时候必须能铺")
            self.assertEqual((photo.width(), photo.height()), (width, height),
                             f"{width}x{height} 要正好铺满，不多不少")

    def test_a_silly_size_does_not_crash_the_login_page(self) -> None:
        from wxclient.ui import icons

        self.assertIsNone(icons.cover_photo(self.root, icons.PANEL_BG_PATH, 0, 0))
        self.assertIsNone(icons.cover_photo(self.root, Path("这文件不存在.png"), 100, 100))

    def test_the_placeholder_never_becomes_the_account_you_submit(self) -> None:
        """★ 灰字提示如果是塞进 StringVar 的，不点那一格直接回车，提交的账号就成了
        「请输入账号」——表现是「密码明明没打错却登不进去」，查起来极贵。
        """
        self.root.update()

        self.assertEqual(self.page._account.get(), "", "占位文字不许进变量")
        self.assertEqual(self.page._password.get(), "")

    def test_the_password_row_lays_the_eye_out_beside_the_entry(self) -> None:
        """★ 「文本框是乱的」的真因：眼睛在已经占满整行的 Entry 之后才 side=RIGHT，
        跟 Entry 抢地方，于是整行错位。这里盯的就是「眼睛先占位、Entry 再 expand」。
        """
        entries: list[tk.Entry] = []

        def walk(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if isinstance(child, tk.Entry):
                    entries.append(child)
                walk(child)

        walk(self.page)
        self.assertGreaterEqual(len(entries), 2, "至少有账号和密码两个输入框")

        eyes = 0
        for entry in entries:
            info = entry.pack_info()
            self.assertEqual(info["side"], "left")
            # expand 有的 Tk 版本回字符串 "1"、有的回整数 1，别按类型比
            self.assertTrue(int(info["expand"]), "输入框要吃掉这一行剩下的宽度")
            # 决定布局的是**打包顺序**（pack_slaves），不是创建顺序（winfo_children）：
            # side=RIGHT 要在 Entry expand 吃掉整行之前把自己那块占下来。
            slaves = entry.master.pack_slaves()
            for slave in slaves:
                if isinstance(slave, tk.Label) and slave.cget("text") == "👁":
                    eyes += 1
                    self.assertLess(slaves.index(slave), slaves.index(entry),
                                    "眼睛必须先 pack，先把右边那块占下来")
                    self.assertEqual(slave.pack_info()["side"], "right")
        self.assertEqual(eyes, 1, "正常登录态只有密码那一行有眼睛")


@unittest.skipUnless(HAS_TK, "没有 Tk，界面样式测不了")
class LogLookTests(unittest.TestCase):
    """实时日志页要长成 `docs/main.png` 那样（用户 2026-08-15：「样式不对，日志的显示也不符合」）。

    像素级好不好看只能人在控制台上看（这条 SSH 上截不了图），所以这里断言的是**结构**：
    列有没有对齐、警告行有没有铺底和左边那根竖条、参数有没有压灰、选中的档位有没有绿框。
    这几条恰好就是当时被指出来的那几处。
    """

    def setUp(self) -> None:
        from wxclient.ui.logline import parse
        from wxclient.ui.logtable import LogTable

        self.parse = parse
        self.root = tk.Tk()
        self.root.withdraw()
        self.table = LogTable(self.root)
        self.table.pack(fill=tk.BOTH, expand=True)
        self.root.update()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.extend([self.root, self.table])
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _feed(self, *raw: str) -> None:
        self.table.append([self.parse(line) for line in raw])
        self.root.update()

    def _dump(self) -> str:
        return self.table._text.get("1.0", tk.END)

    def test_the_columns_have_a_header_that_lines_up_with_the_rows(self) -> None:
        """★ 表头和正文各写各的偏移就会错位——这里逼它们用同一组数字。"""
        heads = {}

        def walk(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if isinstance(child, tk.Label) and child.place_info():
                    heads[child.cget("text")] = int(child.place_info()["x"])
                walk(child)

        walk(self.table)
        self.assertEqual(set(heads), {"时间", "级别", "消息"})

        from wxclient.ui import logtable

        tabs = [int(float(tab)) for tab in self.root.tk.splitlist(self.table._text.cget("tabs"))]
        pad = logtable._PAD_X
        self.assertEqual(len(tabs), 3, "三列三个制表位——时间列也得有一个，见下一条")
        self.assertEqual(heads["时间"], pad + tabs[0], "「时间」要正对时间列")
        self.assertEqual(heads["级别"], pad + tabs[1], "「级别」要正对级别列")
        self.assertEqual(heads["消息"], pad + tabs[2], "「消息」要正对消息列")
        self.assertLess(heads["时间"], heads["级别"], "时间列在最左边")
        # ★ 用户 2026-08-15：「文字离左边不要贴着」——时间列要在色条右边空出一段
        self.assertGreaterEqual(heads["时间"] - pad - self.table._bar_w, logtable._BAR_GAP)

    def test_a_warning_row_gets_both_a_wash_and_a_bar_and_an_info_row_gets_neither(self) -> None:
        """★ 设计稿上警告行是「浅橙底 + 左边一根橙竖条」，光换字色扫一眼看不出来。"""
        self._feed(
            '2026-08-15 13:29:13 [警告] 站消息失败 account="zk_weixin"',
            "2026-08-15 13:29:32 [信息] 已读取本地联调票",
        )

        warn_line, info_line = "1.0", "3.0"  # 中间第 2 行是分隔线
        self.assertIn("row_warn", self.table._text.tag_names(f"{warn_line}+2c"))
        self.assertIn("bar_warn", self.table._text.tag_names(warn_line))
        self.assertNotIn("row_warn", self.table._text.tag_names(f"{info_line}+2c"))
        # 普通行行首那个空格也带 bar_ tag（★不带的话它按正文字号排版，宽出几像素，
        # 时间列就跟警告行错开了），但它只压窄不着色——看上去仍然没有竖条。
        self.assertNotIn("bar_warn", self.table._text.tag_names(info_line))
        self.assertFalse(self.table._text.tag_cget("bar_info", "background"),
                         "普通行不该有看得见的竖条")

        self.assertTrue(self.table._text.tag_cget("bar_warn", "background"))
        self.assertNotEqual(self.table._text.tag_cget("bar_warn", "background"),
                            self.table._text.tag_cget("row_warn", "background"),
                            "竖条要比整行底色重，否则等于没有")

    def test_the_level_column_is_a_dot_plus_an_uppercase_level(self) -> None:
        self._feed("2026-08-15 13:29:13 [警告] 站消息失败")

        self.assertIn("● WARN", self._dump())

    def test_the_structured_arguments_are_greyed_out_but_still_there(self) -> None:
        """★ 一行日志里最该先读到的是那句人话；参数压灰，但一个字都不能丢。"""
        self._feed('2026-08-15 13:29:33 [警告] AI 端连接不可用 account="zk_weixin"')

        text = self._dump()
        self.assertIn('account="zk_weixin"', text, "灰是灰，内容不许吞")

        ranges = self.table._text.tag_ranges("kv")
        self.assertTrue(ranges, "参数要单独打上 kv 标签才灰得掉")
        self.assertEqual(self.table._text.get(*ranges[:2]), 'account="zk_weixin"')
        self.assertEqual(self.table._text.tag_cget("kv", "foreground"), theme.TEXT_MUTED)

    def test_one_log_is_one_line_and_the_rest_is_a_tooltip(self) -> None:
        """★ 长日志换行会把屏幕吃光：四行一条，一屏就剩两条，扫不动了。

        所以不换行、截断显示，整条内容改由悬停气泡给（用户 2026-08-15）。
        """
        long_message = "AI 端连接不可用" + "，重试" * 120
        self._feed(f"2026-08-15 13:29:33 [警告] {long_message}")

        self.assertEqual(str(self.table._text.cget("wrap")), "none", "不许换行")

        record = self.table._record_at(1)
        self.assertIsNotNone(record)
        tip = self.table.tip_text(record)
        self.assertIn(long_message, tip, "★ 截的是显示，气泡里必须是整条")
        self.assertIn("WARN", tip)
        self.assertIn("2026-08-15 13:29:33", tip)

    def test_every_log_is_followed_by_a_divider_line(self) -> None:
        """★ 「每条日志做好分割」：分隔线自己占一行，所以第 k 条在第 2k+1 行。"""
        self._feed(
            "2026-08-15 13:29:13 [警告] 第一条",
            "2026-08-15 13:29:32 [信息] 第二条",
        )

        self.assertIn("sep", self.table._text.tag_names("2.0"))
        self.assertIn("sep", self.table._text.tag_names("4.0"))
        self.assertNotIn("sep", self.table._text.tag_names("1.0"))
        self.assertEqual(self.table._text.tag_cget("sep", "background"), theme.TABLE_ROW_LINE)
        # ★ 用户 2026-08-15 连着三次说「分割线太粗」。线的粗细 = 分隔行那个字体的行高，
        # 所以这里直接量行高，别再去看字号写了几（写 -1 也可能是 3px）。
        import tkinter.font as tkfont

        from wxclient.ui import logtable

        height = int(self.table._sep_font.metrics("linespace"))
        self.assertLessEqual(height, 2, f"分隔线不该超过 2px，现在 {height}px")
        for family in logtable._SEP_FAMILIES:
            try:
                other = int(tkfont.Font(family=family, size=-1).metrics("linespace"))
            except tk.TclError:
                continue
            self.assertLessEqual(height, other, f"{family} 更矮，就该用它")
        # ★ 量字体必须用 family=/size= 这条路：font=(...) 元组形式在 Windows 上会多报一档
        # （Consolas 报 4，实际 3），拿它比大小会挑回最粗的那个——上一版就是这么错的
        self.assertLessEqual(
            int(tkfont.Font(family="Consolas", size=-1).metrics("linespace")),
            int(tkfont.Font(font=("Consolas", -1)).metrics("linespace")),
        )
        # ★ 行距不归零的话，这条「线」会有十几像素高，看着就是行与行之间空了一大块
        self.assertEqual(str(self.table._text.tag_cget("sep", "spacing1")), "0")
        self.assertEqual(str(self.table._text.tag_cget("sep", "spacing3")), "0")

        self.assertEqual(self.table._record_at(1).message, "第一条")
        self.assertIsNone(self.table._record_at(2), "分隔线那行不对应任何一条日志")
        self.assertEqual(self.table._record_at(3).message, "第二条")

    def test_the_severity_bar_is_a_thin_stripe_not_a_block(self) -> None:
        """★ 用户 2026-08-15：「警告最左边的橙色粗了」——一个普通空格宽是它的两三倍。"""
        import tkinter.font as tkfont

        from wxclient.ui import logtable

        bar = tkfont.Font(font=self.table._bar_font).measure(" ")
        normal = tkfont.Font(font=theme.FONT_LOG).measure(" ")

        self.assertLessEqual(bar, logtable._BAR_W)
        self.assertGreater(bar, 0, "细归细，不能细到没有")
        self.assertLess(bar, normal, "比正文的空格窄——原来就是拿它当竖条才显得粗")

    def test_dropping_the_oldest_rows_keeps_lines_and_records_in_step(self) -> None:
        """★ 一条日志占两个 Text 行，删的时候没成对删，气泡就会指到隔壁那条上。"""
        from wxclient.ui.logtable import LogTable

        table = LogTable(self.root, max_lines=2)
        _KEEP_ALIVE.append(table)
        table.append([self.parse(f"2026-08-15 13:29:1{i} [信息] 第{i}条") for i in range(4)])

        self.assertEqual(len(table._shown), 2)
        self.assertEqual(table._record_at(1).message, "第2条")
        self.assertEqual(table._record_at(3).message, "第3条")

    def test_a_line_that_does_not_parse_still_shows_up_whole(self) -> None:
        """★ 拆不开的行（异常栈、第三方 print）不许被列渲染吃掉。"""
        self._feed("Traceback (most recent call last):")

        self.assertIn("Traceback (most recent call last):", self._dump())


@unittest.skipUnless(HAS_TK, "没有 Tk，界面样式测不了")
class FilterChipTests(unittest.TestCase):
    """★ 「上面的全部/警告的选中也不对」——选中态是白底绿框绿字，不是灰底黑字。"""

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = MainWindow(
            self.root, LogBuffer(), GatewayService(lambda: None),
            data_dir=Path(tempfile.mkdtemp()), probe_wechat=False,
        )
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.window)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def test_the_selected_filter_is_the_only_one_wearing_a_green_border(self) -> None:
        for name, chip in self.window._filters.items():
            selected = name == self.window._active_filter
            self.assertEqual(chip.cget("bg"), theme.CONTENT_BG, "没有哪档是灰底的")
            self.assertEqual(chip.cget("fg"), theme.GREEN if selected else theme.TEXT_MUTED)
            self.assertEqual(str(chip.cget("highlightbackground")),
                             theme.GREEN if selected else theme.CONTENT_BG)

    def test_switching_the_filter_moves_the_border_and_leaves_one_lit(self) -> None:
        from wxclient.ui.logline import LEVEL_FILTERS

        self.window._set_level_filter(*LEVEL_FILTERS[1])

        lit = [name for name, chip in self.window._filters.items()
               if str(chip.cget("highlightbackground")) == theme.GREEN]
        self.assertEqual(lit, ["警告"], "同一时刻只有一档亮着")

    def test_the_log_page_offers_clear_and_open_dir(self) -> None:
        """★ 开的是**目录**不是某个文件（用户 2026-08-15）：日志按天分文件，
        要看的往往是前几天那份，直接开今天这份等于替人选错。
        """
        labels = [child.cget("text") for child in self.window._header_actions.winfo_children()]

        self.assertEqual(sorted(labels), sorted(["清空", "打开目录"]))
        self.assertEqual(self.window.log_dir, self.window.data_dir / "logs")


try:
    import pystray  # noqa: F401

    HAS_TRAY = True
except Exception:  # noqa: BLE001 - Linux 上没有托盘后端，整类跳过
    HAS_TRAY = False


@unittest.skipUnless(HAS_TK and HAS_TRAY, "没有托盘后端（pystray），托盘菜单测不了")
class TrayMenuTests(unittest.TestCase):
    """★ 托盘菜单要跟窗口一致（用户 2026-08-15：「右下角的菜单也要同步修改」）。

    托盘是**绕过界面的第二条操作路**——窗口上撤掉的东西如果还留在这里，它就成了后门：
    「启动服务」曾经可以在没登录的时候把网关起起来。
    """

    def _tray(self, logged_in: bool = True):
        from wxclient.ui.service import GatewayService
        from wxclient.ui.tray import Tray

        return Tray(
            GatewayService(lambda: None),
            on_show=lambda: None, on_settings=lambda: None,
            on_quit=lambda: None, on_logout=lambda: None, can_logout=lambda: logged_in,
        )

    def _labels(self, tray) -> list[str]:
        import pystray

        return [str(item.text) for item in tray._icon.menu
                if item is not pystray.Menu.SEPARATOR]

    def test_the_menu_matches_the_window(self) -> None:
        labels = self._labels(self._tray())

        # ★ 窗口里有几页，托盘就得有几项——多一项会指向一个已经不存在的页
        # ★ 菜单名和窗口导航逐字一致（用户 2026-08-15：「显示日志」改叫「实时日志」）
        self.assertEqual(labels, ["实时日志", "配置中心", "退出登录", "退出"])
        self.assertNotIn("计划中心", labels, "★ 那一页删了（计划归 AI 端），托盘得跟着删")
        self.assertNotIn("启动服务", labels, "★ 这一项曾经是登录门的旁路")
        self.assertNotIn("停止服务", labels, "并进「退出登录」了")
        self.assertNotIn("参数设置", labels, "跟着窗口改名叫配置中心")

    def test_logging_out_is_greyed_out_until_you_are_logged_in(self) -> None:
        def enabled(tray, text: str) -> bool:
            item = next(item for item in tray._icon.menu if str(item.text) == text)
            return bool(item.enabled)

        self.assertFalse(enabled(self._tray(logged_in=False), "退出登录"))
        self.assertTrue(enabled(self._tray(logged_in=True), "退出登录"))


if __name__ == "__main__":
    unittest.main()
