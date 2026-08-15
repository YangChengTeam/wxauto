"""主窗口：左侧深色状态栏 + 右侧内容区（实时日志 / 计划中心 / 配置中心三页）。

外观按 ``docs/main.png`` 设计稿（早先是 ``docs/wxui.png``）。关窗 = 缩回托盘，不停服务
——托盘程序的常规预期，也避免误关把网关一起带走；真要退出走左下角「退出服务」或托盘菜单。

**登录是一道真门**：没登录之前 ``require_login()`` 会把登录页盖在整个窗口上，日志、
配置、计划都看不到（``login_page.py``）。窗口本身照建不误——服务可能早就在跑了
（开机自启用的是上次拉下来的参数），登录管的是「谁能看、谁能改」。

窗口只显示状态、不判断状态：服务状态来自 ``GatewayService``，微信登录状态来自
``uia_probe``（只读探测，在后台线程里跑，绝不阻塞界面）。
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from wxclient.heavy_import import HEAVY_IMPORT_LOCK

from . import logline, theme
from .logbuffer import LogBuffer
from .logfeed import FLUSH_MS, LogFeed, is_log_on_screen
from .logtable import LEVEL_FILTERS, LogTable
from .service import GatewayService, ServiceState

_STATE_TEXT = {
    ServiceState.RUNNING: "运行中",
    ServiceState.STARTING: "启动中",
    ServiceState.STOPPING: "停止中",
    ServiceState.STOPPED: "已停止",
    ServiceState.FAILED: "启动失败",
}

#: 窗口标题。抽成常量是因为单实例守卫要靠它把已有窗口拽到前台
#: （`single_instance.raise_existing_window`）——两处写死迟早对不上。
WINDOW_TITLE = "劳小智微信服务"

_STATE_COLOR = {
    ServiceState.RUNNING: theme.GREEN,
    ServiceState.STARTING: theme.ORANGE,
    ServiceState.STOPPING: theme.ORANGE,
    ServiceState.STOPPED: theme.GRAY,
    ServiceState.FAILED: theme.RED,
}

_WECHAT_PROBE_SECONDS = 15

_log = structlog.get_logger(__name__)

#: 界面卡多久算「卡了一下」，要记一笔。心跳本身是 1 秒一次，留 0.6 秒余量给正常抖动。
_STALL_WARN_SECONDS = 0.6

#: 启动多久之后偷偷把参数页建好。要等过启动最忙的那一段，又要赶在用户去点之前。
_SETTINGS_PREBUILD_MS = 3000

#: 转场态（服务正在起/正在停）的提示语。这几秒里任何「再点一下」都只会把事情搞乱，
#: 所以界面自己进入 busy：侧栏转圈说明在干什么，参数页盖住不给改。
_TRANSITION_TEXT = {
    ServiceState.STARTING: "正在启动服务，请稍候",
    ServiceState.STOPPING: "正在停止服务，请稍候",
}

#: 窗口重新打开时从缓冲回放多少行。视图是单个 Text，几百行的重画是一次性的、不心疼。
_REPLAY_LINES = 800

#: 转圈用的帧。每秒换一帧（跟着 _tick 走），够看出「还活着」。
_SPINNER = ("◐", "◓", "◑", "◒")

#: 内容区各页的标题。加页在这里登记一条，别散在 _show_page 的分支里。
#:
#: 「参数设置」→「配置中心」、「监听名单」→「计划中心」是用户 2026-08-15 定的改法：
#: 参数以后由后端下发（``auth.py``），这一页的职责从「填参数」变成「看这台机器按什么在跑」；
#: 监听名单本来就是 AI 端推下来的、本机改不了，归到配置中心里当一节更合适，腾出来的位置
#: 留给后端下发的计划。
_PAGE_TITLES = {"log": "实时日志", "plans": "计划中心", "settings": "配置中心"}


class MainWindow:
    def __init__(
        self,
        root: tk.Tk,
        buffer: LogBuffer,
        service: GatewayService,
        *,
        data_dir: Path,
        dry_run_notice: str = "",
        on_settings_saved: Callable[[bool], None] | None = None,
        on_refetch: Callable[[], None] | None = None,
        log_pipeline: Any = None,
        probe_wechat: bool = True,
    ) -> None:
        self.root = root
        self.buffer = buffer
        self.service = service
        self.data_dir = Path(data_dir)
        self._settings_saved_callback = on_settings_saved
        self._refetch_callback = on_refetch
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
        self._settings_page: Any = None  # 懒建一次、复用；每次进页 reload() 重读磁盘上的值
        self._config_page: Any = None  # 配置中心的外壳（可滚动）：参数表 + 监听名单一节
        self._watch_text: Any = None  # 监听名单那个只读文本框，跟着配置中心一起建
        self._locked = False  # 转场态：由 _sync_transition_lock() 每拍现场核对，别手动改
        self._last_tick: float | None = None  # 卡顿看门狗的上一拍时刻
        self._ticks = 0  # 心跳计数：头两拍不报卡顿，理由见 _watch_for_stalls
        self._pending_after: set[str] = set()  # 排着的 after 回调，destroy 时要撤掉
        self._login: Any = None  # 登录页盖在最上面时是它，登进来之后是 None
        self._identity: Any = None  # 当前登录的人（auth.Identity）
        self._plans: tuple[Any, ...] = ()  # 后端下发的计划；这一版后端还没有，恒为空
        self._config_source = ""  # 参数是「后端」下发的还是「本机」填的，配置中心要说清
        self._logout_provider: Any = None  # 退出登录之后拿什么再登一次
        self._logout_callback: Any = None
        self._version = ""

        root.title(WINDOW_TITLE)
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
        # 探测线程会 import pywinauto 去枚举微信窗口——单测里既不需要，也不该去碰真微信；
        # 而且它一边跑一边触发 GC，GC 落在**非主线程**上回收 tk 对象时 Tcl 会直接 abort
        # 整个进程（`Tcl_AsyncDelete`，真机上实测过）。测试传 probe_wechat=False 关掉。
        if probe_wechat:
            self._schedule_wechat_probe()
        # 参数页提前建好：建控件是「很多次小 Tcl 调用」，正是主线程抢不到 GIL 时最吃亏的活。
        # 真机实测空闲时也要 9ms，网关忙着读聊天记录时点下去就是肉眼可见的一顿。
        # 排在启动之后的空闲档期建，用户点的时候只剩 pack/unpack。
        self._later(_SETTINGS_PREBUILD_MS, self._prebuild_settings_page)

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
        self._paint_sidebar_background(bar)

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
        for key, label in (("service", "服务"), ("gateway", "网关"), ("wechat", "微信"), ("watch", "监听")):
            self._rows[key] = self._status_row(bar, label)

        # 侧栏这一行只报「几个会话」。**具体是谁不铺在这儿**——名字一多就把整条侧栏顶下去，
        # 而且多数时候没人在看。要看名字点左边「监听名单」，和「实时日志 / 参数设置」一个路数。
        self._uptime_label = tk.Label(bar, text="", font=theme.FONT_SMALL,
                                      bg=theme.SIDEBAR_BG, fg=theme.SIDEBAR_MUTED)
        self._uptime_label.pack(anchor="w", padx=28, pady=(22, 0))

        self._divider(bar, pady=(24, 10))

        self._nav: dict[str, tk.Label] = {}
        for page, text in (("log", "实时日志"), ("plans", "计划中心"), ("settings", "配置中心")):
            item = tk.Label(bar, text=text, font=theme.FONT_BODY, bg=theme.SIDEBAR_BG,
                            fg=theme.SIDEBAR_TEXT, anchor="w", padx=28, pady=8, cursor="hand2")
            item.pack(fill=tk.X)
            item.bind("<Button-1>", lambda _e, p=page: self._show_page(p))
            self._nav[page] = item

        # ★ 底部**只有一个**「退出登录」（用户 2026-08-15：「退出登录和退出服务是一个意思」）。
        # 服务本来就只在登录之后才起（见 `__main__`），所以「不再用了」这件事只有一种表达：
        # 退出登录 = 停掉网关 + 回到登录页。摆两个按钮做同一件事，只会让人猜它们差在哪。
        # 真要关掉整个程序走托盘菜单「退出」。
        self._logout_item = tk.Label(bar, text="退出登录", font=theme.FONT_BODY,
                                     bg=theme.SIDEBAR_BG, fg=theme.SIDEBAR_MUTED, cursor="hand2")
        self._logout_item.bind("<Button-1>", lambda _e: self.logout())

        # 谁登的这台机器
        self._who = tk.Label(bar, text="", font=theme.FONT_SMALL, bg=theme.SIDEBAR_BG,
                             fg=theme.SIDEBAR_MUTED, anchor="w", justify=tk.LEFT)

    def _paint_sidebar_background(self, bar: tk.Frame) -> None:
        """侧栏铺一层电路暗纹（``assets/panel-bg.png``，取自设计稿 ``docs/bg.png``）。

        画布**先建**、用 place 铺满整条侧栏，后面那些 pack 出来的标签自然叠在它上面
        （tk 的叠放顺序就是创建顺序）。标签底色和图的底色是同一个 #1c1c1c，所以被盖住的
        只是纹路，看起来就是「字周围透出一点纹理」——正是设计稿的样子。

        图按 cover 铺（等比放大 + 居中裁剪，见 ``icons.cover_photo``），高度随窗口变，
        所以跟着 ``<Configure>`` 重铺一次。
        """
        canvas = tk.Canvas(bar, bg=theme.SIDEBAR_BG, highlightthickness=0, bd=0)
        canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._sidebar_canvas = canvas
        self._sidebar_size = (0, 0)
        canvas.bind("<Configure>", self._repaint_sidebar_background)

    def _repaint_sidebar_background(self, event: Any) -> None:
        from . import icons

        size = (event.width, event.height)
        if size == self._sidebar_size:
            return
        self._sidebar_size = size
        photo = icons.cover_photo(self._sidebar_canvas, icons.PANEL_BG_PATH, *size)
        if photo is None:
            return  # 铺不上就是一条纯色侧栏，不影响任何功能
        self._sidebar_photo = photo  # 自己留引用，被 GC 掉就成了空白
        self._sidebar_canvas.delete("bg")
        self._sidebar_canvas.create_image(0, 0, anchor="nw", image=photo, tags="bg")

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

        self._banner = self._build_banner(content)

        self._pages = tk.Frame(content, bg=theme.CONTENT_BG)
        self._pages.pack(fill=tk.BOTH, expand=True)

        self._log_page = self._build_log_page(self._pages)
        self._plans_page = self._build_plans_page(self._pages)
        self._overlay = self._build_overlay(self._pages)

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

    def _build_banner(self, parent: tk.Misc) -> tk.Frame:
        """顶部告警条：连不上 AI 端这种事，不该只在日志里滚过去。

        它**只报一件事**：现在有没有一个「看得见但自己好不了」的毛病，以及去哪儿治。
        右边那个「检查参数 ›」直接跳配置中心——多数情况就是地址或 key 填错了。
        平时整条不占位（``pack_forget``），别让界面天天顶着一条黄边。
        """
        banner = tk.Frame(parent, bg=theme.BANNER_BG)
        inner = tk.Frame(banner, bg=theme.BANNER_BG)
        inner.pack(fill=tk.X, padx=24, pady=12)
        tk.Label(inner, text="⚠", font=(theme.UI, 12), bg=theme.BANNER_BG,
                 fg=theme.ORANGE).pack(side=tk.LEFT, padx=(0, 10))
        self._banner_title = tk.Label(inner, text="", font=theme.FONT_TITLE,
                                      bg=theme.BANNER_BG, fg=theme.TEXT)
        self._banner_title.pack(side=tk.LEFT)
        self._banner_detail = tk.Label(inner, text="", font=theme.FONT_SMALL,
                                       bg=theme.BANNER_BG, fg=theme.TEXT_MUTED)
        self._banner_detail.pack(side=tk.LEFT, padx=(16, 0))
        action = tk.Label(inner, text="检查参数  ›", font=theme.FONT_SMALL,
                          bg=theme.BANNER_BG, fg=theme.ORANGE, cursor="hand2")
        action.pack(side=tk.RIGHT)
        action.bind("<Button-1>", lambda _e: self._show_page("settings"))
        return banner

    def _render_banner(self, snapshot: Any, state: ServiceState) -> None:
        title, detail = "", ""
        if state is ServiceState.FAILED:
            title, detail = "服务没起来", (self.service.error or "看下面的日志")[:60]
        elif state is ServiceState.RUNNING and snapshot.connect_error:
            title, detail = "AI 端连接异常", f"{str(snapshot.connect_error)[:50]}，消息处理将继续重试"
        elif state is ServiceState.RUNNING and snapshot.backlog > 0:
            # 「微信显示已读、却没人回」在专员眼里就是坏了（070）。积压要说出来。
            title, detail = "消息还没送出去", f"欠 {snapshot.backlog} 批，连上 AI 端后会自动补发"
        if not title:
            self._banner.pack_forget()
            return
        self._banner_title.configure(text=title)
        self._banner_detail.configure(text=detail)
        if not self._banner.winfo_manager():
            self._banner.pack(fill=tk.X, before=self._pages)

    def _action(self, text: str, command: Callable[[], None]) -> tk.Label:
        item = tk.Label(self._header_actions, text=text, font=theme.FONT_SMALL,
                        bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, cursor="hand2")
        item.pack(side=tk.RIGHT, padx=10)
        item.bind("<Button-1>", lambda _e: command())
        return item

    def _build_overlay(self, parent: tk.Misc) -> tk.Frame:
        """转场遮罩：盖住参数页，把点击全吞掉。

        为什么**只盖参数页、不盖日志页**：起服务/停服务那几秒里最该看的就是日志，
        盖住它等于把唯一的进度信息也拿走了。真正不能碰的是参数——正在重启时点
        「保存并重启服务」会叠一次重启上去。
        """
        frame = tk.Frame(parent, bg=theme.CONTENT_BG, cursor="watch")
        box = tk.Frame(frame, bg=theme.CONTENT_BG)
        box.place(relx=0.5, rely=0.42, anchor="center")
        self._overlay_label = tk.Label(box, text="", font=theme.FONT_BODY,
                                       bg=theme.CONTENT_BG, fg=theme.TEXT)
        self._overlay_label.pack()
        tk.Label(box, text="这几秒别改参数，改完的值会被正在收尾的旧实例读走",
                 font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                 fg=theme.TEXT_MUTED).pack(pady=(8, 0))
        # 挡住输入靠的是**几何遮挡**：遮罩盖在参数页上面，鼠标事件先落到它身上，
        # 下面的「保存并重启服务」就点不着了。这里再显式吞一道，免得哪天有人给遮罩
        # 加了子控件又忘了这回事。（别绑 <Key>：Frame 拿不到键盘焦点，绑了也不会触发。）
        for event in ("<Button-1>", "<Button-2>", "<Button-3>", "<Double-Button-1>"):
            frame.bind(event, lambda _e: "break")
        return frame

    def _build_log_page(self, parent: tk.Misc) -> tk.Frame:
        page = tk.Frame(parent, bg=theme.CONTENT_BG)
        self._table = LogTable(page)
        self._table.pack(fill=tk.BOTH, expand=True, padx=24, pady=(0, 16))
        return page

    def _build_plans_page(self, parent: tk.Misc) -> tk.Frame:
        """计划中心。**这一版是空的**，而且空得有话说。

        计划（几点做什么、多久跟进一次）以后由后端下发（``auth.py`` 的 ``plans`` 字段，
        契约已经定好、客户端已经会解析）。后端还没有那一头，所以这页现在只会告诉你
        「还没有计划」——比摆一个能点却什么都不会发生的「新建计划」按钮诚实。
        本机不做计划编辑：一台机器一份本地计划，换台机器就对不上了。
        """
        page = tk.Frame(parent, bg=theme.CONTENT_BG)
        self._plans_hint = tk.Label(page, text="", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                                    fg=theme.TEXT_MUTED, anchor="w", justify=tk.LEFT)
        self._plans_hint.pack(fill=tk.X, padx=24, pady=(0, 12))
        self._plans_box = tk.Frame(page, bg=theme.CONTENT_BG)
        self._plans_box.pack(fill=tk.BOTH, expand=True, padx=24)
        return page

    def _render_plans_page(self) -> None:
        if self._page != "plans":
            return
        if not self._plans:
            self._plans_hint.configure(
                text="还没有计划。计划由后端配置后下发到这台机器，这里只负责显示和执行。"
            )
        else:
            self._plans_hint.configure(text=f"后端下发了 {len(self._plans)} 条计划。")
        for child in self._plans_box.winfo_children():
            child.destroy()
        for plan in self._plans:
            row = tk.Frame(self._plans_box, bg=theme.ROW_ALT_BG)
            row.pack(fill=tk.X, pady=(0, 8))
            tk.Label(row, text=plan.title, font=theme.FONT_BODY, bg=theme.ROW_ALT_BG,
                     fg=theme.TEXT).pack(side=tk.LEFT, padx=14, pady=10)
            tk.Label(row, text=plan.detail, font=theme.FONT_SMALL, bg=theme.ROW_ALT_BG,
                     fg=theme.TEXT_MUTED).pack(side=tk.LEFT)
            tk.Label(row, text=plan.next_run_at or ("已启用" if plan.enabled else "已停用"),
                     font=theme.FONT_SMALL, bg=theme.ROW_ALT_BG,
                     fg=theme.TEXT_MUTED).pack(side=tk.RIGHT, padx=14)

    def _build_watch_section(self, parent: tk.Misc) -> tk.Frame:
        """监听名单：AI 端推下来的会话名单，一行一个。**配置中心里的一节。**

        名单是「谁会被自动回复」的唯一判据（069 fail-closed），所以必须看得见**具体是谁**；
        但它是 AI 端说了算的，本机改不了——摆在配置中心里当只读的一节，正好和上面那些
        「本机能改的参数」形成对照（用户 2026-08-15：监听名单归到配置中心）。
        """
        page = tk.Frame(parent, bg=theme.CONTENT_BG)
        tk.Label(page, text="监听名单", font=theme.FONT_HEADING, bg=theme.CONTENT_BG,
                 fg=theme.TEXT).pack(anchor="w", pady=(8, 6))
        self._watch_hint = tk.Label(page, text="", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                                    fg=theme.TEXT_MUTED, anchor="w", justify=tk.LEFT)
        self._watch_hint.pack(fill=tk.X, pady=(0, 10))

        box = tk.Frame(page, bg=theme.ROW_LINE, padx=1, pady=1)
        box.pack(fill=tk.BOTH, expand=True, pady=(0, 16))
        self._watch_text = tk.Text(
            box, bg=theme.CONTENT_BG, fg=theme.TEXT, font=theme.FONT_BODY,
            relief=tk.FLAT, highlightthickness=0, bd=0, padx=18, pady=12,
            spacing1=3, spacing3=5, cursor="arrow", insertwidth=0, wrap=tk.NONE,
        )
        scroll = tk.Scrollbar(box, orient=tk.VERTICAL, command=self._watch_text.yview, width=12)
        self._watch_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._watch_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._watch_text.configure(height=8)  # 一节而不是一整页，给上面的参数留地方
        self._watch_text.tag_configure("empty", foreground=theme.ORANGE)
        self._watch_text.configure(state=tk.DISABLED)
        # 只读，但要能选中复制——「这个人到底在不在名单上」经常要贴给别人看
        self._watch_text.bind("<Key>", lambda event: None
                              if (event.state & 0x4 and event.keysym in {"c", "C", "a", "A"})
                              or event.keysym in {"Up", "Down", "Prior", "Next", "Home", "End"}
                              else "break")
        return page

    def _render_watch_page(self) -> None:
        """把名单铺到页面上。停在配置中心时每秒刷一次——名单是 AI 端随时会改的。"""
        if self._page != "settings" or self._watch_text is None:
            return
        watched = list(self.service.status().watched)
        running = self.service.state is ServiceState.RUNNING
        if not running:
            self._watch_hint.configure(text="服务没在跑，名单由 AI 端在运行时推下来。")
        elif watched:
            self._watch_hint.configure(text=f"共 {len(watched)} 个会话会被自动回复；不在名单里的一律不回。")
        else:
            self._watch_hint.configure(text="名单是空的——这意味着一条消息都不会回。")

        body = "\n".join(watched) if watched else ("（空）" if running else "（未启动）")
        if self._watch_text.get("1.0", "end-1c") == body:
            return  # 没变就别重画，省得把用户选中的东西冲掉
        self._watch_text.configure(state=tk.NORMAL)
        self._watch_text.delete("1.0", tk.END)
        self._watch_text.insert("1.0", body, () if watched else ("empty",))
        self._watch_text.configure(state=tk.DISABLED)

    # ── 页面切换 ──────────────────────────────────────────────────

    def _show_page(self, page: str) -> None:
        if page == "settings" and self._locked:
            return  # 转场态不给进参数页，见 _build_overlay
        self._page = page
        for child in self._pages.winfo_children():
            child.pack_forget()
        self._render_nav()
        self._heading.configure(text=_PAGE_TITLES.get(page, _PAGE_TITLES["log"]))

        # 「实时更新 · N 行」「级别过滤」「打开目录 / 清空」这一排只属于日志页
        if page == "log":
            self._live_dot.pack(side=tk.LEFT, padx=(12, 4))
            self._live_text.pack(side=tk.LEFT)
            for widget in self._header_actions.winfo_children():
                widget.pack(side=tk.RIGHT, padx=10)
            self._filter_bar.pack(side=tk.RIGHT, padx=(0, 18))
            self._log_page.pack(fill=tk.BOTH, expand=True)
        else:
            self._live_dot.pack_forget()
            self._live_text.pack_forget()
            self._filter_bar.pack_forget()
            for widget in self._header_actions.winfo_children():
                widget.pack_forget()
            if page == "settings":
                self._ensure_settings_page().pack(fill=tk.BOTH, expand=True)
                self._render_watch_page()
            else:
                self._plans_page.pack(fill=tk.BOTH, expand=True)
                self._render_plans_page()
        self._render_overlay()  # 每个分支都要摆一次：切走了就得把遮罩收掉

    def _prebuild_settings_page(self) -> None:
        """空闲时把配置中心建出来（不显示）。失败无所谓——真点的时候还会再建一次。"""
        if self._destroyed or self._config_page is not None:
            return
        try:
            self._ensure_settings_page()
        except tk.TclError:
            pass

    def _ensure_settings_page(self) -> Any:
        """设置页只建一次，之后每次进页重读磁盘上的值。

        早先是每次切页 ``SettingsPage(...)`` 新建一个、旧的只 ``pack_forget()``——控件一直
        挂在 ``_pages`` 底下不销毁，来回切几十次就攒几十份。改销毁也不行：保存那条路径是
        ``_save() → on_saved() → _show_page("log")``，销毁完回到 ``_save()`` 还要拿
        ``parent=self`` 弹提示框，父控件已经没了会直接抛 TclError。复用 + reload 两头都躲开。

        ``SettingsPage`` 的 import 仍然留在这儿：它拖着 ``tkinter.messagebox``，
        启动时不进设置页就不该为它付这份钱。
        """
        if self._config_page is None:
            from .settings_page import SettingsPage

            self._config_page, inner = self._build_scroller(self._pages)
            self._settings_page = SettingsPage(
                inner, self.data_dir, on_saved=self._on_settings_saved,
                on_refetch=self._refetch_config,
            )
            self._settings_page.pack(fill=tk.X)
            self._build_watch_section(inner).pack(fill=tk.BOTH, expand=True, padx=24)
        else:
            self._settings_page.reload()
        return self._config_page

    def _build_scroller(self, parent: tk.Misc) -> tuple[tk.Frame, tk.Frame]:
        """可滚动的壳。配置中心装得下十几行参数 + 一段名单，一屏是放不下的。

        滚轮走 ``bind_all``，进出这一页时装卸：Windows 上滚轮事件只发给指针底下的控件，
        指针多半停在某个输入框上，绑在画布上根本收不到。装卸而不是常驻，是为了别把
        日志页的滚动也一起劫走。
        """
        page = tk.Frame(parent, bg=theme.CONTENT_BG)
        canvas = tk.Canvas(page, bg=theme.CONTENT_BG, highlightthickness=0, bd=0)
        scroll = tk.Scrollbar(page, orient=tk.VERTICAL, command=canvas.yview, width=12)
        inner = tk.Frame(canvas, bg=theme.CONTENT_BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def wheel(event: Any) -> None:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        page.bind("<Enter>", lambda _e: page.bind_all("<MouseWheel>", wheel))
        page.bind("<Leave>", lambda _e: page.unbind_all("<MouseWheel>"))
        return page, inner

    def _refetch_config(self) -> None:
        """配置中心上的「重新拉取」。真正干活的是 ``__main__`` 注入的回调。"""
        if self._refetch_callback is not None:
            self._refetch_callback()

    # ── 转场态（正在启动/停止）：不让操作 ─────────────────────────

    def _sync_transition_lock(self) -> None:
        """每拍现场核对一次：服务正在起/停，界面就进 busy。

        **不留状态、不靠事件**——早先「日志刷不出来」那次教训就是靠布尔量记可见性，
        漏了几条路径就再也对不上（见 `_log_is_on_screen`）。这里同样每拍问一次现实。
        """
        locked = self.service.state in _TRANSITION_TEXT or self.service.restarting
        if locked == self._locked:
            return
        self._locked = locked
        # 停在参数页的**不把人赶走**：正在填的东西不该被界面自作主张丢掉。盖一层遮罩挡住
        # 输入就够了，想看启动进度的自己点左边「实时日志」（转场态只禁参数页，不禁日志页）。
        # 〔早先这里是 `_show_page("log")` 把人请出去，结果 `_locked and page=="settings"`
        #   永远不成立——遮罩成了永远不会出现的死代码。〕
        self._render_nav()
        self._render_overlay()

    def _render_nav(self) -> None:
        """转场态里「参数设置」变灰、鼠标不再变手型——点不动这件事要看得出来。"""
        for key, item in self._nav.items():
            disabled = self._locked and key == "settings"
            active = key == self._page and not disabled
            item.configure(
                bg=theme.SIDEBAR_ACTIVE_BG if active else theme.SIDEBAR_BG,
                fg=theme.SIDEBAR_LINE if disabled
                else (theme.SIDEBAR_TITLE if active else theme.SIDEBAR_TEXT),
                cursor="" if disabled else "hand2",
            )

    def _render_overlay(self) -> None:
        if self._locked and self._page == "settings":
            self._overlay_label.configure(text=self._busy_text or "正在切换服务状态，请稍候")
            self._overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._overlay.lift()
        else:
            self._overlay.place_forget()

    def _on_settings_saved(self, restart: bool) -> None:
        if self._settings_saved_callback is not None:
            self._settings_saved_callback(restart)
        self._show_page("log")

    # ── 登录门 ────────────────────────────────────────────────────

    def require_login(self, provider: Any, *, version: str = "",
                      on_authenticated: Callable[[Any], None] | None = None) -> None:
        """把登录页盖在整个窗口上。登进来之前，日志/配置/计划一概看不到。

        用 ``place`` 盖满而不是「先别建主窗口」：服务可能早就在跑了（开机自启用的是上次
        拉下来的参数），主窗口该建照建、状态照更新，只是没登录的人看不见。
        """
        if self._login is not None:
            return
        from .login_page import LoginPage

        def done(identity: Any) -> None:
            self.finish_login(identity)
            if on_authenticated is not None:
                on_authenticated(identity)

        self._login = LoginPage(self.root, provider, data_dir=self.data_dir,
                                on_authenticated=done, version=version)
        self._login.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._login.lift()
        self._sync_login_status()

    def finish_login(self, identity: Any) -> None:
        self._identity = identity
        if self._login is not None:
            self._login.destroy()
            self._login = None
        self._render_identity()
        self._show_page("log")

    def logout(self) -> None:
        """退出登录 = **停掉服务** + 回到登录页（用户 2026-08-15 把两件事合成了一件）。

        停机要等网关线程收口（最长 15s），所以走 ``stop_async``——在 tk 线程上等的话，
        这十几秒里窗口一次都不重绘，看起来就是点完卡死了。
        """
        if self._login is not None or self._logout_provider is None:
            return
        self._identity = None
        self._render_identity()
        if self.service.state in {ServiceState.RUNNING, ServiceState.STARTING}:
            self.set_busy("正在停止服务，请稍候")
            self.service.stop_async()
        self.require_login(self._logout_provider, version=self._version,
                           on_authenticated=self._logout_callback)

    def set_login_context(self, provider: Any, *, version: str = "",
                          on_authenticated: Callable[[Any], None] | None = None) -> None:
        """记下「退出登录之后拿什么再登一次」。require_login 之前调。"""
        self._logout_provider = provider
        self._logout_callback = on_authenticated
        self._version = version

    @property
    def logged_in(self) -> bool:
        return self._login is None

    @property
    def identity(self) -> Any:
        """当前登录的人（``auth.Identity``）；没登录就是 None。"""
        return self._identity

    def _render_identity(self) -> None:
        if self._identity is None:
            # 连文字一起抹掉：留着上一个人的名字比空着更糟。「退出登录」也跟着收起来——
            # 没人登录的时候它没有意义（登录页正盖在上面）。
            self._who.configure(text="")
            self._who.pack_forget()
            self._logout_item.pack_forget()
            return
        tenant = getattr(self._identity, "tenant_name", "")
        title = getattr(self._identity, "title", "") or getattr(self._identity, "account", "")
        self._who.configure(text=f"{title}\n{tenant}" if tenant else title)
        if not self._who.winfo_manager():
            self._logout_item.pack(side=tk.BOTTOM, anchor="w", padx=28, pady=(0, 24))
            self._who.pack(side=tk.BOTTOM, anchor="w", padx=28, pady=(0, 10))

    def _sync_login_status(self) -> None:
        """登录页左下角那行「本地服务…」要说实话，跟着服务状态走。"""
        if self._login is None:
            return
        state = self.service.state
        text = {
            ServiceState.RUNNING: "本地服务已就绪",
            ServiceState.STARTING: "本地服务正在启动",
            ServiceState.STOPPING: "本地服务正在停止",
            ServiceState.FAILED: "本地服务启动失败",
        }.get(state, "本地服务未启动")
        self._login.set_local_status(text, _STATE_COLOR.get(state, theme.GRAY))

    def set_plans(self, plans: Any) -> None:
        """后端下发的计划。这一版恒为空，接上后端就有东西了。"""
        self._plans = tuple(plans or ())
        self._render_plans_page()

    def set_config_source(self, source: str) -> None:
        self._config_source = source
        if self._settings_page is not None:
            self._settings_page.set_source(source)

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

        只是**额外**说一句（「正在按新参数重启服务」这种）。起服务/停服务本身不用谁来
        通知——`_effective_busy_text()` 会照着服务状态自己出提示，见那儿的说明。

        可从任意线程调？**不可以**——它动控件，调用方必须已经在 tk 线程上
        （后台线程请用 ``root.after(0, ...)`` 丢回来）。
        """
        self._busy_text = text
        self._render_busy()

    def _effective_busy_text(self) -> str:
        """现在该显示什么忙碌提示。显式 set_busy 的优先，否则看服务状态。

        ★ 为什么要自动出：转场那几秒是「点了没反应」的高发区（启动要装配网关、停机要等
        网关线程收口，都能到十几秒）。原来只有「保存并重启」那一条路会 set_busy，从托盘
        点启动/停止时侧栏一片安静，静止的「启动中」和卡死长得一模一样。
        """
        if self._busy_text:
            return self._busy_text
        if self.service.restarting:
            return "正在重启服务，请稍候"
        return _TRANSITION_TEXT.get(self.service.state, "")

    def _render_busy(self) -> None:
        text = self._effective_busy_text()
        if not text:
            self._busy_label.pack_forget()
            return
        if not self._busy_label.winfo_manager():
            self._busy_label.pack(anchor="w", padx=28, pady=(0, 12))
        self._busy_frame = (self._busy_frame + 1) % len(_SPINNER)
        self._busy_label.configure(text=f"{_SPINNER[self._busy_frame]}  {text}")
        if self._overlay.winfo_manager():
            self._overlay_label.configure(text=text)

    def _later(self, delay_ms: int, callback: Callable[[], None]) -> None:
        """排一个定时回调，并记下 id——销毁时要撤干净。

        不撤的话，窗口销毁后那些还排着的心跳照样会被 Tcl 叫起来，而它们要动的控件已经没了：
        退出时 stderr 上会甩出一串 ``invalid command name "...._pump_logs"``。
        不致命，但那是「退出过程出错」的样子，排障时会把人往沟里带。
        """
        if self._destroyed:
            return
        holder: dict[str, str] = {}

        def run() -> None:
            # 谁跑完谁把自己摘掉。**别在别处整集合清空**——`_pump_logs` 是 120ms 一拍、
            # 微信探测是 15s 一拍，跟着心跳一起清就等于 destroy 时撤不到它们。
            self._pending_after.discard(holder.get("id", ""))
            callback()

        holder["id"] = self.root.after(delay_ms, run)
        self._pending_after.add(holder["id"])

    def destroy(self) -> None:
        self._destroyed = True
        for handle in self._pending_after:
            try:
                self.root.after_cancel(handle)
            except tk.TclError:
                pass  # 已经跑过或已经没了，无所谓
        self._pending_after.clear()
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
            subprocess.Popen(["explorer", str(self.data_dir)])

    def open_log(self) -> None:
        """托盘「显示日志」：窗口露面，**并且停在日志页**。

        只 deiconify 是不够的——上次要是停在参数设置页，点「显示日志」弹出来的还是设置页，
        看起来就是菜单点了不算数。
        """
        self.show()
        self._show_page("log")

    def open_settings(self) -> None:
        """托盘「参数设置」：窗口露面，并且停在设置页。"""
        self.show()
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

        # 一次读一份快照：这几个值都由后台轮询线程攒好（GatewayStatus），
        # 主线程读它不拿锁、不查库、不进网关（原来这儿每秒要开一次 SQLite 连接）。
        snapshot = self.service.status()
        connected, total = snapshot.connected, snapshot.accounts
        if state is not ServiceState.RUNNING:
            self._rows["gateway"].configure(text="未启动", fg=theme.GRAY)
        elif connected <= 0:
            # 连不上和还没连上是两回事：前者要红着让人去看日志，后者几秒后自己会变绿。
            failed = snapshot.connect_error
            self._rows["gateway"].configure(
                text="连不上" if failed else "连接中",
                fg=theme.RED if failed else theme.ORANGE,
            )
        elif (backlog := snapshot.backlog) > 0:
            # 连着但有积压：消息读到了、还没送出去。不显示出来的话，专员看到的就是
            # 「微信显示已读、可是没人回」——只会以为功能坏了（070）。
            self._rows["gateway"].configure(text=f"已连接 · 欠 {backlog} 批", fg=theme.ORANGE)
        elif connected >= total:
            self._rows["gateway"].configure(text="已连接", fg=theme.GREEN)
        else:
            self._rows["gateway"].configure(text=f"{connected}/{total} 已连接", fg=theme.ORANGE)

        text, color = self._wechat_status
        self._rows["wechat"].configure(text=text, fg=color)

        # 名单是「谁会被自动回复」的唯一判据（069 fail-closed）。它只在日志里闪过一行，
        # 于是「这个人怎么没被回」永远得去翻日志——摆到状态栏上，一眼就知道。
        watched = list(snapshot.watched) if state is ServiceState.RUNNING else []
        if state is not ServiceState.RUNNING:
            self._rows["watch"].configure(text="未启动", fg=theme.GRAY)
        elif not watched:
            # 空名单不是「没事」：意味着一条消息都不会回。必须是显眼的颜色。
            self._rows["watch"].configure(text="名单是空的", fg=theme.ORANGE)
        else:
            self._rows["watch"].configure(text=f"{len(watched)} 个会话", fg=theme.GREEN)
        self._render_watch_page()  # 停在配置中心时跟着刷；不在那页是个空操作
        self._render_banner(snapshot, state)
        self._sync_login_status()  # 登录页那行「本地服务…」也跟着现实走

        self._live_dot.configure(fg=theme.GREEN if state is ServiceState.RUNNING else theme.GRAY)

    def _tick(self) -> None:
        """每秒刷一次运行时长与连接数——这两样不会有事件通知，只能自己看。"""
        # 先把日志通道接回来再报卡顿：微信探测线程首次 import pywinauto/comtypes 会把
        # 整个 logging 重置掉，顺序反了的话，「刚被重置的那一拍」正好也是最该留痕的那一拍，
        # 告警会凭空消失（真机 2026-08-14 写测试时撞到）。
        if self._log_pipeline is not None:
            self._log_pipeline.ensure()
        self._watch_for_stalls()
        seconds = self.service.uptime_seconds()
        if seconds is None:
            self._uptime_label.configure(text="未运行")
        else:
            hours, rest = divmod(int(seconds), 3600)
            minutes, secs = divmod(rest, 60)
            self._uptime_label.configure(text=f"已运行  {hours:02d}:{minutes:02d}:{secs:02d}")
        self._sync_transition_lock()
        self.refresh_state()
        self._render_busy()
        self._later(1000, self._tick)

    def _watch_for_stalls(self) -> None:
        """★ 卡顿看门狗：这一拍比约好的晚了多少，就是界面卡了多久。

        「操作微信的时候界面容易卡」这类反馈**没法在事后复现**——等用户说的时候现场早没了。
        心跳是 1 秒一拍的，只要主线程被谁占住，下一拍就晚多少。把超过阈值的那次记进日志，
        下次再卡就有实证：卡了几秒、前后正在做什么，日志里连着看得见（这条自己也会出现在
        实时日志页上）。**它只报告、不修**——真因得靠这些数据去定位。
        """
        now = time.monotonic()
        previous, self._last_tick = self._last_tick, now
        self._ticks += 1
        # 头两拍不算：第一拍是在 `__init__` 里跑的，那会儿 `root.mainloop()` 还没开始，
        # 中间还隔着建托盘 + 装配网关（真机 4.5 秒）。那段「迟到」是启动本来的样子，
        # 不是卡顿——照报的话每次启动都会刷一条假警报，真警报就没人信了。
        if previous is None or self._ticks <= 2:
            return
        late = now - previous - 1.0
        if late >= _STALL_WARN_SECONDS:
            _log.warning(
                "界面卡了一下",
                stalled_ms=int(late * 1000),
                service=self.service.state.value,
                page=self._page,
            )

    def _schedule_wechat_probe(self) -> None:
        threading.Thread(target=self._probe_wechat, name="wx-ui-probe", daemon=True).start()
        self._later(_WECHAT_PROBE_SECONDS * 1000, self._schedule_wechat_probe)

    def _probe_wechat(self) -> None:
        if not self._probe_lock.acquire(blocking=False):
            return  # 上一次还没探完，跳过这轮，别堆线程
        try:
            # 首次 import 会拉起 pywinauto 那套 C 扩展；必须和网关装配串行，否则两个线程
            # 同时 LoadLibrary 会卡死在 Windows 加载器锁上（见 heavy_import.py）。
            with HEAVY_IMPORT_LOCK:
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
            return is_log_on_screen(self._page, self.root.state())
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
        self._later(FLUSH_MS, self._pump_logs)

    def _append(self, lines: list[logline.LogLine]) -> None:
        self._table.append(lines)
