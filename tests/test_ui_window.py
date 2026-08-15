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

if HAS_TK:
    from wxclient.logging_config import configure_logging
    from wxclient.ui.logbuffer import LogBuffer, LogPipeline
    from wxclient.ui.service import GatewayService, GatewayStatus, ServiceState
    from wxclient.ui.window import MainWindow


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
            data_dir=Path(tempfile.mkdtemp()), on_quit=lambda: None,
        )
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
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

        # 只该有四个：日志页 + 计划中心 + 配置中心 + 遮罩
        self.assertLessEqual(len(self.window._pages.winfo_children()), 4)

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

    def test_the_nav_is_log_plans_config(self) -> None:
        """★ 导航改名（用户 2026-08-15）：参数设置→配置中心，监听名单→计划中心。

        名单本来就是 AI 端说了算、本机改不了的，归进配置中心当只读的一节；腾出来的位置
        留给后端下发的计划。侧栏那行仍然只报「几个会话」，名字不铺在侧栏上。
        """
        self.assertEqual([self.window._nav[key].cget("text") for key in ("log", "plans", "settings")],
                         ["实时日志", "计划中心", "配置中心"])
        self.assertNotIn("watch", self.window._nav, "监听名单不再是单独一项")
        self.assertFalse(hasattr(self.window, "_watch_label"), "侧栏不该再铺名单")

        self.window._show_page("plans")

        self.assertEqual(self.window._page, "plans")
        self.assertEqual(self.window._heading.cget("text"), "计划中心")
        self.assertEqual(self.window._plans_page.winfo_manager(), "pack")
        self.assertEqual(self.window._log_page.winfo_manager(), "", "日志页要让位")
        # 日志页专属的那一排不该跟着跑出来
        self.assertEqual(self.window._filter_bar.winfo_manager(), "")
        self.assertEqual(self.window._live_dot.winfo_manager(), "")

    def test_the_empty_plan_page_says_why_it_is_empty(self) -> None:
        """空页不许空着不说话：得讲清计划是后端下发的、这台机器只负责显示和执行。"""
        self.window._show_page("plans")
        hint = self.window._plans_hint.cget("text")
        self.assertIn("还没有计划", hint)
        self.assertIn("后端", hint)

    def test_the_watch_list_lists_the_actual_names_inside_the_config_page(self) -> None:
        """名单是「谁会被自动回复」的唯一判据，配置中心里必须看得到具体是谁。"""
        from wxclient.ui.service import GatewayStatus

        self.window.service._status = GatewayStatus(watched=("张三", "李四", "王五"))
        self.window._show_page("settings")

        body = self.window._watch_text.get("1.0", "end-1c")
        for name in ("张三", "李四", "王五"):
            self.assertIn(name, body)
        self.assertEqual(self.window._watch_text.cget("state"), "disabled", "只读")

    def test_an_empty_watch_list_says_nothing_will_be_answered(self) -> None:
        """空名单分两种，别混为一谈：没起服务是正常的，**跑着却空**才是要命的。"""
        self.window._show_page("settings")
        self.assertIn("没在跑", self.window._watch_hint.cget("text"), "没启动时别吓人")

        # 跑起来了、名单却是空的：意味着一条消息都不会回，必须说出来
        self.window.service._snapshot = (ServiceState.RUNNING, None)
        self.window._render_watch_page()

        self.assertIn("一条消息都不会回", self.window._watch_hint.cget("text"))

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
            data_dir=Path(tempfile.mkdtemp()), on_quit=lambda: None,
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
            data_dir=data_dir, on_quit=lambda: None,
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
            data_dir=Path(tempfile.mkdtemp()), on_quit=lambda: None,
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
        from wxclient.ui import auth

        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.withdraw()
        self.service = GatewayService(lambda: None)
        self.window = MainWindow(
            self.root, LogBuffer(), self.service, data_dir=self.dir, on_quit=lambda: None,
        )
        self.provider = auth.LocalAccountProvider(self.dir)
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
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

    def test_a_fresh_machine_asks_you_to_set_an_account_first(self) -> None:
        self.window.require_login(self.provider)

        self.assertFalse(self.window.logged_in, "还没登录")
        login = self.window._login
        self.assertTrue(login._setup_mode, "第一次要设账号，不是登录")
        self.assertEqual(login._button.cget("text"), "设置并进入")
        self.assertIs(self.root.winfo_children()[-1], login, "登录页要压在最上面")
        self.assertEqual(login.place_info().get("relwidth"), "1", "要盖满整个窗口")

    def test_the_wrong_password_keeps_you_out_and_says_why(self) -> None:
        self.provider.create("zhangsan", "hunter2")
        self.window.require_login(self.provider)
        login = self.window._login
        login._account.set("zhangsan")
        login._password.set("这不是密码")

        login.submit()

        self.assertTrue(self._pump(lambda: bool(login._error.cget("text"))), "错误要显示出来")
        self.assertEqual(login._error.cget("text"), "账号或密码不对")
        self.assertFalse(self.window.logged_in, "★ 密码错了绝不能放进来")
        self.assertEqual(login._account.get(), "zhangsan", "账号别给人清掉，改密码再来就行")

    def test_logging_in_takes_the_door_away_and_shows_who_you_are(self) -> None:
        self.provider.create("zhangsan", "hunter2")
        seen = []
        self.window.require_login(self.provider, on_authenticated=seen.append)
        login = self.window._login
        login._account.set("zhangsan")
        login._password.set("hunter2")

        login.submit()

        self.assertTrue(self._pump(lambda: self.window.logged_in), "登录之后门要撤掉")
        self.assertEqual([identity.account for identity in seen], ["zhangsan"])
        self.assertEqual(self.window._page, "log", "登进来落在实时日志")
        self.assertIn("zhangsan", self.window._who.cget("text"), "侧栏要说清是谁在用")

    def test_logging_out_closes_the_door_again_without_stopping_the_service(self) -> None:
        """★ 退出登录只关门。把服务一起停掉等于「谁下班谁就把网关关了」。"""
        self.provider.create("zhangsan", "hunter2")
        self.window.set_login_context(self.provider)
        self.window.finish_login(self.provider.login("zhangsan", "hunter2"))
        self.service._snapshot = (ServiceState.RUNNING, None)

        self.window.logout()

        self.assertFalse(self.window.logged_in, "门要关回去")
        self.assertEqual(self.window._who.cget("text"), "", "身份要抹掉")
        self.assertIs(self.service.state, ServiceState.RUNNING, "服务不许受影响")

    def test_remembering_the_account_never_remembers_the_password(self) -> None:
        from wxclient.ui import auth

        self.provider.create("zhangsan", "hunter2")
        self.window.require_login(self.provider)
        login = self.window._login
        login._account.set("zhangsan")
        login._password.set("hunter2")
        login._remember.set(True)

        login.submit()
        self.assertTrue(self._pump(lambda: self.window.logged_in))

        raw = (self.dir / auth.UI_STATE_FILE).read_text(encoding="utf-8")
        self.assertIn("zhangsan", raw)
        self.assertNotIn("hunter2", raw)


