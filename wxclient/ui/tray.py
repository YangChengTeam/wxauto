"""右下角托盘图标（pystray）。

托盘线程和 tkinter 线程是两条线：菜单点击发生在 pystray 线程，凡是要动界面的动作
一律用 ``root.after`` 丢回 tk 线程执行，别在托盘线程里直接碰控件。
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
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
        on_logout: Callable[[], None],
        can_logout: Callable[[], bool] | None = None,
    ) -> None:
        import pystray

        self._service = service
        self._on_show = on_show
        self._on_settings = on_settings
        self._on_quit = on_quit
        self._on_logout = on_logout
        self._can_logout_extra = can_logout
        # ★ 菜单跟着窗口一起改（用户 2026-08-15）：
        #   ① 「启动服务/停止服务」没了——服务只在登录之后才起（见 `ui/__main__`），
        #      留着「启动服务」等于给登录门开一条旁路；「停止服务」并进了「退出登录」。
        #   ② 「参数设置」改名「配置中心」，和窗口里的导航一致。
        self._icon = pystray.Icon(
            "lxz-wechat-gateway",
            icon=build_icon_image(service.state),
            title=self._title(),
            menu=pystray.Menu(
                pystray.MenuItem("显示日志", self._show, default=True),
                pystray.MenuItem("配置中心", self._settings),
                pystray.MenuItem("退出登录", self._logout, enabled=self._can_logout),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._quit),
            ),
        )

    def run_detached(self) -> None:
        """把托盘的消息循环放到**守护**线程里跑。

        不用 pystray 自带的 ``run_detached()``：它起的是**非守护**线程
        （``pystray/_win32.py:_run_detached``）。退出时只要那条线程没干净收场，
        解释器就会一直等它——窗口没了、托盘图标没了，进程却还在，
        用户看到的是「点了退出没反应，再点快捷方式也起不来」。
        """
        self._thread = threading.Thread(target=self._icon.run, name="wx-tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._icon.stop()

    def refresh(self) -> None:
        """服务状态变了就刷图标颜色和悬浮提示。可从任意线程调。"""
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

    def _settings(self, *_args: Any) -> None:
        self._on_settings()

    def _quit(self, *_args: Any) -> None:
        self._on_quit()
