"""桌面入口：``python -m wxclient.ui``。

打开即启动服务、直接缩到右下角托盘；双击托盘看日志。命令行参数覆盖 ``WXCLIENT_UI_*``
环境变量。真实动作照旧默认关闭，要真发得显式 ``--allow-real-actions``。
"""

from __future__ import annotations

import argparse
import sys
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


def _mode_text(args: argparse.Namespace, settings: UiSettings) -> str:
    real = args.allow_real_actions or settings.allow_real_actions
    return "模式：真实发送" if real else "模式：dry-run（不真发）"


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
        # 保存后立刻按新参数重启：让「改完到底生效没有」不留悬念
        service.stop()
        settings_store.apply_to_environ(
            settings_store.load(wechat_settings.data_dir), force=True
        )
        window.set_mode_text(_mode_text(args, UiSettings()))
        service.start()

    window = MainWindow(
        root,
        buffer,
        service,
        data_dir=wechat_settings.data_dir,
        mode_text=_mode_text(args, settings),
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

    _log.info("桌面端已启动", mode=_mode_text(args, settings), autostart=autostart)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown(root, window, service, tray)
    return 0


def shutdown(root: tk.Tk, window: MainWindow, service: GatewayService, tray: Tray) -> None:
    _log.info("正在退出桌面端")
    try:
        service.stop()
    finally:
        tray.stop()
        window.destroy()
        root.quit()


if __name__ == "__main__":
    sys.exit(main())
