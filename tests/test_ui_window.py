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
    from wxclient.ui.service import GatewayService, GatewayStatus
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

        # 只该有三个：日志页 + 设置页 + 遮罩
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
            order.index(self.window._overlay), order.index(self.window._settings_page),
            "遮罩必须层叠在参数页之上，否则点击照样落到「保存并重启服务」上",
        )
        self.assertTrue(self.window._overlay.bind("<Button-1>"), "遮罩要自己吞掉鼠标事件")

        self.window._locked = False
        self.window._render_overlay()
        self.assertEqual(self.window._overlay.winfo_manager(), "", "解锁后遮罩要收起")

    def test_the_settings_page_is_prebuilt_so_the_first_click_is_cheap(self) -> None:
        """★ 建控件是「很多次小 Tcl 调用」，网关忙着操作微信时主线程抢不到 GIL，最吃亏。

        所以启动后趁空闲先建好，用户点下去只剩 pack/unpack。
        """
        self.assertIsNone(self.window._settings_page, "刚起来时还没建")

        self.window._prebuild_settings_page()

        page = self.window._settings_page
        self.assertIsNotNone(page, "预热之后就该有了")
        self.assertEqual(page.winfo_manager(), "", "预热只建不显示")
        self.window.open_settings()
        self.assertIs(self.window._settings_page, page, "真点的时候直接用预热那个")

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


if __name__ == "__main__":
    unittest.main()
