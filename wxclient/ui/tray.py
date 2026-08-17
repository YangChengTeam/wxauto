"""右下角托盘图标（pystray）。

托盘线程和 tkinter 线程是两条线：菜单点击发生在 pystray 线程，凡是要动界面的动作
一律用 ``root.after`` 丢回 tk 线程执行，别在托盘线程里直接碰控件。

★ **图标只在登录之后露面**（用户 2026-08-16：「登录之后才启动托盘，Loading 中不好出现
这个」）。启动动画还在转、或者人停在登录页时，托盘里那个图标什么都代表不了——服务还没起
（服务只在登录后起，见 `ui/__main__`），菜单里三项有两项点了也没用。所以 `start()` 由
登录成功那一刻调，`stop()` 由退出登录那一刻调，两边都做成可以重复调。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .icons import build_icon_image
from .service import GatewayService


class Tray:
    def __init__(
        self,
        service: GatewayService,
        *,
        on_show: Callable[[], None],
        on_debug: Callable[[], None],
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
        on_logout: Callable[[], None],
        can_logout: Callable[[], bool] | None = None,
    ) -> None:
        import pystray

        self._service = service
        self._on_show = on_show
        self._on_debug = on_debug
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._on_logout = on_logout
        self._can_logout_extra = can_logout
        self._pystray = pystray
        self._thread: threading.Thread | None = None
        self._running = False
        self._icon = self._build_icon()

    def _build_icon(self) -> Any:
        """建一个 pystray.Icon。**建了不等于露面**——要 `run()` 起来才看得见。

        单独拆出来是因为 `start()` 可能要建第二个：pystray 的 Icon 停过之后不能再 run
        （win32 那边窗口已经销毁了），而「退出登录 → 再登录」正好要求托盘能回来。
        """
        # ★ 菜单跟着窗口一起改（用户 2026-08-15）：
        #   ① 「启动服务/停止服务」没了——服务只在登录之后才起（见 `ui/__main__`），
        #      留着「启动服务」等于给登录门开一条旁路；「停止服务」并进了「退出登录」。
        #   ② 菜单名和窗口左侧导航**逐字一致**（「显示日志」→「实时日志」、「参数设置」→
        #      「配置中心」，用户 2026-08-15）：同一个地方在两处叫两个名字，人就得自己去对。
        #   ③ 菜单和左侧导航一一对应：窗口里有几页，这儿就有几项——少一项就等于托盘只能
        #      到一半的地方；多一项则会指向一个已经不存在的页（「计划中心」删掉时就是连着
        #      这儿一起删的，计划归 AI 端）。
        pystray = self._pystray
        return pystray.Icon(
            "lxz-wechat-gateway",
            icon=build_icon_image(self._service.state),
            title=self._title(),
            menu=pystray.Menu(
                pystray.MenuItem("实时日志", self._show, default=True),
                pystray.MenuItem("调试会话", self._debug),
                pystray.MenuItem("配置中心", self._settings),
                pystray.MenuItem("退出登录", self._logout, enabled=self._can_logout),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            ),
        )

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """让图标露面：托盘的消息循环放到**守护**线程里跑。登录成功那一刻调。

        不用 pystray 自带的 ``run_detached()``：它起的是**非守护**线程
        （``pystray/_win32.py:_run_detached``）。退出时只要那条线程没干净收场，
        解释器就会一直等它——窗口没了、托盘图标没了，进程却还在，
        用户看到的是「点了退出没反应，再点快捷方式也起不来」。

        重复调是空操作；停过之后再调会**重建**一个 Icon（停过的那个 run 不起来了）。
        """
        if self._running:
            return
        if self._thread is not None:
            self._icon = self._build_icon()
        self._running = True
        self._thread = threading.Thread(target=self._icon.run, name="wx-tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """把图标收掉。没起过就什么都不做——退出登录和退出程序都会调到这儿。"""
        if not self._running:
            return
        self._running = False
        self._icon.stop()

    def refresh(self) -> None:
        """服务状态变了就刷图标颜色和悬浮提示。可从任意线程调。

        没露面的时候直接返回：登录前服务根本不会跑，这时候去动一个没 run 起来的 Icon
        只会在别的线程上炸出一个跟状态刷新毫无关系的异常。
        """
        if not self._running:
            return
        self._icon.icon = build_icon_image(self._service.state)
        self._icon.title = self._title()
        self._icon.update_menu()

    def _title(self) -> str:
        return f"劳小智微信网关 · {self._service.label}"[:127]  # 悬浮提示有长度上限

    def _can_logout(self, _item: Any = None) -> bool:
        """没登录就没什么可退的——这一项是灰的。"""
        return self._can_logout_extra is None or bool(self._can_logout_extra())

    def _show(self, *_args: Any) -> None:
        self._on_show()

    def _logout(self, *_args: Any) -> None:
        # 退出登录会停服务，而停机要等网关线程收口（最长 15s）——那件事由窗口丢到后台去做
        # （`MainWindow.logout`），托盘这边只负责把动作传过去，别在托盘线程上等。
        self._on_logout()

    def _debug(self, *_args: Any) -> None:
        self._on_debug()

    def _settings(self, *_args: Any) -> None:
        self._on_settings()

    def _quit(self, *_args: Any) -> None:
        self._on_quit()
