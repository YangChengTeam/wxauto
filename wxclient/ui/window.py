"""主窗口：左侧深色状态栏 + 右侧内容区（实时日志 / 参数设置两页）。

外观按 ``docs/wxui.png`` 设计稿。关窗 = 缩回托盘，不停服务——托盘程序的常规预期，
也避免误关把网关一起带走；真要退出走左下角「退出服务」或托盘菜单。

窗口只显示状态、不判断状态：服务状态来自 ``GatewayService``，微信登录状态来自
``uia_probe``（只读探测，在后台线程里跑，绝不阻塞界面）。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import logline, theme
from .logbuffer import LogBuffer
from .logfeed import FLUSH_MS, LogFeed
from .logtable import LEVEL_FILTERS, LogTable
from .service import GatewayService, ServiceState

_STATE_TEXT = {
    ServiceState.RUNNING: "运行中",
    ServiceState.STARTING: "启动中",
    ServiceState.STOPPING: "停止中",
    ServiceState.STOPPED: "已停止",
    ServiceState.FAILED: "启动失败",
}

_STATE_COLOR = {
    ServiceState.RUNNING: theme.GREEN,
    ServiceState.STARTING: theme.ORANGE,
    ServiceState.STOPPING: theme.ORANGE,
    ServiceState.STOPPED: theme.GRAY,
    ServiceState.FAILED: theme.RED,
}

_WECHAT_PROBE_SECONDS = 15

#: 窗口重新打开时从缓冲回放多少行。视图是单个 Text，几百行的重画是一次性的、不心疼。
_REPLAY_LINES = 800

#: 转圈用的帧。每秒换一帧（跟着 _tick 走），够看出「还活着」。
_SPINNER = ("◐", "◓", "◑", "◒")


class MainWindow:
    def __init__(
        self,
        root: tk.Tk,
        buffer: LogBuffer,
        service: GatewayService,
        *,
        data_dir: Path,
        dry_run_notice: str = "",
        on_quit: Callable[[], None],
        on_settings_saved: Callable[[bool], None] | None = None,
        log_pipeline: Any = None,
    ) -> None:
        self.root = root
        self.buffer = buffer
        self.service = service
        self.data_dir = Path(data_dir)
        self._on_quit = on_quit
        self._settings_saved_callback = on_settings_saved
        self._log_pipeline = log_pipeline
        self._dry_run_notice = dry_run_notice
        self._unsubscribe: Any = None
        self._wechat_status = ("未检测", theme.GRAY)
        self._probe_lock = threading.Lock()
        self._shutting_down = False
        self._busy_text = ""
        self._busy_frame = 0
        self._feed = LogFeed()
        self._destroyed = False
        self._log_visible = False  # 由 _sync_log_visibility() 每拍现场核对，别手动改
        self._page = "log"

        root.title("劳小智微信服务")
        self._apply_window_icon()
        root.geometry("1180x720")
        root.minsize(900, 560)
        root.configure(bg=theme.CONTENT_BG)
        root.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_sidebar()
        self._build_content()

        self._show_page("log")
        # 日志走「攒一批画一批」：网关线程只往 LogFeed 里塞，tk 线程按节拍取。
        # 默认是暂停态——窗口现在还缩着（start_hidden 是常态），没人看的日志一行都不画，
        # 真打开时再从缓冲整屏回放。
        self._unsubscribe = buffer.subscribe(self._feed.push)

        self.refresh_state()
        self._tick()
        self._pump_logs()
        self._schedule_wechat_probe()

    def _apply_window_icon(self) -> None:
        """标题栏 / Alt-Tab / 任务栏用同一个品牌图标（和托盘、快捷方式一致）。

        图标是锦上添花，装歪了不该让窗口开不出来——所以整段包起来，失败就用默认图标。
        """
        from . import icons

        try:
            path = icons.ico_path()
            if path:
                self.root.iconbitmap(default=path)
        except tk.TclError:
            # 非 Windows 的 tk 不吃 .ico，退回 PNG
            try:
                if icons.PNG_PATH.exists():
                    self._icon_image = tk.PhotoImage(file=str(icons.PNG_PATH))
                    self.root.iconphoto(True, self._icon_image)
            except tk.TclError:
                pass

    # ── 左侧状态栏 ────────────────────────────────────────────────

    def _build_sidebar(self) -> None:
        bar = tk.Frame(self.root, bg=theme.SIDEBAR_BG, width=300)
        bar.pack(side=tk.LEFT, fill=tk.Y)
        bar.pack_propagate(False)
        self._sidebar = bar

        tk.Label(bar, text="微信服务", font=theme.FONT_TITLE, bg=theme.SIDEBAR_BG,
                 fg=theme.SIDEBAR_TITLE).pack(anchor="w", padx=28, pady=(26, 18))

        self._status_label = tk.Label(bar, text="", font=theme.FONT_STATUS,
                                      bg=theme.SIDEBAR_BG, fg=theme.GREEN)
        self._status_label.pack(anchor="w", padx=28, pady=(6, 20))

        # 停机/重启要几秒到十几秒，这几秒必须有东西在动。静止的「停止中」和卡死长得一样。
        self._busy_label = tk.Label(bar, text="", font=theme.FONT_SMALL,
                                    bg=theme.SIDEBAR_BG, fg=theme.ORANGE,
                                    anchor="w", justify=tk.LEFT, wraplength=244)

        self._divider(bar)

        self._rows: dict[str, tk.Label] = {}
        for key, label in (("service", "服务"), ("gateway", "网关"), ("wechat", "微信")):
            self._rows[key] = self._status_row(bar, label)

        self._uptime_label = tk.Label(bar, text="", font=theme.FONT_SMALL,
                                      bg=theme.SIDEBAR_BG, fg=theme.SIDEBAR_MUTED)
        self._uptime_label.pack(anchor="w", padx=28, pady=(22, 0))

        self._divider(bar, pady=(24, 10))

        self._nav: dict[str, tk.Label] = {}
        for page, text in (("log", "实时日志"), ("settings", "参数设置")):
            item = tk.Label(bar, text=text, font=theme.FONT_BODY, bg=theme.SIDEBAR_BG,
                            fg=theme.SIDEBAR_TEXT, anchor="w", padx=28, pady=8, cursor="hand2")
            item.pack(fill=tk.X)
            item.bind("<Button-1>", lambda _e, p=page: self._show_page(p))
            self._nav[page] = item

        quit_item = tk.Label(bar, text="退出服务", font=theme.FONT_BODY, bg=theme.SIDEBAR_BG,
                             fg=theme.SIDEBAR_MUTED, cursor="hand2")
        quit_item.pack(side=tk.BOTTOM, anchor="w", padx=28, pady=24)
        quit_item.bind("<Button-1>", lambda _e: self._on_quit())

    def _divider(self, parent: tk.Misc, pady: tuple[int, int] = (10, 16)) -> None:
        tk.Frame(parent, bg=theme.SIDEBAR_LINE, height=1).pack(fill=tk.X, padx=28, pady=pady)

    def _status_row(self, parent: tk.Misc, label: str) -> tk.Label:
        row = tk.Frame(parent, bg=theme.SIDEBAR_BG)
        row.pack(fill=tk.X, padx=28, pady=9)
        tk.Label(row, text=label, font=theme.FONT_BODY, bg=theme.SIDEBAR_BG,
                 fg=theme.SIDEBAR_TEXT).pack(side=tk.LEFT)
        value = tk.Label(row, text="-", font=theme.FONT_BODY, bg=theme.SIDEBAR_BG, fg=theme.GRAY)
        value.pack(side=tk.RIGHT)
        return value

    # ── 右侧内容区 ────────────────────────────────────────────────

    def _build_content(self) -> None:
        content = tk.Frame(self.root, bg=theme.CONTENT_BG)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        header = tk.Frame(content, bg=theme.CONTENT_BG)
        header.pack(fill=tk.X, padx=24, pady=(20, 12))
        self._heading = tk.Label(header, text="实时日志", font=theme.FONT_HEADING,
                                 bg=theme.CONTENT_BG, fg=theme.TEXT)
        self._heading.pack(side=tk.LEFT)
        self._live_dot = tk.Label(header, text="●", font=(theme.UI, 9),
                                  bg=theme.CONTENT_BG, fg=theme.GREEN)
        self._live_dot.pack(side=tk.LEFT, padx=(12, 4))
        self._live_text = tk.Label(header, text="实时更新", font=theme.FONT_SMALL,
                                   bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._live_text.pack(side=tk.LEFT)

        self._header_actions = tk.Frame(header, bg=theme.CONTENT_BG)
        self._header_actions.pack(side=tk.RIGHT)
        self._action("打开目录", self.open_data_dir)
        self._action("清空", self.clear)

        # 级别过滤。排障时最常问的一句是「有没有报错」，让人自己在几千行里翻不合适。
        self._filter_bar = tk.Frame(header, bg=theme.CONTENT_BG)
        self._filter_bar.pack(side=tk.RIGHT, padx=(0, 18))
        self._filters: dict[str, tk.Label] = {}
        for name, allowed in LEVEL_FILTERS:
            chip = tk.Label(self._filter_bar, text=name, font=theme.FONT_SMALL,
                            bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED,
                            padx=10, pady=3, cursor="hand2")
            chip.pack(side=tk.LEFT, padx=2)
            chip.bind("<Button-1>", lambda _e, n=name, a=allowed: self._set_level_filter(n, a))
            self._filters[name] = chip
        self._active_filter = LEVEL_FILTERS[0][0]
        self._render_filter_chips()

        self._pages = tk.Frame(content, bg=theme.CONTENT_BG)
        self._pages.pack(fill=tk.BOTH, expand=True)

        self._log_page = self._build_log_page(self._pages)

        footer = tk.Frame(content, bg=theme.FOOTER_BG, height=36)
        footer.pack(fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(footer, text="窗口关闭后，服务继续在托盘运行", font=theme.FONT_SMALL,
                 bg=theme.FOOTER_BG, fg=theme.TEXT_MUTED).pack(side=tk.LEFT, padx=24)
        # 只在「不会真的发消息」时才出声。常态是真实发送，界面上摆个「模式：真实发送」
        # 属于把开发档位摆给客户看；但反过来一声不吭更糟——开关没打开时界面一切正常、
        # 消息其实一条没出去，那是静默陷阱。所以：正常无显示，异常才亮。
        self._dry_run_label = tk.Label(footer, text="", font=theme.FONT_SMALL,
                                       bg=theme.FOOTER_BG, fg=theme.ORANGE)
        self._render_dry_run_notice()

    def _action(self, text: str, command: Callable[[], None]) -> tk.Label:
        item = tk.Label(self._header_actions, text=text, font=theme.FONT_SMALL,
                        bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, cursor="hand2")
        item.pack(side=tk.RIGHT, padx=10)
        item.bind("<Button-1>", lambda _e: command())
        return item

    def _build_log_page(self, parent: tk.Misc) -> tk.Frame:
        page = tk.Frame(parent, bg=theme.CONTENT_BG)
        self._table = LogTable(page)
        self._table.pack(fill=tk.BOTH, expand=True, padx=24, pady=(0, 16))
        return page

    # ── 页面切换 ──────────────────────────────────────────────────

    def _show_page(self, page: str) -> None:
        self._page = page
        for child in self._pages.winfo_children():
            child.pack_forget()
        for key, item in self._nav.items():
            item.configure(bg=theme.SIDEBAR_ACTIVE_BG if key == page else theme.SIDEBAR_BG,
                           fg=theme.SIDEBAR_TITLE if key == page else theme.SIDEBAR_TEXT)

        if page == "settings":
            from .settings_page import SettingsPage

            self._heading.configure(text="参数设置")
            self._live_dot.pack_forget()
            self._live_text.pack_forget()
            self._filter_bar.pack_forget()
            for widget in self._header_actions.winfo_children():
                widget.pack_forget()
            settings_page = SettingsPage(self._pages, self.data_dir, on_saved=self._on_settings_saved)
            settings_page.pack(fill=tk.BOTH, expand=True)
        else:
            self._heading.configure(text="实时日志")
            self._live_dot.pack(side=tk.LEFT, padx=(12, 4))
            self._live_text.pack(side=tk.LEFT)
            for widget in self._header_actions.winfo_children():
                widget.pack(side=tk.RIGHT, padx=10)
            self._filter_bar.pack(side=tk.RIGHT, padx=(0, 18))
            self._log_page.pack(fill=tk.BOTH, expand=True)

    def _on_settings_saved(self, restart: bool) -> None:
        if self._settings_saved_callback is not None:
            self._settings_saved_callback(restart)
        self._show_page("log")

    # ── 显示 / 隐藏 ───────────────────────────────────────────────

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()

    def begin_shutdown(self) -> bool:
        """进入退出中：立刻把界面切成「正在停止…」。已经在退了返回 False。

        点了退出之后停机要花上几秒（等网关线程收口 + 微信队列停），这几秒里界面必须
        说得出自己在干什么，否则看起来就是卡死了。
        """
        if self._shutting_down:
            return False
        self._shutting_down = True
        self._status_label.configure(text="正在停止…", fg=theme.ORANGE)
        self._live_dot.configure(fg=theme.ORANGE)
        self.set_busy("正在停止服务，请稍候")
        return True

    def set_busy(self, text: str) -> None:
        """显示/清除「正在忙」的转圈提示。传空字符串 = 忙完了。

        可从任意线程调？**不可以**——它动控件，调用方必须已经在 tk 线程上
        （后台线程请用 ``root.after(0, ...)`` 丢回来）。
        """
        self._busy_text = text
        if not text:
            self._busy_label.pack_forget()
            return
        self._busy_label.pack(anchor="w", padx=28, pady=(0, 12))
        self._render_busy()

    def _render_busy(self) -> None:
        if not self._busy_text:
            return
        self._busy_frame = (self._busy_frame + 1) % len(_SPINNER)
        self._busy_label.configure(text=f"{_SPINNER[self._busy_frame]}  {self._busy_text}")

    def destroy(self) -> None:
        self._destroyed = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.root.destroy()

    # ── 动作 ──────────────────────────────────────────────────────

    def clear(self) -> None:
        self.buffer.clear()
        self._feed.drain()  # 攒着还没画的也一起扔，不然刚清完又被旧行填回来
        self._table.clear()
        self._render_live_hint()

    def open_data_dir(self) -> None:
        import subprocess
        import sys

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(self.data_dir)])  # noqa: S603,S607

    def open_settings(self) -> None:
        self._show_page("settings")

    def set_dry_run_notice(self, text: str) -> None:
        """设置「当前不会真的发消息」的提示；传空字符串表示一切正常，不显示。"""
        self._dry_run_notice = text
        self._render_dry_run_notice()

    def _render_dry_run_notice(self) -> None:
        if self._dry_run_notice:
            self._dry_run_label.configure(text=self._dry_run_notice)
            self._dry_run_label.pack(side=tk.RIGHT, padx=24)
        else:
            self._dry_run_label.pack_forget()

    # ── 状态刷新 ──────────────────────────────────────────────────

    def refresh_state(self) -> None:
        if self._shutting_down:
            return  # 退出中：状态已经定格在「正在停止…」，别让每秒一次的刷新把它盖掉

        state = self.service.state
        color = _STATE_COLOR.get(state, theme.GRAY)
        self._status_label.configure(text=_STATE_TEXT.get(state, "-"), fg=color)

        if state is ServiceState.FAILED and self.service.error:
            self._rows["service"].configure(text="启动失败", fg=theme.RED)
        elif state is ServiceState.RUNNING:
            self._rows["service"].configure(text="正常", fg=theme.GREEN)
        else:
            self._rows["service"].configure(text=_STATE_TEXT.get(state, "-"), fg=theme.GRAY)

        connected, total = self.service.connection_summary()
        if state is not ServiceState.RUNNING:
            self._rows["gateway"].configure(text="未启动", fg=theme.GRAY)
        elif connected <= 0:
            # 连不上和还没连上是两回事：前者要红着让人去看日志，后者几秒后自己会变绿。
            failed = self.service.connect_error()
            self._rows["gateway"].configure(
                text="连不上" if failed else "连接中",
                fg=theme.RED if failed else theme.ORANGE,
            )
        elif connected >= total:
            self._rows["gateway"].configure(text="已连接", fg=theme.GREEN)
        else:
            self._rows["gateway"].configure(text=f"{connected}/{total} 已连接", fg=theme.ORANGE)

        text, color = self._wechat_status
        self._rows["wechat"].configure(text=text, fg=color)

        self._live_dot.configure(fg=theme.GREEN if state is ServiceState.RUNNING else theme.GRAY)

    def _tick(self) -> None:
        """每秒刷一次运行时长与连接数——这两样不会有事件通知，只能自己看。"""
        seconds = self.service.uptime_seconds()
        if seconds is None:
            self._uptime_label.configure(text="未运行")
        else:
            hours, rest = divmod(int(seconds), 3600)
            minutes, secs = divmod(rest, 60)
            self._uptime_label.configure(text=f"已运行  {hours:02d}:{minutes:02d}:{secs:02d}")
        if self._log_pipeline is not None:
            self._log_pipeline.ensure()
        self.refresh_state()
        self._render_busy()
        if not self._destroyed:
            self.root.after(1000, self._tick)

    def _schedule_wechat_probe(self) -> None:
        threading.Thread(target=self._probe_wechat, name="wx-ui-probe", daemon=True).start()
        if not self._destroyed:
            self.root.after(_WECHAT_PROBE_SECONDS * 1000, self._schedule_wechat_probe)

    def _probe_wechat(self) -> None:
        if not self._probe_lock.acquire(blocking=False):
            return  # 上一次还没探完，跳过这轮，别堆线程
        try:
            from wxclient.uia_probe import probe_wechat_uia

            probe = probe_wechat_uia()
            if probe.get("logged_in"):
                status = ("已登录", theme.GREEN)
            elif probe.get("login_window"):
                status = ("待扫码", theme.ORANGE)
            else:
                status = (str(probe.get("reason") or "未登录")[:12], theme.GRAY)
        except Exception as exc:  # noqa: BLE001 - 探测失败只影响这一格显示
            status = (f"探测失败：{type(exc).__name__}"[:16], theme.GRAY)
        finally:
            self._probe_lock.release()
        self._wechat_status = status

    # ── 日志 ──────────────────────────────────────────────────────

    def _log_is_on_screen(self) -> bool:
        """日志表这会儿有没有真的在人眼前。

        **每次都现问，不留状态。** 早先是靠 show()/hide()/切页去记一个布尔量，结果漏掉了
        一堆让窗口重新露面的路径——从任务栏还原、从最小化恢复，都不经过 show()；而窗口
        最大化时 ``state()`` 返回的是 "zoomed" 不是 "normal"。任何一条漏掉，标志位就和
        现实对不上，日志从此不再刷新（2026-08-13 真机反馈「实时日志没有了」就是这么来的）。
        现在按 120ms 的节拍现场问一次 tk，漏不掉也不会跑偏，代价只是一次状态查询。
        """
        try:
            return self._page == "log" and self.root.state() not in {"withdrawn", "iconic"}
        except tk.TclError:
            return False  # 窗口正在销毁

    def _sync_log_visibility(self) -> None:
        visible = self._log_is_on_screen()
        if visible == self._log_visible:
            return
        self._log_visible = visible
        if not visible:
            self._feed.pause()
        elif self._feed.resume():
            self._replay()  # 暂停期间攒下的没画，从缓冲整屏补上

    def _set_level_filter(self, name: str, allowed: frozenset[str]) -> None:
        self._active_filter = name
        self._render_filter_chips()
        self._table.set_level_filter(allowed)
        self._render_live_hint()

    def _render_filter_chips(self) -> None:
        for name, chip in self._filters.items():
            active = name == self._active_filter
            chip.configure(bg=theme.ROW_LINE if active else theme.CONTENT_BG,
                           fg=theme.TEXT if active else theme.TEXT_MUTED)

    def _render_live_hint(self) -> None:
        """标题旁边报一下收了多少行——「它到底还在不在动」一眼可见。"""
        count = self._table.line_count
        self._live_text.configure(text=f"实时更新 · {count:,} 行" if count else "实时更新")

    def _replay(self) -> None:
        """从缓冲重画最后一屏。每行都是真控件，铺两千行会卡住，所以只取最后一截。"""
        self._table.clear()
        self._table.append([logline.parse(line) for line in self.buffer.snapshot()[-_REPLAY_LINES:]])
        self._render_live_hint()

    def _pump_logs(self) -> None:
        """tk 线程按节拍取一批日志画一次。空转的那次只有一次取锁，几乎不要钱。"""
        self._sync_log_visibility()
        lines = self._feed.drain()
        if lines:
            self._table.append([logline.parse(line) for line in lines])
            self._render_live_hint()
        if not self._destroyed:
            self.root.after(FLUSH_MS, self._pump_logs)

    def _append(self, lines: list[logline.LogLine]) -> None:
        self._table.append(lines)
