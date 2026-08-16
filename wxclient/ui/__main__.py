"""桌面入口：``python -m wxclient.ui``。

打开即启动服务、直接缩到右下角托盘；双击托盘看日志。命令行参数覆盖 ``WXCLIENT_UI_*``
环境变量。真实动作照旧默认关闭，要真发得显式 ``--allow-real-actions``。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from typing import TYPE_CHECKING, Any

from wxclient import __version__

# ★ 模块级只留「把启动动画画出来」必需的这几样。**别在这儿 import 业务模块**——
# `python -m wxclient.ui` 会先把它们全跑一遍，而在它们回来之前屏幕上一片空白。
# 真机实测（源码模式，冷启动）：到看见启动动画 5.8 秒，其中 4.7 秒花在 import 上
# （pydantic_settings + structlog + rich + asyncio）。这些东西**动画出来之后再要也不迟**。
from . import icons
from .splash import Splash

if TYPE_CHECKING:  # 只为类型标注——`from __future__ import annotations` 让它们不在运行时求值
    from wxclient.gateway.factory import Gateway

    from .config import UiSettings
    from .service import GatewayService
    from .tray import Tray
    from .window import MainWindow


class _LazyLogger:
    """structlog 也得等动画出来再拉。它连着 rich，冷启动上不便宜。"""

    def __getattr__(self, name: str) -> Any:
        import structlog

        return getattr(structlog.get_logger("wxclient.ui.__main__"), name)


_log = _LazyLogger()


def _real_actions(args: argparse.Namespace, settings: UiSettings) -> bool:
    """这台机器会不会**真的**动微信。命令行的 `--allow-real-actions` 压过配置文件。

    ★ 这句判断此前在四个地方各写了一遍（提示语、装配网关、算能力表、启动日志）。
    它决定的是同一件事，写成四份的后果是改判据时漏掉一处——而漏掉的那处会去
    告诉用户「已经发出去了」，实际一条都没发。
    """
    return bool(args.allow_real_actions or settings.allow_real_actions)


def _dry_run_notice(args: argparse.Namespace, settings: UiSettings) -> str:
    """不会真的发消息时给的提示；正常（真实发送）返回空串，界面上不显示任何档位。"""
    return "" if _real_actions(args, settings) else "⚠ 当前只走流程、不会真的发消息"


#: Windows 任务栏用它给窗口分组、挑图标。不设的话我们只是「某个 python.exe」，
#: 任务栏上顶着 Python 的图标，跟托盘、快捷方式对不上。
APP_ID = "laoxiaozhi.wechat.gateway"

#: 启动动画最多挡多久（毫秒）。网关装配正常就一两秒；这条只在「微信没登录、UIA 起不来」
#: 那种卡住的情况下兜底——界面该露面还得露面，剩下的由状态栏自己说。
SPLASH_MAX_MS = 12_000


def _claim_taskbar_identity() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception as exc:  # noqa: BLE001 - 只影响任务栏图标，不该拦住启动
        _log.debug("设置任务栏标识失败", error=str(exc))


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

    # ── 第一件事：把启动动画画出来 ────────────────────────────────
    # 下面那一堆 import 要花几秒（冷启动更久），在它们回来之前屏幕上必须已经有东西。
    _claim_taskbar_identity()
    root = tk.Tk()
    root.withdraw()  # 主窗口先藏着，是否露面由后面决定
    splash = Splash(root, icon_png=icons.PNG_PATH)
    splash.stage("正在加载…")

    # ★ 让界面在网关忙的时候还能动。真机实测（主线程 mainloop + 一条后台线程狂跑纯 Python）：
    # 默认 5ms 时界面只跑到 37% 的心跳、迟到中位 40ms 最慢 86ms；调到 0.5ms 回到 66%
    # （= 完全无干扰时的水平）、迟到中位 10ms，代价是后台吞吐 -4%。
    # ⚠️ 这个代价只在「同时只有一条线程在啃 CPU」时才这么便宜：四条一起啃时吞吐会掉 97%。
    # 本进程正是前者（网关一条线程），哪天真要跑多线程 CPU 活，先把这行删了重测。
    sys.setswitchinterval(0.0005)

    from wxclient.config import load_settings
    from wxclient.heavy_import import HEAVY_IMPORT_LOCK
    from wxclient.logging_config import configure_logging

    from . import settings_store
    from .auth import ClientAuth
    from .client_api import ClientApi
    from .config import UiSettings
    from .logbuffer import LogBuffer, LogPipeline
    from .service import GatewayService, ServiceState
    from .single_instance import SingleInstance, raise_existing_window
    from .status_reporter import StatusReporter
    from .tray import Tray
    from .window import WINDOW_TITLE, MainWindow

    splash.stage("正在读取配置…")
    wechat_settings = load_settings(args.config)
    # 先把参数灌进环境变量再读配置——真实环境变量优先。
    # 只有一份参数：这台机器在「配置中心」里填的那份（客户端接口不下发配置）。
    settings_store.apply_to_environ(settings_store.effective(wechat_settings.data_dir))
    settings = UiSettings()

    # ★ 单实例：两个进程同时驱动同一个微信 = 同一条消息读两遍、回两遍，两边还各按各的
    # 节拍动窗口——风控最不喜欢的形状（2026-08-13 真机出过一次）。守卫要活到进程结束，
    # 所以挂在这里而不是 with 块里：文件一关，锁就没了。
    instance_guard = SingleInstance(wechat_settings.data_dir)
    if not instance_guard.acquire():
        # 默默退出会让人以为「双击了没反应」，所以尽力把已有窗口拽到前台。
        splash.close()
        root.update()  # 动画得真的消失：这条路径不进 mainloop，没人替它重绘
        raise_existing_window(WINDOW_TITLE)
        print("劳小智微信服务已经在运行了（同一台机器只能开一个）。", file=sys.stderr)
        return 0

    buffer = LogBuffer(capacity=settings.log_lines)
    log_pipeline = LogPipeline(
        buffer,
        lambda: configure_logging(log_dir=wechat_settings.data_dir / "logs"),
    )
    log_pipeline.install()

    def build() -> Gateway:
        # 惰性 import：build() 只在网关线程上跑，把 factory 那 2.7 秒（SQLAlchemy + 队列 +
        # 微信驱动）从「窗口出现之前」挪到「服务启动过程中」。顶上 import 的话，双击到
        # 看见窗口要等近 4 秒，而那几秒里屏幕上什么都没有，像是没点着。
        # 和微信探测线程串行：两个后台线程同时首次 import 重型 C 扩展会卡死在
        # Windows 加载器锁上（真机实证，见 heavy_import.py）。
        with HEAVY_IMPORT_LOCK:
            from wxclient.gateway.factory import build_gateway

        # 每次启动都重读一遍 UiSettings：界面上改完参数点「保存并重启」才真的按新值跑
        current = UiSettings()
        # 账号标识 = 登录拿到的那个招聘分身（一 HR 一分身一微信，PRD §1.3）。
        # 界面上那个「微信号」输入框已经删了：手抄的号错一个字符，表现是消息游标全乱。
        identity = auth_client.identity
        accounts = args.account or ([identity.account] if identity else "") or current.accounts
        return build_gateway(
            accounts=accounts,
            allow_real_actions=_real_actions(args, current),
            config_path=args.config,
            gateway_db=args.gateway_db,
            client_token=lambda: auth_client.token,
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
                settings_store.effective(wechat_settings.data_dir), force=True
            )

        def done() -> None:
            # 回调在后台线程上，动界面必须先回 tk 线程
            root.after(0, lambda: (
                window.set_busy(""),
                window.set_dry_run_notice(_dry_run_notice(args, UiSettings())),
            ))

        if service.restart_async(before_start=before_start, on_done=done) is not None:
            window.set_busy("正在按新参数重启服务")

    # ── 登录与状态上报 ────────────────────────────────────────────
    # 一条路：邮箱密码登录业务后端的招聘分身客户端接口（`client_api.py`）。
    # 契约 §7：**没有心跳**——在线/离线/异常全靠这台机器在状态变化时主动说一声，
    # 所以下面每一处状态变化都得记得开口，尤其是退出前那次 offline。
    base_url = settings_store.effective(wechat_settings.data_dir).get(
        "WXCLIENT_GATEWAY_BUSINESS_API_URL", ""
    )
    api = ClientApi(base_url)
    auth_client = ClientAuth(base_url, wechat_settings.data_dir,
                             client_version=__version__, api=api)

    def session_revoked() -> None:
        """服务端把这台设备的会话撤了（换设备、管理员撤销、账号停用）。

        Token 已经在 `handle_revoked` 里清掉了，这里只负责界面：停服务 + 回登录页。
        不弹模态框——它可能发生在没人看着屏幕的时候，弹窗会把后面所有事都堵住。
        """
        _log.warning("客户端会话已失效，回到登录页")
        window.logout()

    auth_client.set_revoked_hook(lambda: root.after(0, session_revoked))

    def wechat_capability_status(probe: dict[str, Any]) -> str:
        """微信连上之后，这台机器到底能干哪几件事（契约 §8 的 ``capabilityStatus``）。

        直接复用网关那份能力表（`gateway/capabilities.py`，074）：它算的就是「当下真能做
        哪几件」。**不另起一套判据**——两套迟早给出两个答案，而企业端只显示一个。
        重型 import 要和探测串行（Windows 加载器锁，见 heavy_import.py）。
        """
        with HEAVY_IMPORT_LOCK:
            from wxclient.gateway import COMMANDS
            from wxclient.gateway.capabilities import (
                capability_status,
                probe_capabilities,
            )

        return capability_status(probe_capabilities(
            uia=probe,
            allow_real_actions=_real_actions(args, UiSettings()),
            # 取文件通道由网关自己建（factory 里恒有），不是可配项。
            has_file_channel=True,
            # ★ 和 `gateway/factory.py` 传同一份 op 白名单：不传的话这里默认「所有 op 都支持」，
            # 于是同一台机器上，企业端看到的是 passed、网关自己算出来的是 limited。
            supported_ops=set(COMMANDS),
        ))

    reporter = StatusReporter(api, lambda: auth_client.token,
                              client_version=__version__,
                              capability_check=wechat_capability_status,
                              on_revoked=auth_client.handle_revoked)

    def on_wechat_probe(probe: Any, error: BaseException | None) -> None:
        """微信探测每 15 秒一拍的结果 → 契约 §8 的连接状态。去重在 reporter 里。"""
        if error is not None:
            reporter.wechat_probe_failed(error)
        elif probe is not None:
            reporter.wechat_probe(probe)

    splash.stage("正在准备界面…")
    window = MainWindow(
        root,
        buffer,
        service,
        data_dir=wechat_settings.data_dir,
        dry_run_notice=_dry_run_notice(args, settings),
        on_settings_saved=on_settings_saved,
        log_pipeline=log_pipeline,
        on_wechat_probe=on_wechat_probe,
    )

    def start_service() -> None:
        """★ 服务**只在这里**起：登录之后才轮到它跑。别再加第二条起服务的路。"""
        if autostart and service.state in (ServiceState.STOPPED, ServiceState.FAILED):
            service.start()

    def on_authenticated(identity: Any) -> None:
        # 不在这里补一次 online：登录成功时服务端已经把客户端置成 online（契约 §7），
        # 客户端再报一遍只是多一条审计。
        # 但去重记录要清：新会话那边什么都不知道，微信状态得重新说一遍。
        reporter.session_changed()
        _log.info("已登录", user=identity.title, agent=identity.agent_id)
        start_service()

    def on_logout() -> None:
        """退出登录：撤销服务端会话（契约 §6）。在后台线程上跑，由窗口那边丢过来。

        ★ 顺序是硬的：**先把 offline 送到，再撤销会话**。反过来的话（2026-08-15 真机），
        排队中的那条 offline 会拿着刚作废的 Token 出门，吃一个 401，于是界面弹一句
        「客户端会话已失效」——听着像出事了，其实是自己撤销了自己。
        所以这里用同步的 `shutdown()`（最多等 3 秒），不是丢进队列的 `client_offline()`。
        """
        reporter.shutdown("user_logout")
        auth_client.logout()
        reporter.session_changed()

    def logout_requested() -> None:
        """托盘点了「退出登录」。真正的动作（停服务 + 回登录页）在窗口那边，
        它自己会把停机丢到后台线程去等。"""
        window.show()
        window.logout()

    tray = Tray(
        service,
        # 菜单点哪一项就落到哪一页：open_log/open_settings 自己负责「露面 + 切页」，
        # 别在这儿只 show()——那样点「实时日志」出来的可能还是上次停着的配置中心。
        on_show=lambda: root.after(0, window.open_log),
        on_settings=lambda: root.after(0, window.open_settings),
        on_quit=lambda: root.after(0, lambda: shutdown(root, window, service, tray, reporter)),
        on_logout=lambda: root.after(0, logout_requested),
        can_logout=lambda: window.logged_in,  # 没登录时这一项直接是灰的
    )

    # ★ **登录之前一概不起服务**（用户 2026-08-15：「登录前不应该启动相关的服务，
    # 登录之后才有」）。所以窗口一定要露面——不然就是一个缩在托盘里、什么都不干、
    # 也不说为什么的图标。服务只在 `start_service()` 一处起。
    show_window = True
    revealed = False

    def reveal() -> None:
        """启动动画收尾、主窗口接手。多路会喊它（服务就绪 / 兜底超时），只认第一次。"""
        nonlocal revealed
        if revealed:
            return
        revealed = True
        if show_window:
            window.show()
        splash.close()

    def on_state_change(*_args: object) -> None:
        # 状态回调来自网关线程：界面回 tk 线程刷，托盘图标 pystray 自己是线程安全的
        root.after(0, window.refresh_state)
        tray.refresh()
        # 服务跑不起来 = 这台机器有会话但干不了活，正是契约 §7 的 `error`。
        # 企业端那一格会显示「客户端运行异常」，而不是一直挂着「运行中」。
        if service.state is ServiceState.FAILED:
            reporter.client_error("runtime_error")
        elif service.state is ServiceState.RUNNING:
            reporter.client_online("service_running")
        # 网关装配完（成了或没成）就该把界面交出去——这一段正是启动里最长的那几秒。
        if service.state is not ServiceState.STARTING:
            root.after(0, reveal)

    service.set_state_listener(on_state_change)

    splash.stage("正在启动托盘…")
    tray.run_detached()

    autostart = settings.autostart and not args.no_autostart

    # 上次登录过就直接接上（Token 不过期，契约 §2.2）——每天重开都要重登一遍是白折腾。
    # 这张票到底还算不算数，由第一次状态上报去问服务端：401 就地退回登录页。
    window.set_login_context(auth_client, version=__version__,
                             on_authenticated=on_authenticated, on_logout=on_logout)
    restored = auth_client.restore()
    if restored is not None:
        window.finish_login(restored)
        reporter.client_online("client_start")
        root.after(0, start_service)
    else:
        window.require_login(auth_client, version=__version__,
                             on_authenticated=on_authenticated)

    # 这里**只把界面交出去**，不碰服务：谁来用这台机器还不知道。
    root.after(0, reveal)
    # 兜底：装配卡住（微信没登录、UIA 不可用）也不能让启动动画一直挡在屏幕中间。
    root.after(SPLASH_MAX_MS, reveal)

    _log.info("桌面端已启动",
              real_actions=_real_actions(args, settings),
              autostart=autostart, logged_in=restored is not None)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown(root, window, service, tray, reporter)
    return 0


def shutdown(root: tk.Tk, window: MainWindow, service: GatewayService, tray: Tray,
             reporter: Any = None) -> None:
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
            # ★ 先把「我要下线了」送出去，再停服务（契约 §7：**没有心跳**，服务端不会
            # 自己把 online 改成 offline；漏了这一步，企业端会一直显示这台机器在线）。
            # 它最多等 3 秒——退出不能被网络卡住。
            if reporter is not None:
                reporter.shutdown()
            service.stop()
        finally:
            root.after(0, lambda: (window.destroy(), root.quit()))
            _exit_backstop()

    threading.Thread(target=finish, name="wx-ui-shutdown", daemon=True).start()


def _exit_backstop(seconds: float = 5.0) -> None:
    """收完尾还没退干净就硬退。

    正常路径（窗口销毁 → mainloop 返回 → main 返回）走完进程自己就没了，这条
    守护线程也跟着消失。它只在「有第三方库留了个非守护线程」时才轮得到发言——
    那种情况下用户看到的是「退出没反应」，比丢掉几行日志缓冲糟得多。
    """

    def bail() -> None:
        time.sleep(seconds)
        _log.warning("收尾超时，强制退出")
        os._exit(0)

    threading.Thread(target=bail, name="wx-ui-exit-backstop", daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