@unittest.skipUnless(HAS_TK, "没有 tkinter / 显示环境，窗口层测不了")
class ConfigCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.root = tk.Tk()
        self.root.withdraw()
        self.service = GatewayService(lambda: None)
        self.window = MainWindow(
            self.root, LogBuffer(), self.service, data_dir=self.dir, on_quit=lambda: None,
        )
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def test_saving_does_not_freeze_every_pushed_value_into_a_local_override(self) -> None:
        """★ 这是最容易埋进去、又最难发现的一个坑。

        配置中心一保存就把满屏的值全写进本机文件的话，十几个键会**全部**变成「本机覆盖」，
        从此后端下发什么都不生效——而界面上一切正常（值确实对，只是冻在了保存那一刻）。
        本机文件里只该躺着「这台机器上的人真改过的那几个」。
        """
        from wxclient.ui import settings_store

        settings_store.save_remote(self.dir, {
            "WXCLIENT_UI_ACCOUNTS": "后端给的",
            "WXCLIENT_GATEWAY_AI_WS_URL": "wss://后端/ws",
        })
        page = self.window._ensure_settings_page() and self.window._settings_page

        kept = page._drop_same_as_remote({
            "WXCLIENT_UI_ACCOUNTS": "后端给的",      # 没动过 → 不该留在本机
            "WXCLIENT_GATEWAY_AI_WS_URL": "wss://本机/ws",  # 真改了 → 留
            "WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR": "20",    # 后端压根没下发 → 留
        })

        self.assertEqual(kept, {
            "WXCLIENT_GATEWAY_AI_WS_URL": "wss://本机/ws",
            "WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR": "20",
        })

    def test_an_overridden_row_shows_what_the_backend_had(self) -> None:
        from wxclient.ui import settings_store

        settings_store.save_remote(self.dir, {"WXCLIENT_UI_ACCOUNTS": "后端给的"})
        settings_store.save(self.dir, {"WXCLIENT_UI_ACCOUNTS": "本机改的"})

        self.window._ensure_settings_page()
        mark = self.window._settings_page._marks["WXCLIENT_UI_ACCOUNTS"]

        self.assertIn("本机已改", mark.cget("text"))
        self.assertIn("后端给的", mark.cget("text"))

    def test_a_secret_override_never_prints_the_backend_value(self) -> None:
        """★ internal key 是网关唯一的凭证，界面上任何地方都不该把它铺出来。"""
        from wxclient.ui import settings_store

        settings_store.save_remote(self.dir, {"WXCLIENT_GATEWAY_INTERNAL_KEY": "后端的密钥"})
        settings_store.save(self.dir, {"WXCLIENT_GATEWAY_INTERNAL_KEY": "本机的密钥"})

        self.window._ensure_settings_page()
        mark = self.window._settings_page._marks["WXCLIENT_GATEWAY_INTERNAL_KEY"]

        self.assertIn("本机已改", mark.cget("text"))
        self.assertNotIn("后端的密钥", mark.cget("text"))

    def test_the_banner_only_shows_up_when_something_is_actually_wrong(self) -> None:
        self.service._snapshot = (ServiceState.RUNNING, None)
        self.service._status = GatewayStatus(connected=1, accounts=1)
        self.window.refresh_state()
        self.assertEqual(self.window._banner.winfo_manager(), "", "一切正常时不占地方")

        self.service._status = GatewayStatus(connected=0, accounts=1, connect_error="认证失败")
        self.window.refresh_state()

        self.assertEqual(self.window._banner.winfo_manager(), "pack")
        self.assertIn("AI 端连接异常", self.window._banner_title.cget("text"))
        self.assertIn("认证失败", self.window._banner_detail.cget("text"))


if __name__ == "__main__":
    unittest.main()
