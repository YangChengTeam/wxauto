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

_SEVERITY_COLOR = {
    "info": theme.GREEN,
    "warn": theme.ORANGE,
    "error": theme.RED,
    "debug": theme.GRAY,
}

_WECHAT_PROBE_SECONDS = 15


class MainWindow:
    def __init__(
        self,
        root: tk.Tk,
        buffer: LogBuffer,
        service: GatewayService,
        *,
        data_dir: Path,
        mode_text: str,
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
        self._mode_text = mode_text
        self._unsubscribe: Any = None
        self._row = 0
        self._wechat_status = ("未检测", theme.GRAY)
        self._probe_lock = threading.Lock()

        root.title("劳小智微信服务")
        root.geometry("1180x720")
        root.minsize(900, 560)
        root.configure(bg=theme.CONTENT_BG)
        root.protocol("WM_DELETE_WINDOW", self.hide)

        self._build_sidebar()
        self._build_content()

        self._show_page("log")
        self._append([logline.parse(line) for line in buffer.snapshot()])
        self._unsubscribe = buffer.subscribe(self._on_log_line)

        self.refresh_state()
        self._tick()
        self._schedule_wechat_probe()

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

        self._pages = tk.Frame(content, bg=theme.CONTENT_BG)
        self._pages.pack(fill=tk.BOTH, expand=True)

        self._log_page = self._build_log_page(self._pages)

        footer = tk.Frame(content, bg=theme.FOOTER_BG, height=36)
        footer.pack(fill=tk.X)
        footer.pack_propagate(False)
        tk.Label(footer, text="窗口关闭后，服务继续在托盘运行", font=theme.FONT_SMALL,
                 bg=theme.FOOTER_BG, fg=theme.TEXT_MUTED).pack(side=tk.LEFT, padx=24)
        self._mode_label = tk.Label(footer, text=self._mode_text, font=theme.FONT_SMALL,
                                    bg=theme.FOOTER_BG, fg=theme.TEXT_MUTED)
        self._mode_label.pack(side=tk.RIGHT, padx=24)

    def _action(self, text: str, command: Callable[[], None]) -> tk.Label:
        item = tk.Label(self._header_actions, text=text, font=theme.FONT_SMALL,
                        bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, cursor="hand2")
        item.pack(side=tk.RIGHT, padx=10)
        item.bind("<Button-1>", lambda _e: command())
        return item

    def _build_log_page(self, parent: tk.Misc) -> tk.Frame:
        page = tk.Frame(parent, bg=theme.CONTENT_BG)
        wrap = tk.Frame(page, bg=theme.ROW_LINE, padx=1, pady=1)
        wrap.pack(fill=tk.BOTH, expand=True, padx=24, pady=(0, 16))

        self._text = tk.Text(
            wrap,
            wrap=tk.WORD,
            font=theme.FONT_LOG,
            bg=theme.CONTENT_BG,
            fg=theme.TEXT,
            relief=tk.FLAT,
            padx=0,
            pady=0,
            spacing1=7,
            spacing3=7,
            tabs=("150p", "215p"),
            state=tk.DISABLED,
            cursor="arrow",
        )
        scroll = tk.Scrollbar(wrap, command=self._text.yview, width=12, relief=tk.FLAT)
        self._text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._text.tag_configure("row_odd", background=theme.ROW_ALT_BG)
        self._text.tag_configure("time", foreground=theme.TEXT_MUTED)
        self._text.tag_configure("message", foreground=theme.TEXT, lmargin2=215)
        for severity, color in _SEVERITY_COLOR.items():
            self._text.tag_configure(f"level_{severity}", foreground=color)
        return page

    # ── 页面切换 ──────────────────────────────────────────────────

    def _show_page(self, page: str) -> None:
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

    def destroy(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self.root.destroy()

    # ── 动作 ──────────────────────────────────────────────────────

    def clear(self) -> None:
        self.buffer.clear()
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)
        self._row = 0

    def open_data_dir(self) -> None:
        import subprocess
        import sys

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(self.data_dir)])  # noqa: S603,S607

    def open_settings(self) -> None:
        self._show_page("settings")

    def set_mode_text(self, text: str) -> None:
        self._mode_text = text
        self._mode_label.configure(text=text)

    # ── 状态刷新 ──────────────────────────────────────────────────

    def refresh_state(self) -> None:
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
            self._rows["gateway"].configure(text="连接中", fg=theme.ORANGE)
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
        self.root.after(1000, self._tick)

    def _schedule_wechat_probe(self) -> None:
        threading.Thread(target=self._probe_wechat, name="wx-ui-probe", daemon=True).start()
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

    def _on_log_line(self, line: str) -> None:
        # 日志来自网关线程，必须回到 tk 线程再动控件
        self.root.after(0, lambda: self._append([logline.parse(line)]))

    def _append(self, lines: list[logline.LogLine]) -> None:
        if not lines:
            return
        at_bottom = self._text.yview()[1] > 0.999  # 用户往回翻时不要把他拽回底部
        self._text.configure(state=tk.NORMAL)
        for item in lines:
            row_tag = "row_odd" if self._row % 2 else "row_even"
            self._row += 1
            start = self._text.index(tk.END + "-1c")
            self._text.insert(tk.END, f"{item.time}\t", ("time", row_tag))
            self._text.insert(tk.END, f"{item.level}\t", (f"level_{item.severity}", row_tag))
            self._text.insert(tk.END, f"{item.message}\n", ("message", row_tag))
            self._text.tag_add(row_tag, start, tk.END + "-1c")
        self._text.configure(state=tk.DISABLED)
        if at_bottom:
            self._text.see(tk.END)
