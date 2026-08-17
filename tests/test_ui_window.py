"""窗口层的对抗性测试：专挑恶意/极端序列打，看哪里会崩、会卡、会说谎。

**为什么单独一个文件**：这些用例要真建 Tk 窗口，Linux 打包机/CI 上没有 tkinter，
`tests/test_ui.py` 里那些「不含 GUI 的部分」才是到处都能跑的。这一份在没有 Tk 的地方
整体跳过，在 Windows 真机上 `python -m unittest discover -s tests` 会照常带上。

⚠️ 像素级的点击命中测不了：SSH 会话的窗口站从不真映射窗口（`winfo_ismapped()` 恒为 0，
所有几何都是 1x1）。所以「遮罩挡住了点击」这条断言的是 Tk 里可判定的两件事——遮罩铺满
整页 + 层叠在参数页之上（`winfo children` 对非 toplevel 是按层叠顺序返回的）。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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
    from wxclient.ui import window as window_module
    from wxclient.ui.service import (
        GatewayService,
        GatewayStatus,
        ServiceNotRunning,
        ServiceState,
    )
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


class _DiskProtector(Protector):
    """会**落盘**的假加密（倒序）。只给「关掉客户端再打开」那类用例用——

    默认的 `_NoDiskProtector` 一个字节都不写，重开之后当然什么都读不到，
    拿它验「重开还记不记得密码」验的是测试自己（2026-08-16 就这么红过一次）。
    """

    available = True
    name = "test-disk"

    def protect(self, plain: str) -> str:
        return plain[::-1]

    def unprotect(self, blob: str) -> str:
        return blob[::-1]


def make_auth(data_dir: Path, api: StubApi, *, base_url: str = "https://business.test",
              persist: bool = False):
    api.base_url = base_url  # ClientAuth.configured 看的就是它
    protector = _DiskProtector() if persist else _NoDiskProtector()
    return auth_module.ClientAuth(base_url, data_dir, client_version="test",
                                  api=api, store=TokenStore(data_dir, protector))


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

    def test_the_nav_is_log_debug_and_config(self) -> None:
        """★ 三页：实时日志 / 调试会话 / 配置中心。

        参数设置→配置中心；监听名单本来就是 AI 端说了算、本机改不了，归进配置中心当
        只读的一节。计划归 AI 端排、经 WS 推给网关执行，桌面端不摆这一页
        （用户 2026-08-15：「计划中心不需要，是发给我们 agent 端的」）。
        调试会话是 2026-08-17 加的：直接跟 AI 端说话，验这台电脑到底动不动得了微信。
        """
        self.assertEqual([item.cget("text") for item in self.window._nav.values()],
                         ["实时日志", "调试会话", "配置中心"])
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

    def _click_the_x(self) -> None:
        """**照窗口管理器的路走一遍**：取出注册在 WM_DELETE_WINDOW 上的那条命令再执行它。

        不直接调 `window.close_requested()`——那样测不到「× 到底接的是哪个方法」。
        接错了（比如还接在 `hide` 上）本条会当场红，而直接调的写法一路绿到上线。
        """
        command = self.root.tk.call("wm", "protocol", self.root._w, "WM_DELETE_WINDOW")
        self.assertTrue(command, "× 上没接任何东西")
        self.root.tk.eval(command)

    def _reveal(self) -> None:
        """把窗口露出来。**没有这一步，「缩起来了没有」这类断言全是假的**——
        setUp 里就 withdraw 过一次，断言 withdrawn 不管代码怎么写都成立。
        """
        self.root.deiconify()
        self.root.update()
        self.assertEqual(self.root.state(), "normal", "前提没成立：窗口没露出来")

    def test_closing_the_window_on_the_login_page_really_quits(self) -> None:
        """★ 还没登录时点 × = 退出程序，不是缩托盘（用户 2026-08-16）。

        缩托盘的后果是「人以为关掉了，进程还在」——而登录页上根本没有任何东西在跑，
        留一个图标只是让人下次开机发现它又在了。
        """
        quits: list[int] = []
        self.window.set_quit_action(lambda: quits.append(1))
        self.window.require_login(self.auth)
        self._reveal()

        self._click_the_x()
        self.root.update()

        self.assertEqual(quits, [1], "登录页上点 × 要走真退出")
        self.assertEqual(self.root.state(), "normal",
                         "窗口别自己先藏起来——藏了就等于又缩了一次托盘")

    def test_closing_the_window_after_login_still_hides_it(self) -> None:
        """★ 反面：登进来之后点 × 还是缩托盘。网关正替人干活，关窗口只是不想看着它。

        没有这一条，上面那条测试可以靠「× 一律退出」通过——而那会把正在跑的网关关掉。
        """
        quits: list[int] = []
        self.window.set_quit_action(lambda: quits.append(1))
        self.window.finish_login(self.auth.login("hr@example.com", "pw"))
        self._reveal()

        self._click_the_x()
        self.root.update()

        self.assertEqual(quits, [], "登进来了就不该退出程序")
        self.assertEqual(self.root.state(), "withdrawn", "缩回托盘")

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
        self.assertEqual(self.window._who.cget("text"), "", "身份要立刻抹掉")
        # ★ 服务还在跑着，这时候**先别切页**（用户 2026-08-16：「加一个 Loading，
        # 等服务退出后再退出登录界面」）。原来点完立刻就切，人已经在登录页上了，
        # 后台还在停机、还在撤会话——这时候重新登进去，撞上的是一个正在关的服务。
        self.assertTrue(self.window.logged_in, "服务没停下来之前不该把登录页放出来")
        self.assertIn("正在退出登录", self.window._busy_label.cget("text"),
                      "转场那几秒最像卡死，得转圈说一句")

        self.service._snapshot = (ServiceState.STOPPED, None)
        self.assertTrue(self._pump(lambda: not self.window.logged_in), "停完了才回登录页")
        self.assertTrue(revoked,
                        "★ 还要告诉服务端撤销这台设备的会话，否则企业端一直显示在线")

    def test_one_click_logout_does_not_log_you_straight_back_in(self) -> None:
        """★ 真机 2026-08-16：**第一次**点退出登录又弹回主界面，第二次才退得出去。

        真因是赌线程时序：撤销会话（当时会顺带清掉记住的密码）丢在后台线程，而登录页在
        同一拍就建起来了。第一次点的时候密码还没被清掉，登录页一出来就「自动登录」又登
        回去了；第二次点时存储已经空了，才显得正常。

        ★ 2026-08-16 起密码**故意留着**（要回填进密码框），「存储恰好空了」这层运气没了。
        挡住自动登录的是两道显式开关：这一页的 `allow_autologin=False`（管这一次的意图）
        和落盘的 `auto_login=False`（管跨重启的意图）。这条用例因此比原来更值钱。
        """
        self.service.stop_async = lambda **_kwargs: None  # type: ignore[method-assign]
        self.window.set_login_context(self.auth, on_logout=self.auth.logout)
        self.window.require_login(self.auth)
        login = self.window._login
        login._account.set("hr@example.com")
        login._password.set("pw")
        login._remember.set(True)
        login.submit()
        self.assertTrue(self._pump(lambda: self.window.logged_in), "先得登进去")
        self.assertEqual(self.auth.remembered_login(), ("hr@example.com", "pw"),
                         "前提：勾了「记住登录状态」，密码确实存下来了")

        self.window.logout()

        self.assertTrue(self._pump(lambda: not self.window.logged_in), "要回到登录页")
        self.assertFalse(self.window._login._allow_autologin,
                         "★ 主动退出来的这一页，自动登录必须是关的")
        # 再等几拍：自动登录是 after(60) 排的，真有的话这段时间足够把人送回主界面。
        self.assertFalse(self._pump(lambda: self.window.logged_in, timeout=1.0),
                         "★ 一次就该退干净，不许被自动登录又送回主界面")

    def test_the_switch_alone_stops_autologin(self) -> None:
        """★ 单独验那道开关：**凭据还在、密码也照样回填**，但不许自动登录。

        不这么验的话，上面那条端到端用例其实是被「存储已经清空了」兜住的——开关删掉它
        照样绿，而那正是这个 bug 原来的样子（赌线程时序）。这里让凭据留着，只关开关。
        """
        from wxclient.ui.login_page import LoginPage

        self.window.require_login(self.auth)
        login = self.window._login
        login._account.set("hr@example.com")
        login._password.set("pw")
        login._remember.set(True)
        login.submit()
        self.assertTrue(self._pump(lambda: self.window.logged_in))
        self.assertEqual(self.auth.remembered_login(), ("hr@example.com", "pw"),
                         "前提：凭据确实还在")

        page = LoginPage(self.root, self.auth, data_dir=self.dir,
                         on_authenticated=lambda _identity: None,
                         allow_autologin=False)
        self.addCleanup(page.destroy)

        self.assertEqual(page._password.get(), "pw",
                         "★ 密码该回填——记住登录状态就是为了别再打一遍（用户 2026-08-16）")
        self.assertEqual(self.api.calls.count("login"), 1,
                         "★ 但**不许自己提交**：填好了等人点「登录」")

    def test_reopening_after_a_manual_logout_fills_the_password_but_waits(self) -> None:
        """★ 退出登录之后**重开客户端**这一路（用户 2026-08-16）。

        这一路 `allow_autologin` 是 True（没人刚点退出，是全新一次启动），挡住自动登录的
        只剩落盘的那个标记。所以这条用例验的正是「跨重启」那一半：
        密码填好了摆在框里，但**没有**自己登进去。
        """
        from wxclient.ui.login_page import LoginPage

        # 这条必须用**会落盘**的存储：验的就是「关掉再打开还记不记得」。
        first = make_auth(self.dir, self.api, persist=True)
        first.login("hr@example.com", "pw")
        first.logout()  # 人自己点的退出

        # 重开：新的 ClientAuth 从磁盘读，没有 Token 可 restore，于是建登录页
        reopened = make_auth(self.dir, self.api, persist=True)
        self.assertIsNone(reopened.restore(), "前提：Token 已经作废了")
        page = LoginPage(self.root, reopened, data_dir=self.dir,
                         on_authenticated=lambda _identity: None)
        self.addCleanup(page.destroy)

        self.assertEqual(page._account.get(), "hr@example.com")
        self.assertEqual(page._password.get(), "pw", "★ 密码要回填")
        self.assertTrue(page._allow_autologin, "前提：这一路的开关是开的，靠的是落盘那个标记")
        self.assertFalse(self._pump(lambda: self.api.calls.count("login") > 1, timeout=1.0),
                         "★ 但别自己登回去——他刚亲手退出来的")
        self.assertEqual(self.api.calls.count("login"), 1, "只有一开始那次人工登录")

    def test_reopening_the_app_still_autologins(self) -> None:
        """★ 关掉自动登录只针对「主动退出」这一条路，别把功能整个关死了。

        正常开机那一路（`require_login` 不传参）该照样拿记住的账号密码登进去。
        """
        self.window.require_login(self.auth)

        self.assertTrue(self.window._login._allow_autologin)

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
        self.assertTrue(self.window._busy_label.cget("text"),
                        "停机过程中该有提示——转场那几秒最像卡死")

        # ★ 2026-08-16 起这句提示是 logout 显式设的，所以**必须有人负责清掉**：
        # `_finish_logout` 在切回登录页的同一拍 set_busy("")。
        self.service._snapshot = (ServiceState.STOPPED, None)
        self.assertTrue(self._pump(lambda: not self.window.logged_in), "停完了回登录页")
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
            on_show=lambda: None, on_debug=lambda: None, on_settings=lambda: None,
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
        self.assertEqual(labels, ["实时日志", "调试会话", "配置中心", "退出登录", "退出"])
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


@unittest.skipUnless(HAS_TK, "没有 Tk")
class WindowSizeTests(unittest.TestCase):
    """★ 默认窗口大一点（用户 2026-08-16），但**不能大过屏幕**。

    大过屏幕不是「不好看」：标题栏被顶到屏幕外之后，窗口既拖不动也缩不了。
    """

    def test_the_default_is_bigger_than_it_used_to_be(self) -> None:
        from wxclient.ui import window as window_mod

        geometry = window_mod.initial_geometry(1920, 1080)
        size = geometry.split("+")[0]
        width, height = (int(n) for n in size.split("x"))

        self.assertEqual((width, height), window_mod.DEFAULT_SIZE, "屏幕够大就按默认给")
        self.assertGreater(width, 1180, "★ 比改之前那个 1180 宽")

    def test_it_never_grows_past_a_small_screen(self) -> None:
        from wxclient.ui import window as window_mod

        geometry = window_mod.initial_geometry(1366, 768)
        size, x, y = geometry.split("+")
        width, height = (int(n) for n in size.split("x"))

        self.assertLessEqual(width, 1366 - window_mod.SCREEN_MARGIN, "得给屏幕留边")
        self.assertLessEqual(height, 768 - window_mod.SCREEN_MARGIN)
        self.assertGreaterEqual((width, height), window_mod.MIN_SIZE,
                                "但也不许缩到比最小尺寸还小")
        self.assertEqual((int(x), int(y)), ((1366 - width) // 2, (768 - height) // 2),
                         "居中：原来不给位置，Windows 会往左上角级联着摆")

    def test_a_tiny_screen_keeps_the_window_usable(self) -> None:
        """★ 屏幕小到放不下最小尺寸时，宁可超出屏幕也不缩——缩到那份上界面自己就挤坏了。"""
        from wxclient.ui import window as window_mod

        geometry = window_mod.initial_geometry(800, 600)
        width, height = (int(n) for n in geometry.split("+")[0].split("x"))

        self.assertEqual((width, height), window_mod.MIN_SIZE)


@unittest.skipUnless(HAS_TK, "没有 Tk")
class WatchSectionTests(unittest.TestCase):
    """★「同步名单」按钮的样式要和别处一致（绿框绿字，用户 2026-08-16）。

    ⚠️ 它**功能还没接上**：点了会明说「同步接口还没接上」。样式一致 ≠ 假装能用——
    真正防它变成假按钮的是那句话，所以这里两条一起验。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.withdraw()
        self.window = MainWindow(self.root, LogBuffer(), GatewayService(lambda: None),
                                 data_dir=self.dir, probe_wechat=False)
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.window)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _sync_button(self):
        page = self.window._build_watch_section(self.root)
        self.addCleanup(page.destroy)
        found = [w for w in page.winfo_children()[0].winfo_children()
                 if isinstance(w, tk.Label) and w.cget("text") == "同步名单"]
        self.assertEqual(len(found), 1, "找不到「同步名单」按钮")
        return found[0]

    def test_it_looks_like_the_other_outline_buttons(self) -> None:
        button = self._sync_button()

        self.assertEqual(button.cget("fg"), theme.GREEN, "★ 绿字，和别处一样")
        self.assertEqual(str(button.cget("highlightbackground")), theme.GREEN, "★ 绿框")
        self.assertEqual(str(button.cget("cursor")), "hand2")

    def test_it_still_tells_the_truth_when_clicked(self) -> None:
        """★ 长得能用，点下去必须说实话——不然就是个假按钮。"""
        button = self._sync_button()  # 顺带把那一节建出来（提示标签在里面）
        # 点击本身在这个窗口站上发不出去（窗口从不真映射），所以分两步验：
        # ① 按钮确实绑了点击；② 绑的那件事说的是实话。
        self.assertTrue(button.bind("<Button-1>"), "按钮没绑点击")

        self.window._sync_watchlist()

        self.assertIn("还没接上", self.window._watch_hint.cget("text"))


