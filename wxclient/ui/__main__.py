"""桌面入口：``python -m wxclient.ui``。

打开即启动服务、直接缩到右下角托盘；双击托盘看日志。命令行参数覆盖 ``WXCLIENT_UI_*``
环境变量。真实动作照旧默认关闭，要真发得显式 ``--allow-real-actions``。
"""

from __future__ import annotations

import argparse
import sys
import threading
import tkinter as tk

import structlog

from wxclient.config import load_settings
from wxclient.gateway.factory import Gateway, build_gateway
from wxclient.logging_config import configure_logging

from . import settings_store
from .config import UiSettings
from .logbuffer import LogBuffer, LogPipeline
from .service import GatewayService
from .tray import Tray
from .window import MainWindow

_log = structlog.get_logger(__name__)


def _dry_run_notice(args: argparse.Namespace, settings: UiSettings) -> str:
    """不会真的发消息时给的提示；正常（真实发送）返回空串，界面上不显示任何档位。"""
    real = args.allow_real_actions or settings.allow_real_actions
    return "" if real else "⚠ 当前只走流程、不会真的发消息"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wxclient-ui", description="劳小智微信网关（桌面版）")
    parser.add_argument("--config", help="微信运行参数配置文件")
    parser.add_argument("--account", action="append", default=[], metavar="WECHAT_ID",
                        help="要接管的微信号（可重复）")
    parser.add_argument("--allow-real-actions", action="store_true", help="真的往微信发消息")
    parser.add_argument("--gateway-db", help="网关状态库路径")
    parser.add_argument("--no-autostart", action="store_true", help="开窗后不自动启动服务")
    parser.add_argument("--show", action="store_true", help="启动时直接显示日志窗口")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    wechat_settings = load_settings(args.config)
    # 先把上次在「参数设置」里填的值灌进环境变量，再读配置——真实环境变量优先
    settings_store.apply_to_environ(settings_store.load(wechat_settings.data_dir))
    settings = UiSettings()

    buffer = LogBuffer(capacity=settings.log_lines)
    log_pipeline = LogPipeline(
        buffer,
        lambda: configure_logging(log_file=wechat_settings.data_dir / "gateway.log"),
    )
    log_pipeline.install()

    root = tk.Tk()
    root.withdraw()  # 先藏起来，是否露面由下面决定

    def build() -> Gateway:
        # 每次启动都重读一遍 UiSettings：界面上改完参数点「保存并重启」才真的按新值跑
        current = UiSettings()
        return build_gateway(
            accounts=args.account or current.accounts,
            allow_real_actions=args.allow_real_actions or current.allow_real_actions,
            config_path=args.config,
            gateway_db=args.gateway_db,
        )

    service = GatewayService(build)

    def on_settings_saved(restart: bool) -> None:
        if not restart:
            return

        # 保存后立刻按新参数重启：让「改完到底生效没有」不留悬念。
        # 但停机要等网关线程收口（最长 15s），**绝不能在 tk 主线程上等**——
        # 那 15 秒里窗口一次都不重绘，用户看到的就是「点了保存，程序死了」。
        def before_start() -> None:
            settings_store.apply_to_environ(
                settings_store.load(wechat_settings.data_dir), force=True
            )

        def done() -> None:
            # 回调在后台线程上，动界面必须先回 tk 线程
            root.after(0, lambda: (
                window.set_busy(""),
                window.set_dry_run_notice(_dry_run_notice(args, UiSettings())),
            ))

        if service.restart_async(before_start=before_start, on_done=done) is not None:
            window.set_busy("正在按新参数重启服务")

    window = MainWindow(
        root,
        buffer,
        service,
        data_dir=wechat_settings.data_dir,
        dry_run_notice=_dry_run_notice(args, settings),
        on_quit=lambda: shutdown(root, window, service, tray),
        on_settings_saved=on_settings_saved,
        log_pipeline=log_pipeline,
    )

    tray = Tray(
        service,
        on_show=lambda: root.after(0, window.show),
        on_settings=lambda: root.after(0, lambda: (window.show(), window.open_settings())),
        on_quit=lambda: root.after(0, lambda: shutdown(root, window, service, tray)),
    )

    def on_state_change(*_args: object) -> None:
        # 状态回调来自网关线程：界面回 tk 线程刷，托盘图标 pystray 自己是线程安全的
        root.after(0, window.refresh_state)
        tray.refresh()

    service.set_state_listener(on_state_change)

    tray.run_detached()

    autostart = settings.autostart and not args.no_autostart
    if autostart:
        service.start()
    if args.show or not settings.start_hidden:
        window.show()

    _log.info("桌面端已启动", real_actions=(args.allow_real_actions or settings.allow_real_actions), autostart=autostart)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown(root, window, service, tray)
    return 0


def shutdown(root: tk.Tk, window: MainWindow, service: GatewayService, tray: Tray) -> None:
    """退出：界面立刻响应，真正的收尾扔到后台线程。

    ``service.stop()`` 要等网关线程收口（最长 15s），放在 tkinter 主线程上跑，这段时间
    窗口连重绘都不做——点下去像死了一样。所以主线程只负责「立刻看得见的部分」（撤托盘、
    状态改成停止中），停机在后台跑完再回主线程销毁窗口。
    """
    if not window.begin_shutdown():
        return  # 已经在退了，重复点不叠加
    _log.info("正在退出桌面端")
    tray.stop()

    def finish() -> None:
        try:
            service.stop()
        finally:
            root.after(0, lambda: (window.destroy(), root.quit()))

    threading.Thread(target=finish, name="wx-ui-shutdown", daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