@unittest.skipUnless(HAS_TK, "没有 Tk")
class WatchRemovalTests(unittest.TestCase):
    """把会话移出监听名单（用户 2026-08-17：「监听名单可以进行删除，有确认框」）。

    ★ 三条不许破：
      ① **先问一句**——名单是白名单，删掉 = 从此不再自动回他；
      ② **删在 AI 端**（069 权威在那儿）。本机把行抹掉是假的：名单推一次覆盖一次，
         下一次推送又把他带回来，用户看到的是「删了又回来」；
      ③ AI 端说「本来就不在名单里」时，**不许当成删成功**。
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.withdraw()
        self.service = GatewayService(lambda: None)
        self.window = MainWindow(self.root, LogBuffer(), self.service,
                                 data_dir=self.dir, probe_wechat=False)
        self.window._build_watch_section(self.root)
        self.calls: list[str] = []
        self.future: Any = _DoneFuture({"removed": ["候选人-王建国"], "watching": 0})
        self.service.remove_watched = self._remove  # type: ignore[method-assign]
        self.addCleanup(self._teardown)

    def _remove(self, name: str):
        self.calls.append(name)
        return self.future

    def _dialogs(self, answer: bool):
        """★ **只挡住「弹出来」这一下，弹框本身照真建**。

        2026-08-17 真机上「点了没反应」正是被这层遮住的：那时候用例把 `dialog.ask`
        整个换成 Mock，于是没人发现真调用会在 `parent.winfo_toplevel()` 上抛
        AttributeError（`MainWindow` 不是控件）——而 tk 回调里的异常在窗口化程序里
        看不见，界面就是一动不动。所以这里 patch 的是 `_Dialog.show`，`__init__`
        照跑，parent 传错立刻炸给用例看。
        """
        made: list[tuple[Any, str, str]] = []
        real_init = window_module.dialog._Dialog.__init__

        def spy_init(inner, parent, title, message, **kw):  # noqa: ANN001, ANN202
            real_init(inner, parent, title, message, **kw)
            made.append((parent, title, message))
            self.addCleanup(_destroy, inner)

        patches = (
            mock.patch.object(window_module.dialog._Dialog, "__init__", spy_init),
            mock.patch.object(window_module.dialog._Dialog, "show",
                              lambda _inner: answer),
        )
        return made, patches

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.window)
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def test_the_confirm_box_really_opens(self) -> None:
        """★ 弹框得真弹得出来——2026-08-17 真机上它根本没弹（parent 传了个非控件）。"""
        made, patches = self._dialogs(False)
        with patches[0], patches[1]:
            self.window._ask_remove_watched("候选人-王建国")

        self.assertEqual(len(made), 1, "确认框没弹出来")
        parent, title, message = made[0]
        self.assertIsInstance(parent, tk.Misc, "★ 弹框的 parent 必须是真控件")
        self.assertIn("移出", title)
        self.assertIn("候选人-王建国", message)
        self.assertIn("不会被自动回复", message, "得说清后果")

    def test_cancelling_the_confirm_changes_nothing(self) -> None:
        made, patches = self._dialogs(False)
        with patches[0], patches[1]:
            self.window._ask_remove_watched("候选人-王建国")

        self.assertEqual(len(made), 1)
        self.assertEqual(self.calls, [], "点了取消还去删，那这个框就是个摆设")
        self.assertEqual(self.window._watch_removing, {})

    def test_confirming_asks_the_ai_side_and_waits_for_it(self) -> None:
        self.future = _PendingFuture()
        made, patches = self._dialogs(True)
        with patches[0], patches[1]:
            self.window._ask_remove_watched("候选人-王建国")

        self.assertEqual(self.calls, ["候选人-王建国"])
        self.assertIn("候选人-王建国", self.window._watch_removing,
                      "★ 还没回话就不算删掉了——这一行得停在「正在移出…」")

    def test_a_no_op_removal_is_not_reported_as_success(self) -> None:
        self.future = _DoneFuture({"removed": [], "watching": 3})
        made, patches = self._dialogs(True)
        with patches[0], patches[1]:
            self.window._ask_remove_watched("查无此人")
            self.window._poll_watch_removals()

        self.assertEqual(len(made), 2, "确认框 + 「没有变化」各一个")
        self.assertIn("本来就不在", made[1][2])

    def test_a_failure_is_told_as_a_failure(self) -> None:
        self.future = _FailedFuture(RuntimeError("AI 端连不上"))
        made, patches = self._dialogs(True)
        with patches[0], patches[1]:
            self.window._ask_remove_watched("候选人-王建国")
            self.window._poll_watch_removals()

        self.assertEqual(self.window._watch_removing, {}, "失败了也要把这一条收掉")
        self.assertIn("AI 端连不上", made[-1][2])

    def test_the_service_being_down_says_so_instead_of_throwing(self) -> None:
        def boom(_name: str):
            raise ServiceNotRunning("服务没在运行，名单改不了")

        self.service.remove_watched = boom  # type: ignore[method-assign]
        made, patches = self._dialogs(True)
        with patches[0], patches[1]:
            self.window._ask_remove_watched("候选人-王建国")

        self.assertIn("名单改不了", made[-1][2])


def _destroy(dialog_obj: Any) -> None:
    """用例里 `show()` 被挡了，弹框那个 Toplevel 得自己收掉。"""
    try:
        dialog_obj.top.destroy()
    except tk.TclError:
        pass


class _DoneFuture:
    def __init__(self, value: Any) -> None:
        self._value = value

    def done(self) -> bool:
        return True

    def result(self) -> Any:
        return self._value


class _PendingFuture:
    def done(self) -> bool:
        return False

    def result(self) -> Any:  # pragma: no cover - 不该被调到
        raise AssertionError("还没完成")


class _FailedFuture:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def done(self) -> bool:
        return True

    def result(self) -> Any:
        raise self._exc


@unittest.skipUnless(HAS_TK, "没有 Tk")
class TkCallbackErrorTests(unittest.TestCase):
    """★ tk 回调里抛的异常必须留痕。

    2026-08-17 真机：「移出监听名单」点了一点反应都没有。那个回调里抛了 AttributeError，
    而窗口化程序没有控制台——tk 默认把回调异常打到 stderr，于是它去了虚空：
    界面不动、日志不写、没有任何线索。这一条守的是「别再无声无息」。
    """

    def test_the_hook_writes_the_error_into_the_log(self) -> None:
        from wxclient.ui.__main__ import _log_tk_callback_errors

        root = tk.Tk()
        root.withdraw()
        _KEEP_ALIVE.append(root)
        self.addCleanup(root.destroy)

        _log_tk_callback_errors(root)
        logger = mock.MagicMock()
        with mock.patch("structlog.get_logger", return_value=logger):
            try:
                raise ValueError("界面回调炸了")
            except ValueError:
                root.report_callback_exception(*sys.exc_info())

        logger.error.assert_called_once()
        self.assertIn("界面回调出错", logger.error.call_args.args[0])
        self.assertIn("界面回调炸了", str(logger.error.call_args.kwargs))


@unittest.skipUnless(HAS_TK, "没有 Tk")
class SplashRevealTests(unittest.TestCase):
    """★ 启动动画得**摆好了再露面**（用户 2026-08-17：位置图标都对了，还是闪一下）。

    `overrideredirect(True)` 是在窗口建出来之后去掉边框的，Windows 那边等于把窗口重建
    一次。窗口这时候要是已经可见，人就会看见一个带边框的灰框闪一下再变成这张卡片。
    """

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.root)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _splash(self):
        from wxclient.ui.splash import Splash

        splash = Splash(self.root)
        self.addCleanup(lambda: splash._win.destroy())
        return splash

    def test_it_stays_hidden_until_everything_is_in_place(self) -> None:
        """★ 单独验那道「先藏着」：把露面这一步掐掉，窗口就该还是藏着的。

        只验「构造完是可见的」是验不到这条的——那条不管藏没藏过都成立。
        """
        from wxclient.ui.splash import Splash

        with mock.patch.object(Splash, "_reveal", lambda _self: None):
            splash = self._splash()

        self.assertEqual(splash._win.state(), "withdrawn")

    def test_it_is_showing_by_the_time_the_constructor_returns(self) -> None:
        """藏完得记着露出来——不然就成了「启动时什么都没有」。"""
        splash = self._splash()

        self.assertEqual(splash._win.state(), "normal")

    def test_the_static_first_frame_is_closed_only_after_ours_is_painted(self) -> None:
        """★ 顺序是硬的：我们这张画出来了，才关 PyInstaller 那张静态首屏。

        反过来的话中间那一瞬间屏幕上一张都没有——那就是「闪一下」。
        """
        from wxclient.ui import splash as splash_mod

        order: list[str] = []
        with mock.patch.object(splash_mod, "_close_frozen_splash",
                               lambda: order.append("close_frozen")), \
                mock.patch.object(splash_mod.Splash, "_reveal",
                                  lambda _self: order.append("reveal")):
            self._splash()

        self.assertEqual(order, ["reveal", "close_frozen"])


class _StubParent:
    """假的父窗口几何。真窗口在 SSH 会话里全是 1×1，拿它验居中等于什么都没验。"""

    def __init__(self, *, rootx: int, rooty: int, width: int, height: int) -> None:
        self._values = {"rootx": rootx, "rooty": rooty, "width": width, "height": height}

    def winfo_ismapped(self) -> int:
        return 1

    def winfo_rootx(self) -> int:
        return self._values["rootx"]

    def winfo_rooty(self) -> int:
        return self._values["rooty"]

    def winfo_width(self) -> int:
        return self._values["width"]

    def winfo_height(self) -> int:
        return self._values["height"]


@unittest.skipUnless(HAS_TK, "没有 Tk")
class DialogTests(unittest.TestCase):
    """★ 自己画的弹框（用户 2026-08-16：「弹框改成自定义的和界面视觉风格一致，
    在这个窗口的中心」）。

    这里守三件：落在**父窗口**中心（不是屏幕中心）、模态、以及「没明确点确定」一律回 False。
    """

    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.geometry("900x600+120+80")
        self.root.update_idletasks()
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        _KEEP_ALIVE.append(self.root)
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _dialog(self, *, cancel: str | None = "取消", tone: str = "info"):
        from wxclient.ui import dialog

        return dialog._Dialog(self.root, "标题", "正文" * 40,
                              confirm="确定", cancel=cancel, tone=tone)

    def test_it_lands_on_the_middle_of_its_own_window(self) -> None:
        """★ 不是屏幕正中：人正在这个窗口里做事，弹框跑到别处等于让视线跳一次。"""
        self.root.deiconify()
        self.root.update()
        if not self.root.winfo_ismapped():
            self.skipTest("这个窗口站不真映射窗口，父窗口几何是假的")
        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)

        dialog.center()
        dialog.top.update_idletasks()

        expected_x = self.root.winfo_rootx() + (
            self.root.winfo_width() - dialog.top.winfo_reqwidth()) // 2
        self.assertAlmostEqual(dialog.top.winfo_x(), max(0, expected_x), delta=2)

    def test_an_unmapped_parent_falls_back_to_the_screen(self) -> None:
        """★ 窗口缩在托盘里时父窗口是 1×1 的假坐标，照它摆弹框会飞到左上角。"""
        self.root.withdraw()
        self.root.update()
        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)

        dialog.center()

        self.assertNotEqual(dialog.top.geometry().split("+")[1:], ["0", "0"])

    def test_the_centering_maths(self) -> None:
        """★ 居中算得对不对，不能只靠上面那条——SSH 会话的窗口站不真映射窗口，
        那条会整条跳过（真机上跑测试时就是这样）。这里把父窗口的几何喂进去算。
        """
        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)
        dialog.parent = _StubParent(rootx=200, rooty=100, width=900, height=600)

        dialog.center()

        width = dialog.top.winfo_reqwidth()
        height = dialog.top.winfo_reqheight()
        _, x, y = dialog.top.geometry().split("+")
        self.assertEqual(int(x), 200 + (900 - width) // 2)
        self.assertEqual(int(y), 100 + (600 - height) // 2)

    def test_closing_without_confirming_means_no(self) -> None:
        """★ 弹框问的都是有后果的事（顶掉别人的登录）。Esc / 关掉 / 取消，一律回 False。"""
        dialog = self._dialog()

        dialog._close(False)

        self.assertFalse(dialog.result)

    def test_escape_is_wired_up(self) -> None:
        """自画标题栏就没有右上角那个 ×，Esc 是唯一的键盘退路——它必须绑上。"""
        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)

        self.assertTrue(dialog.top.bind("<Escape>"), "★ Esc 没绑，这个框就只能用鼠标关")
        self.assertTrue(dialog.top.bind("<Return>"))

    def test_a_telling_dialog_has_no_cancel_button(self) -> None:
        """只是告诉一声的框不该摆一个「取消」——取消什么？"""
        dialog = self._dialog(cancel=None)
        self.addCleanup(dialog.top.destroy)

        self.assertIsNone(dialog.cancel)

    def test_it_stays_above_the_window_it_blocks(self) -> None:
        """★ 真机 2026-08-16：「弹框跑到主窗口后面去了」。

        `grab_set` 管输入不管层叠：自画标题栏的窗口不归窗口管理器管，点一下主窗口就把
        弹框压到后面——而抓取还在，主窗口点什么都没反应，**看起来就是程序卡死了**。
        抓了输入就得让人看得见是谁抓的。
        """
        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)

        self.assertEqual(int(dialog.top.attributes("-topmost")), 1)

    def test_it_shows_even_if_the_grab_fails(self) -> None:
        """★ 抓不到输入也得把框显示出来——宁可少一层模态，也不能弹一个谁都点不着的框。"""
        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)
        dialog.top.grab_set = mock.Mock(side_effect=tk.TclError("grab failed"))
        dialog.top.wait_window = mock.Mock()  # 别真等，不然测试挂在这儿

        dialog.show()  # 不该抛

        self.assertTrue(dialog.top.winfo_exists())

    def test_it_carries_the_app_logo(self) -> None:
        """★ 用户 2026-08-16「弹框可以加上应用 logo」——一眼看出这是劳小智弹的。"""
        from wxclient.ui import dialog as dialog_mod

        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)

        self.assertIsNotNone(dialog.logo, "图标没加载上")
        self.assertEqual(dialog.logo.width(), dialog_mod.LOGO_SIZE)
        labels = [w for w in dialog.top.winfo_children()[0].winfo_children()[0].winfo_children()
                  if isinstance(w, tk.Label)]
        self.assertTrue(any(str(w.cget("image")) for w in labels), "★ 图标得真挂到界面上")

    def test_a_missing_logo_does_not_leave_a_hole(self) -> None:
        """★ 取不到图就只放标题。留一块空占位比没有更糟——看起来像图裂了。"""
        from wxclient.ui import dialog as dialog_mod

        with mock.patch.object(dialog_mod.icons, "logo_photo", lambda *_a, **_k: None):
            dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)

        self.assertIsNone(dialog.logo)
        head = dialog.top.winfo_children()[0].winfo_children()[0]
        self.assertEqual(len(head.winfo_children()), 1, "只剩标题那一个控件")

    def test_the_accent_says_how_bad_it_is(self) -> None:
        from wxclient.ui import theme

        self.assertEqual(self._dialog(tone="error").accent.cget("bg"), theme.RED)
        self.assertEqual(self._dialog().accent.cget("bg"), theme.GREEN)

    def test_it_uses_the_app_palette_not_the_system_one(self) -> None:
        from wxclient.ui import theme

        dialog = self._dialog()
        self.addCleanup(dialog.top.destroy)

        self.assertEqual(dialog.confirm.cget("bg"), theme.GREEN_DEEP,
                         "确认按钮和登录页那颗同一个绿")
        self.assertTrue(dialog.top.overrideredirect(),
                        "★ 系统标题栏是灰的，和这套白底界面接不上")


class _StubIcon:
    """假的 pystray.Icon。**不许在测试里 run 真的**——那会在打包机右下角真弹出一个图标，

    而且 `run()` 是个消息循环，停不掉就把测试挂死。
    """

    def __init__(self) -> None:
        self.runs = 0
        self.stops = 0
        self.icon: Any = None
        self.title = ""
        self.menu: list = []
        self._alive = threading.Event()

    def run(self) -> None:
        self.runs += 1
        self._alive.wait(5)  # 假装是个消息循环

    def stop(self) -> None:
        self.stops += 1
        self._alive.set()

    def update_menu(self) -> None:
        pass


@unittest.skipUnless(HAS_TK and HAS_TRAY, "没有托盘后端（pystray），托盘测不了")
class TrayLifecycleTests(unittest.TestCase):
    """★ 托盘图标只在登录之后露面（用户 2026-08-16：「登录之后才启动托盘，
    Loading 中不好出现这个」）。

    这里测的是 `Tray` 自己的开关行为；「谁来调、什么时候调」在
    `tests/test_ui.py::TrayAfterLoginTests` 里按接线验。
    """

    def _tray(self):
        from wxclient.ui.service import GatewayService
        from wxclient.ui.tray import Tray

        tray = Tray(
            GatewayService(lambda: None),
            on_show=lambda: None, on_debug=lambda: None, on_settings=lambda: None,
            on_quit=lambda: None, on_logout=lambda: None,
        )
        tray._icon = _StubIcon()  # 换掉真 Icon：测试不许在打包机上真弹图标
        self.addCleanup(tray.stop)
        return tray

    def _wait(self, predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_fresh_tray_is_not_showing_yet(self) -> None:
        """★ 建出来 ≠ 露面。启动那一路会先建好它，但登录之前不许出现在右下角。"""
        tray = self._tray()

        self.assertFalse(tray.running)
        self.assertEqual(tray._icon.runs, 0, "★ 没人调 start()，就不该跑起来")

    def test_starting_twice_only_shows_one_icon(self) -> None:
        """恢复登录 + 登录回调可能都调到——重复调必须是空操作，否则右下角两个图标。"""
        tray = self._tray()

        tray.start()
        self.assertTrue(self._wait(lambda: tray._icon.runs == 1))
        tray.start()

        self.assertTrue(tray.running)
        self.assertEqual(tray._icon.runs, 1)

    def test_stopping_without_starting_is_harmless(self) -> None:
        """登录页上点 × 退出程序时会走到 `tray.stop()`，那会儿托盘根本没起过。"""
        tray = self._tray()

        tray.stop()  # 不该抛

        self.assertEqual(tray._icon.stops, 0)

    def test_refresh_does_nothing_while_hidden(self) -> None:
        """★ 服务状态回调来自网关线程。托盘没露面时去动一个没 run 起来的 Icon，
        炸出来的异常和「状态刷新」毫无关系，查起来极贵。"""
        tray = self._tray()

        tray.refresh()

        self.assertIsNone(tray._icon.icon, "没露面就别碰它")

    def test_logging_back_in_brings_the_icon_back(self) -> None:
        """★ 退出登录 → 再登录：图标得回来。

        pystray 的 Icon 停过之后 run 不起来（win32 那边窗口已经销毁），所以 `start()`
        必须**重建**一个。少了这一步，第二次登录之后右下角就永远空着。
        """
        tray = self._tray()
        tray.start()
        self.assertTrue(self._wait(lambda: tray._icon.runs == 1))
        first = tray._icon

        tray.stop()
        self.assertEqual(first.stops, 1)
        with mock.patch.object(tray, "_build_icon", _StubIcon):
            tray.start()
            self.assertTrue(self._wait(lambda: tray._icon.runs == 1))

        self.assertIsNot(tray._icon, first, "★ 必须是新建的那一个")
        self.assertTrue(tray.running)


if __name__ == "__main__":
    unittest.main()
