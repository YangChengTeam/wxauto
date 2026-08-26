"""主窗口：左侧深色状态栏 + 右侧内容区（实时日志 / 配置中心两页）。

外观按 ``docs/main.png`` 设计稿（早先是 ``docs/wxui.png``）。关窗 = 缩回托盘，不停服务
——托盘程序的常规预期，也避免误关把网关一起带走；真要退出走左下角「退出服务」或托盘菜单。

**登录是一道真门**：没登录之前 ``require_login()`` 会把登录页盖在整个窗口上，日志、
配置都看不到（``login_page.py``）。窗口本身照建不误——服务可能早就在跑了
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

from . import dialog, logline, theme, widgets
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
WINDOW_TITLE = "劳小智微信助手"

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

#: 退出登录时转圈那句话，以及等它结束的节奏。
#: 轮询而不是回调：停机走的是后台线程，而这里只能在 tk 线程上动控件。
LOGOUT_BUSY_TEXT = "正在退出登录，等服务停下来…"
LOGOUT_POLL_MS = 200
#: 等不到也得放人走——**退不出去比多等几秒严重得多**。停机上限本身是 15s，留点余量。
LOGOUT_TIMEOUT_SECONDS = 20.0

#: 启动多久之后偷偷把参数页建好。要等过启动最忙的那一段，又要赶在用户去点之前。
_SETTINGS_PREBUILD_MS = 3000

#: 默认窗口大小。**这一版特意放大**（用户 2026-08-16「默认窗口宽度大一点」）——
#: 日志表一行信息量不小（时间 / 会话 / 动作 / 结果），窄了就得横向挤，看一眼变成看半天。
DEFAULT_SIZE = (1360, 820)
#: 再小就没法用了：左侧导航 + 日志表至少要这么宽，配置中心那几行至少要这么高。
MIN_SIZE = (900, 560)
#: 距屏幕边缘留白。放大之后必须挡住「比屏幕还大」这种情况——1366×768 的笔记本上，
#: 一个 1360×820 的窗口标题栏会被顶出屏幕，人连拖都拖不回来。
SCREEN_MARGIN = 80

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
#: 「参数设置」→「配置中心」是用户 2026-08-15 定的改法：参数以后由后端下发（``auth.py``），
#: 这一页的职责从「填参数」变成「看这台机器按什么在跑」；监听名单本来就是 AI 端推下来的、
#: 本机改不了，归到配置中心里当一节。
#:
#: ★ 曾经还有一页「计划中心」，2026-08-15 用户裁决删掉：**计划归 AI 端**（几点做什么、
#: 多久跟进一次由 AI 端排，指令经 WS 推下来），桌面端摆一页只会让人以为计划在这儿配。
_PAGE_TITLES = {"log": "实时日志", "debug": "调试会话", "settings": "配置中心"}

#: 标题底下那句话。空 = 这一页不摆副标题。
_PAGE_SUBTITLES = {
    "settings": "管理连接、调度与启动设置",
    "debug": "直接和小智对话，指令会在本机执行",
}

#: 左侧导航选中那根绿条的宽度（像素）。
_NAV_MARK_W = 4


def _next_read_text(snapshot: Any, running: bool) -> str:
    """标题栏上「下次读消息」那一格。**说不准就别说**，宁可留白也不给个会骗人的数。"""
    if not running:
        return ""
    if snapshot.quiet_hours:
        return "不在工作时间窗，暂不读"
    seconds = snapshot.next_read_seconds
    if seconds is None:
        return "正在读消息…"
    minutes, secs = divmod(int(seconds), 60)
    return f"下次读消息 {minutes:02d}:{secs:02d}"


def _initials(name: str) -> str:
    """头像圈里写什么：英文名取前两位，中文名取后两位（姓氏重的多，名字才分得开）。"""
    text = name.strip()
    if not text:
        return "?"
    return text[:2].upper() if text.isascii() else text[-2:]


def initial_geometry(screen_width: int, screen_height: int) -> str:
    """开窗多大、摆在哪。**按屏幕算，不写死**。

    写死的坏处不是「不好看」，是**打不开**：默认值一旦比屏幕大，标题栏会被顶到屏幕外，
    窗口既拖不动也缩不了。所以先按 `DEFAULT_SIZE` 要，要不到就退到屏幕减两边留白，
    但不小于 `MIN_SIZE`（比这更小界面自己就挤坏了，宁可让它超出屏幕）。

    顺手居中：原来不给位置，Windows 按级联规则往左上角摆，第二次打开还会往右下挪一格。
    """
    width = max(MIN_SIZE[0], min(DEFAULT_SIZE[0], screen_width - SCREEN_MARGIN * 2))
    height = max(MIN_SIZE[1], min(DEFAULT_SIZE[1], screen_height - SCREEN_MARGIN * 2))
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    return f"{width}x{height}+{x}+{y}"


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
        log_pipeline: Any = None,
        probe_wechat: bool = True,
        on_wechat_probe: Callable[[dict[str, Any] | None, BaseException | None], None]
        | None = None,
    ) -> None:
        self.root = root
        self.buffer = buffer
        self.service = service
        self.data_dir = Path(data_dir)
        self._settings_saved_callback = on_settings_saved
        self._log_pipeline = log_pipeline
        self._dry_run_notice = dry_run_notice
        self._unsubscribe: Any = None
        self._wechat_status = ("未检测", theme.GRAY)
        # 探测结果除了摆在侧栏，还要上报给业务端（契约 §8）。窗口自己不发请求——
        # 它只把「探到了什么」原样交出去，发不发、怎么去重是 status_reporter 的事。
        self._on_wechat_probe = on_wechat_probe
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
        self._debug_page: Any = None   # 调试会话（`debug_page.py`），进那一页才建
        self._debug_actions: Any = None  # 它在标题栏右边那一组（● AI 已连接）
        self._watch_card: Any = None  # 监听名单那张卡片，跟着配置中心一起建
        # 名单那一节的说明标签，同上。**必须在这儿先占个 None**：`_note_watch` 会在
        # 它还没建出来时被问到，而 tk 回调里的 AttributeError 是**看不见的**（没有控制台），
        # 现场表现就是「点了没反应」（2026-08-17 那次的形状）。
        self._watch_hint: Any = None
        #: 正在请 AI 端移出的会话名 → 那次请求的 Future（tk 线程读，别在别处改）。
        self._watch_removing: dict[str, Any] = {}
        #: 「同步名单」那一次请求的 Future；None = 现在没有在同步的。
        self._watch_syncing: Any = None
        #: 名单那一节要临时顶出来的一句话 `(文本, 颜色, 到期时刻)`；到期时刻 None = 不自己消失。
        #: 不存进 `_watch_hint` 是因为那个标签每秒被 `_render_watch_page` 重写一次——
        #: 直接写上去的话，说的话活不过一拍（老的 `_sync_watchlist` 就是这个毛病）。
        self._watch_note: tuple[str, str, float | None] | None = None
        #: 091 · 会话名 → 那次「接管 / 交回」还没回话的 Future。
        self._mode_pending: dict[str, Any] = {}
        #: 会话名 → 「还没同步给业务端」那一句。本机已经生效了，这句只是说外面还不知道。
        self._mode_notes: dict[str, str] = {}
        self._locked = False  # 转场态：由 _sync_transition_lock() 每拍现场核对，别手动改
        self._last_tick: float | None = None  # 卡顿看门狗的上一拍时刻
        self._ticks = 0  # 心跳计数：头两拍不报卡顿，理由见 _watch_for_stalls
        self._pending_after: set[str] = set()  # 排着的 after 回调，destroy 时要撤掉
        self._login: Any = None  # 登录页盖在最上面时是它，登进来之后是 None
        self._identity: Any = None  # 当前登录的人（auth.Identity）
        self._logout_provider: Any = None  # 退出登录之后拿什么再登一次
        self._logout_callback: Any = None
        self._logout_action: Any = None  # 退出登录时向服务端撤销会话
        self._logging_out = False  # 正在退出登录（转圈中）；防连点，也防重复排定时器
        self._quit_action: Callable[[], None] | None = None  # 点 × 真退出时走它
        self._version = ""

        root.title(WINDOW_TITLE)
        self._apply_window_icon()
        root.geometry(initial_geometry(root.winfo_screenwidth(), root.winfo_screenheight()))
        root.minsize(*MIN_SIZE)
        root.configure(bg=theme.CONTENT_BG)
        root.protocol("WM_DELETE_WINDOW", self.close_requested)

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

        tk.Label(bar, text="微信助手", font=theme.FONT_TITLE, bg=theme.SIDEBAR_BG,
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
        # ★ 这里曾经还有一行「分身」和一个分身级接管总开关（089）。2026-08-26 裁决
        #   「接管是针对候选人的，按钮都要去掉」——整套拆了，改成监听名单里**一人一个**
        #   小按钮（`_watch_row`）。别再往侧栏加回一个能一键停掉全部的开关。
        for key, label in (("service", "服务"), ("gateway", "网关"), ("wechat", "微信"),
                           ("watch", "监听")):
            self._rows[key] = self._status_row(bar, label)

        # 侧栏这一行只报「几个会话」。**具体是谁不铺在这儿**——名字一多就把整条侧栏顶下去，
        # 而且多数时候没人在看。要看名字点左边「监听名单」，和「实时日志 / 参数设置」一个路数。
        self._uptime_label = tk.Label(bar, text="", font=theme.FONT_SMALL,
                                      bg=theme.SIDEBAR_BG, fg=theme.SIDEBAR_MUTED)
        self._uptime_label.pack(anchor="w", padx=28, pady=(22, 0))

        self._divider(bar, pady=(24, 10))

        self._nav: dict[str, tk.Label] = {}
        self._nav_marks: dict[str, tk.Frame] = {}
        for page, text in (("log", "实时日志"), ("debug", "调试会话"), ("settings", "配置中心")):
            # 选中的那一页在**最左边**立一根绿条（`docs/main.png`）。只靠深一档的底色
            # 在这张暗纹背景上几乎看不出来，绿条才是「我在这儿」的那个信号。
            row = tk.Frame(bar, bg=theme.SIDEBAR_BG)
            row.pack(fill=tk.X)
            mark = tk.Frame(row, bg=theme.SIDEBAR_BG, width=_NAV_MARK_W)
            mark.pack(side=tk.LEFT, fill=tk.Y)
            item = tk.Label(row, text=text, font=theme.FONT_BODY, bg=theme.SIDEBAR_BG,
                            fg=theme.SIDEBAR_TEXT, anchor="w",
                            padx=28 - _NAV_MARK_W, pady=8, cursor="hand2")
            item.pack(side=tk.LEFT, fill=tk.X, expand=True)
            item.bind("<Button-1>", lambda _e, p=page: self._show_page(p))
            row.bind("<Button-1>", lambda _e, p=page: self._show_page(p))
            self._nav[page] = item
            self._nav_marks[page] = mark

        # ★ 底部**只有一个**「退出登录」（用户 2026-08-15：「退出登录和退出服务是一个意思」）。
        # 服务本来就只在登录之后才起（见 `__main__`），所以「不再用了」这件事只有一种表达：
        # 退出登录 = 停掉网关 + 回到登录页。摆两个按钮做同一件事，只会让人猜它们差在哪。
        # 真要关掉整个程序走托盘菜单「退出」。
        self._logout_item = tk.Label(bar, text="退出登录", font=theme.FONT_BODY,
                                     bg=theme.SIDEBAR_BG, fg=theme.SIDEBAR_MUTED, cursor="hand2")
        self._logout_item.bind("<Button-1>", lambda _e: self.logout())
        # 悬停变绿：它一直是灰的，和旁边那些**只是标签**的静态文字长得一模一样，
        # 唯一「这是个能点的东西」的信号只有鼠标形状——而手型要移上去才看得见，
        # 等于要先猜对了才有反馈。退出登录本身不轻（停服务 + 撤会话），
        # 更不该让人靠试出来。（正在退出时不再变色：那时候它已经点不动了。）
        self._logout_item.bind("<Enter>", lambda _e: self._hover_logout(True))
        self._logout_item.bind("<Leave>", lambda _e: self._hover_logout(False))

        # 谁登的这台机器
        self._who = tk.Label(bar, text="", font=theme.FONT_SMALL, bg=theme.SIDEBAR_BG,
                             fg=theme.SIDEBAR_MUTED, anchor="w", justify=tk.LEFT)

    def _hover_logout(self, on: bool) -> None:
        """鼠标进/出「退出登录」。正在退出登录时保持灰——点不动的东西不该有悬停反馈。"""
        if self._destroyed:
            return
        color = theme.GREEN_BRIGHT if (on and not self._logging_out) else theme.SIDEBAR_MUTED
        try:
            self._logout_item.configure(fg=color)
        except tk.TclError:
            pass  # 控件已经没了（正在销毁），悬停这点小事不值得炸出来

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
        title_box = tk.Frame(header, bg=theme.CONTENT_BG)
        title_box.pack(side=tk.LEFT)
        self._heading = tk.Label(title_box, text="实时日志", font=theme.FONT_HEADING,
                                 bg=theme.CONTENT_BG, fg=theme.TEXT)
        self._heading.pack(anchor="w")
        # 副标题只有配置中心有（`docs/setting.png`）；没有的页面直接不占位，标题就不会被顶歪
        self._subtitle = tk.Label(title_box, text="", font=theme.FONT_SMALL,
                                  bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        # 日志页的「● 实时更新 · N 行」就是它那一页的副标题：和配置中心一样摆在标题下面
        # 一行（用户 2026-08-15），而不是横着挤在标题右边。
        self._live_row = tk.Frame(title_box, bg=theme.CONTENT_BG)
        self._live_dot = tk.Label(self._live_row, text="●", font=(theme.UI, 8),
                                  bg=theme.CONTENT_BG, fg=theme.GREEN_BRIGHT)
        self._live_dot.pack(side=tk.LEFT, padx=(0, 5))
        self._live_text = tk.Label(self._live_row, text="实时更新", font=theme.FONT_SMALL,
                                   bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._live_text.pack(side=tk.LEFT)

        self._header_actions = tk.Frame(header, bg=theme.CONTENT_BG)
        self._header_actions.pack(side=tk.RIGHT)
        self._action("打开目录", self.open_log_dir)
        self._action("清空", self.clear)

        # 级别过滤。排障时最常问的一句是「有没有报错」，让人自己在几千行里翻不合适。
        self._filter_bar = tk.Frame(header, bg=theme.CONTENT_BG)
        self._filter_bar.pack(side=tk.RIGHT, padx=(0, 18))
        self._filters: dict[str, tk.Label] = {}
        for name, allowed in LEVEL_FILTERS:
            # 边框走 highlight：选中时点亮成绿框，没选中时和底色同色——看不见，但位置不跳
            chip = tk.Label(self._filter_bar, text=name, font=theme.FONT_SMALL,
                            bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED,
                            padx=12, pady=4, cursor="hand2",
                            highlightthickness=1, highlightbackground=theme.CONTENT_BG,
                            highlightcolor=theme.CONTENT_BG, bd=0)
            chip.pack(side=tk.LEFT, padx=4)
            chip.bind("<Button-1>", lambda _e, n=name, a=allowed: self._set_level_filter(n, a))
            self._filters[name] = chip
        self._active_filter = LEVEL_FILTERS[0][0]
        self._render_filter_chips()

        self._pages = tk.Frame(content, bg=theme.CONTENT_BG)
        self._pages.pack(fill=tk.BOTH, expand=True)

        self._log_page = self._build_log_page(self._pages)
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

    def _build_watch_section(self, parent: tk.Misc) -> tk.Frame:
        """监听名单：谁会被自动回复。**配置中心里的一节**，版式照 `docs/setting2.png`。

        名单是「谁会被自动回复」的唯一判据（069 fail-closed），所以必须看得见**具体是谁**。
        名单权威在 AI 端，平时由它主动推下来；「同步名单」（用户 2026-08-15）是手动要一份，
        补的是推送**没送到**的那些时候——推的那一瞬间这台机器正在重连、或者名单是在别处改的。
        """
        page = tk.Frame(parent, bg=theme.CONTENT_BG)

        head = tk.Frame(page, bg=theme.CONTENT_BG)
        head.pack(fill=tk.X, pady=(14, 4))
        tk.Label(head, text="监听名单", font=theme.FONT_HEADING, bg=theme.CONTENT_BG,
                 fg=theme.TEXT).pack(side=tk.LEFT)
        self._watch_count = tk.Label(head, text="", font=theme.FONT_SMALL,
                                     bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._watch_count.pack(side=tk.RIGHT)
        # 下一趟读消息的倒计时（用户 2026-08-15）。**读的是网关真实那一觉**（含抖动、
        # 含快慢档），不是照配置算的——照配置算会一直和现实差几十秒，看着就像坏了。
        self._watch_next = tk.Label(head, text="", font=theme.FONT_SMALL,
                                    bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._watch_next.pack(side=tk.RIGHT, padx=(0, 14))
        # ★ 样式和别处的描边按钮一致（绿框绿字，用户 2026-08-16）。
        widgets.ghost_button(head, "同步名单", self._sync_watchlist).pack(
            side=tk.RIGHT, padx=(0, 14))

        self._watch_hint = tk.Label(page, text="", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                                    fg=theme.TEXT_MUTED, anchor="w", justify=tk.LEFT)
        self._watch_hint.pack(fill=tk.X, pady=(0, 10))

        box = tk.Frame(page, bg=theme.ROW_LINE, padx=1, pady=1)
        box.pack(fill=tk.X, pady=(0, 16))
        self._watch_card = tk.Frame(box, bg=theme.CONTENT_BG)
        self._watch_card.pack(fill=tk.BOTH, expand=True)
        self._watch_shown: tuple | None = None
        return page

    def _sync_watchlist(self) -> None:
        """「同步名单」：**现在就去取一份最新的**。

        名单权威在业务后端（spec 085），平时由网关按周期自己同步。这个按钮补的是
        「等不及下一轮」的时候：名单刚在工作台改过，专员想立刻看到。

        ★ 落地口仍然只有一个（`Watchlist`）——按钮只负责触发和报数，
        不自己解析名单。多解析一处就多一份会漂的实现。
        """
        if self._watch_syncing is not None and not self._watch_syncing.done():
            return  # 上一次还没回话，别排第二条
        try:
            self._watch_syncing = self.service.sync_watched()
        except Exception as exc:  # noqa: BLE001 - 服务没起/连不上都要说人话
            _log.warning("同步监听名单失败", error=str(exc))
            self._note_watch(f"同步不了：{exc}", theme.ORANGE)
            return
        _log.info("请求同步监听名单")
        # 不自己消失：等回话那一刻由 `_poll_watch_sync` 换掉，中间别让人以为点空了。
        self._note_watch("正在向 AI 端要最新名单…", theme.TEXT_MUTED, hold=True)

    def _note_watch(self, text: str, color: str, *, hold: bool = False) -> None:
        """在名单那一节顶出一句话。`hold` = 一直挂着，否则几秒后自己让位给常规说明。"""
        expiry = None if hold else time.monotonic() + 6.0
        self._watch_note = (text, color, expiry)
        if self._watch_hint is not None:
            self._watch_hint.configure(text=text, fg=color)

    def _poll_watch_sync(self) -> None:
        """看看「同步名单」有没有回话。**跑在 tk 线程的心跳里**——后台只放值，这里只读值。"""
        future = self._watch_syncing
        if future is None or not future.done():
            return
        self._watch_syncing = None
        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 - 超时/断线照原样说出来
            _log.warning("同步监听名单失败", error=str(exc))
            self._note_watch(f"同步失败：{exc}", theme.ORANGE)
            return
        watching = int(result.get("watching") or 0) if isinstance(result, dict) else 0
        _log.info("监听名单已同步", 会话数=watching)
        self._watch_shown = None  # 名单可能变了，逼下一拍重画
        if watching:
            self._note_watch(f"已同步：现在盯着 {watching} 个会话。", theme.TEXT_MUTED)
        else:
            # 空名单不是「同步没成功」，是「名单里真的一个都没有」——两句话必须分得开。
            self._note_watch("已同步：名单是空的——一条消息都不会回。", theme.ORANGE)

    # ── 候选人级人工接管（091 / docs/19 v2 §1） ──────────────────

    def _toggle_candidate_mode(self, name: str) -> None:
        """按名单里某个人后面那个小按钮：接管 ⇄ 交回分身。

        **先落本机、界面立刻变**，再异步去同步业务端——接管的意思是「现在立刻别再发给他了」，
        它必须在断网、业务端 500 的时候照样成立。

        ★ 开接管要先问一句并要一行原因：`reason` 是业务端 `human_takeover` 的**必填项**
          （v1.5 §4.1）。**交回分身不问**——那是把状态交回默认值，多一道确认只会让
          「我要它干活」比「让它停下」还费劲。
        ★ 弹框的 parent 必须是**真控件**（`self.root`）：`MainWindow` 不是 tk 控件，
          而 tk 回调里的 AttributeError 是**看不见的**，现场表现就是「点了没反应」
          （[[tk-silent-callback-errors]]）。
        """
        if name in self._mode_pending:
            return  # 这个人上一次还没回话，别排第二条
        taken_over = self.service.status().candidate_modes.get(name) == "human_takeover"
        reason = ""
        if not taken_over:
            answer = dialog.ask_text(
                self.root, "人工接管",
                f"接管「{name}」之后，他就不再被自动回复，改由你本人跟进。\n\n"
                "其他人不受影响；他发来的消息照常收着。\n"
                "请写一句原因（主管在企业端看得到）：",
                prompt="我本人跟进这个候选人",
                confirm="接管", cancel="取消",
            )
            if answer is None:
                return
            reason = answer or "招聘老师在客户端上接管"
        mode = "ai_managed" if taken_over else "human_takeover"
        _log.info("按下候选人接管按钮", mode=mode)
        try:
            self._mode_pending[name] = self.service.set_candidate_mode(name, mode, reason=reason)
        except Exception as exc:  # noqa: BLE001 - 服务没起/连不上都要说人话
            _log.warning("改候选人接管状态失败", error=str(exc))
            dialog.tell(self.root, "没能改", str(exc), tone="error")
            return
        self._mode_notes.pop(name, None)
        self._watch_shown = None  # 逼下一拍重画，让这一行立刻显示新档位

    def _poll_candidate_modes(self) -> None:
        """看看那几次「接管 / 交回」有没有回话。**跑在 tk 线程的心跳里**——后台只放值。"""
        for name, future in list(self._mode_pending.items()):
            if not future.done():
                continue
            self._mode_pending.pop(name, None)
            self._watch_shown = None
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - 超时/断线照原样说出来
                # ★ **不回滚**：落本机那一步早在网关线程上做完了，这个人此刻已经按新状态在跑。
                #   这里只多标一句「未同步」（说了没送到 ≠ 没说）。
                _log.warning("候选人接管状态没同步出去", error=str(exc))
                self._mode_notes[name] = "还没同步给业务端，稍后重试"
                continue
            note = str(result.get("note") or "") if isinstance(result, dict) else ""
            if note:
                self._mode_notes[name] = note
            else:
                self._mode_notes.pop(name, None)

    def _ask_remove_watched(self, name: str) -> None:
        """「移出」：先问一句，再请 AI 端删。

        ★ 问的这一句要说清后果和归属：删掉之后**他再来消息不会被自动回复**；名单的权威
        在 AI 端，所以这里是「请 AI 端删」，删完新名单推回来、这一行才会消失——
        不在本机先把行删掉装成功（那样下一次推送又会把它带回来，看起来像「删不掉」）。

        ★ 弹框的 parent 必须是**真控件**（`self.root`）：`MainWindow` 不是 tk 控件，
        传 `self` 会在 `winfo_toplevel()` 上抛 AttributeError——而 tk 回调里的异常在
        窗口化程序里是**看不见的**（没有控制台），现场表现就是「点了没反应」
        （2026-08-17 用户反馈，v0.1.14 真机）。
        """
        if name in self._watch_removing:
            return  # 上一次还没有回话，别排第二条
        _log.info("请求移出监听名单", 会话=name)
        if not dialog.ask(
            self.root, "移出监听名单",
            f"移出「{name}」之后，他再发消息就不会被自动回复了。\n\n"
            "名单由 AI 端保管，这里会请 AI 端删掉；删完新名单会自动同步回这台电脑。",
            confirm="移出", cancel="取消",
        ):
            return
        try:
            future = self.service.remove_watched(name)
        except Exception as exc:  # noqa: BLE001 - 服务没起/连不上都要说人话
            _log.warning("移出监听名单失败", 会话=name, error=str(exc))
            dialog.tell(self.root, "没能移出", str(exc), tone="error")
            return
        self._watch_removing[name] = future
        self._watch_shown = None  # 逼下一拍重画，让这一行显示「正在移出…」

    def _poll_watch_removals(self) -> None:
        """看看那几条「移出」有没有回话。**跑在 tk 线程的心跳里**——后台只放值，这里只读值。"""
        for name, future in list(self._watch_removing.items()):
            if not future.done():
                continue
            del self._watch_removing[name]
            self._watch_shown = None
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - 超时/断线照原样说出来
                _log.warning("移出监听名单失败", 会话=name, error=str(exc))
                dialog.tell(self.root, "没能移出", f"{name}：{exc}", tone="error")
                continue
            removed = list(result.get("removed") or []) if isinstance(result, dict) else []
            if not removed:
                # AI 端说它本来就不在名单上。**别当成删成功**——那会让人以为改过了。
                _log.info("移出监听名单：AI 端说本来就不在名单里", 会话=name)
                dialog.tell(self.root, "没有变化",
                            f"AI 端说「{name}」本来就不在自动回复名单里。")
            else:
                _log.info("已移出监听名单", 会话=name, 删掉=removed)

    def _render_watch_page(self) -> None:
        """把名单铺到页面上。停在配置中心时每秒刷一次——名单是 AI 端随时会改的。"""
        if self._page != "settings" or self._watch_card is None:
            return
        snapshot = self.service.status()
        watched = tuple(snapshot.watched)
        running = self.service.state is ServiceState.RUNNING
        self._watch_next.configure(text=_next_read_text(snapshot, running))
        if not running:
            self._watch_count.configure(text="未启动")
            hint = ("服务没在跑，名单由 AI 端在运行时推下来。", theme.TEXT_MUTED)
        elif watched:
            self._watch_count.configure(text=f"{len(watched)} 个会话")
            hint = ("名单内会话将自动回复，未列入名单的会话不回复。", theme.TEXT_MUTED)
        else:
            self._watch_count.configure(text="0 个会话")
            hint = ("名单是空的——这意味着一条消息都不会回。", theme.ORANGE)
        # 「同步名单」刚说过的话优先，且这一段每秒都会走一遍——所以那句话必须存在
        # `_watch_note` 里、每拍重新贴上去，直接 configure 一次是活不过一拍的。
        if self._watch_note is not None:
            text, color, expiry = self._watch_note
            if expiry is not None and time.monotonic() >= expiry:
                self._watch_note = None
            else:
                hint = (text, color)
        self._watch_hint.configure(text=hint[0], fg=hint[1])

        # ★ 重画的判据要把**档位**也算进去（091）：只看名字的话，按下接管之后
        #   那一行会一直画着旧样子，直到名单本身变了为止。
        shown = (watched, tuple(sorted(snapshot.candidate_modes.items())),
                 tuple(sorted(self._mode_pending)), tuple(sorted(self._mode_notes.items())))
        if shown == self._watch_shown:
            return  # 没变就别重画：每秒重建一次控件既费又会把滚动位置抖没
        self._watch_shown = shown
        for child in self._watch_card.winfo_children():
            child.destroy()
        if not watched:
            tk.Label(self._watch_card, text="（空）" if running else "（未启动）",
                     font=theme.FONT_BODY, bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED,
                     anchor="w").pack(fill=tk.X, padx=18, pady=14)
            return
        modes = dict(snapshot.candidate_modes)
        for index, name in enumerate(watched):
            self._watch_row(index, name, modes.get(name))

    def _watch_row(self, index: int, name: str, mode: str | None = None) -> None:
        if index:
            tk.Frame(self._watch_card, bg=theme.ROW_LINE, height=1).pack(fill=tk.X)
        row = tk.Frame(self._watch_card, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X)

        avatar = tk.Canvas(row, width=34, height=34, bg=theme.CONTENT_BG,
                           highlightthickness=0, bd=0)
        avatar.pack(side=tk.LEFT, padx=(16, 12), pady=12)
        avatar.create_oval(1, 1, 33, 33, fill=theme.PILL_BG, outline=theme.PILL_BG)
        avatar.create_text(17, 17, text=_initials(name), font=theme.FONT_SMALL,
                           fill=theme.TEXT_MUTED)

        tk.Label(row, text=name, font=theme.FONT_BODY, bg=theme.CONTENT_BG,
                 fg=theme.TEXT).pack(side=tk.LEFT)
        tk.Label(row, text="微信会话", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                 fg=theme.TEXT_MUTED).pack(side=tk.LEFT, padx=(14, 0))

        if name in self._watch_removing:
            # 请求还在路上。**不提前把这一行抹掉**——AI 端还没说删掉了，抹了就是假装。
            tk.Label(row, text="正在移出…", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                     fg=theme.ORANGE).pack(side=tk.RIGHT, padx=(0, 16))
            return

        # ★「移出」放在最右：名单是白名单，删掉 = 从此不再自动回他，是有后果的动作，
        # 所以点了先问一句（`_ask_remove_watched`），而且**删在 AI 端**——本机删只能撑到
        # 下一次推送（名单推一次覆盖一次），删了立刻又回来，那是假的删除。
        remove = tk.Label(row, text="移出", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                          fg=theme.TEXT_MUTED, cursor="hand2")
        remove.pack(side=tk.RIGHT, padx=(0, 16))
        remove.bind("<Button-1>", lambda _e, n=name: self._ask_remove_watched(n))
        remove.bind("<Enter>", lambda _e, w=remove: w.configure(fg=theme.RED))
        remove.bind("<Leave>", lambda _e, w=remove: w.configure(fg=theme.TEXT_MUTED))

        # ★★ 091 · 一人一个小按钮：接管 ⇄ 交回分身。放在「移出」左边——它是可逆的日常动作，
        #   而「移出」是从此不再自动回他。两者挨着，但轻重不同，颜色也不同。
        #
        # ★ `mode is None` = **不知道**这个人现在哪一档（本机没记录，同步名单也还没带回来：
        #   业务端还没把 `aiRecruitment` 加进 `GET /client/contacts`）。这时按钮照样能按，
        #   但**不谎报当前档**——右边那句状态写「监听中」，不写「AI 托管中」。
        taken_over = mode == "human_takeover"
        if name in self._mode_pending:
            tk.Label(row, text="正在切换…", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                     fg=theme.ORANGE).pack(side=tk.RIGHT, padx=(0, 16))
        else:
            button = tk.Label(row, text="交回分身" if taken_over else "我来接管",
                              font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                              fg=theme.TEXT_MUTED, cursor="hand2")
            button.pack(side=tk.RIGHT, padx=(0, 16))
            button.bind("<Button-1>", lambda _e, n=name: self._toggle_candidate_mode(n))
            button.bind("<Enter>", lambda _e, w=button: w.configure(fg=theme.TEXT))
            button.bind("<Leave>", lambda _e, w=button: w.configure(fg=theme.TEXT_MUTED))

        note = self._mode_notes.get(name, "")
        if note:
            tk.Label(row, text=note, font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                     fg=theme.ORANGE).pack(side=tk.RIGHT, padx=(0, 16))

        if taken_over:
            # 接管中常驻一行橙的：它是**例外状态**，一眼要看得出来「这个人分身不管了」。
            tk.Label(row, text="人工接管中", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                     fg=theme.ORANGE).pack(side=tk.RIGHT, padx=(0, 16))
            tk.Label(row, text="●", font=(theme.UI, 8), bg=theme.CONTENT_BG,
                     fg=theme.ORANGE).pack(side=tk.RIGHT, padx=(0, 5))
            return
        tk.Label(row, text="AI 托管中" if mode else "监听中", font=theme.FONT_SMALL,
                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack(side=tk.RIGHT, padx=(0, 16))
        tk.Label(row, text="●", font=(theme.UI, 8), bg=theme.CONTENT_BG,
                 fg=theme.GREEN_BRIGHT).pack(side=tk.RIGHT, padx=(0, 5))

    # ── 页面切换 ──────────────────────────────────────────────────

    def _show_page(self, page: str) -> None:
        if page == "settings" and self._locked:
            return  # 转场态不给进参数页，见 _build_overlay
        if page not in _PAGE_TITLES:
            page = "log"  # 页删掉了（比如原来的计划中心），退回日志页，别把内容区留成一片白
        self._page = page
        for child in self._pages.winfo_children():
            child.pack_forget()
        self._render_nav()
        self._heading.configure(text=_PAGE_TITLES.get(page, _PAGE_TITLES["log"]))
        subtitle = _PAGE_SUBTITLES.get(page, "")
        if subtitle:
            self._subtitle.configure(text=subtitle)
            self._subtitle.pack(anchor="w", pady=(2, 0))
        else:
            self._subtitle.pack_forget()

        # 「实时更新 · N 行」「级别过滤」「清空 / 打开目录」这一排只属于日志页
        for widget in self._header_actions.winfo_children():
            widget.pack_forget()  # 每页自己摆自己的那几个，先一律收掉
        if page == "log":
            self._live_row.pack(anchor="w", pady=(2, 0))
            for widget in self._header_actions.winfo_children():
                if widget is not self._debug_actions:
                    widget.pack(side=tk.RIGHT, padx=10)
            self._filter_bar.pack(side=tk.RIGHT, padx=(0, 18))
            self._log_page.pack(fill=tk.BOTH, expand=True)
        else:
            self._live_row.pack_forget()
            self._filter_bar.pack_forget()
            if page == "debug":
                self._ensure_debug_page().pack(fill=tk.BOTH, expand=True)
                self._debug_actions.pack(side=tk.RIGHT)
            else:
                self._ensure_settings_page().pack(fill=tk.BOTH, expand=True)
                self._render_watch_page()
        self._render_overlay()  # 每个分支都要摆一次：切走了就得把遮罩收掉

    def _prebuild_settings_page(self) -> None:
        """空闲时把配置中心建出来（不显示）。失败无所谓——真点的时候还会再建一次。"""
        if self._destroyed or self._config_page is not None:
            return
        try:
            self._ensure_settings_page()
        except tk.TclError:
            pass

    def _ensure_debug_page(self) -> Any:
        """调试会话只建一次。**进页不重置**——上一轮说了什么正是要回看的东西。

        和配置中心一样惰性建：它 import 的 `debug_page` 会连带把 `gateway.debugchat` 拉进来，
        不进这一页就不该为它付这份钱。
        """
        if self._debug_page is None:
            from .debug_page import DebugPage

            self._debug_page = DebugPage(self._pages, self.service,
                                         on_open_log=lambda: self._show_page("log"))
            # 指令回执要能找回这一页（回调跑在网关线程上，页面那头只入队）
            self.service.set_command_listener(self._debug_page.on_command_result)
            self._debug_actions = self._debug_page.build_header_actions(self._header_actions)
        return self._debug_page

    def _ensure_settings_page(self) -> Any:
        """设置页只建一次，之后每次进页重读磁盘上的值。

        早先是每次切页 ``SettingsPage(...)`` 新建一个、旧的只 ``pack_forget()``——控件一直
        挂在 ``_pages`` 底下不销毁，来回切几十次就攒几十份。改销毁也不行：保存那条路径是
        ``_save() → on_saved() → …``，回到 ``_save()`` 还要拿自己当父控件弹提示框，
        父控件已经没了会直接抛 TclError。复用 + reload 两头都躲开。

        ``SettingsPage`` 的 import 仍然留在这儿：它是整页控件里最重的一份，
        启动时不进设置页就不该为它付这份钱。
        """
        if self._config_page is None:
            from .settings_page import SettingsPage

            self._config_page = tk.Frame(self._pages, bg=theme.CONTENT_BG)
            scroller, inner = self._build_scroller(self._config_page)
            self._settings_page = SettingsPage(
                inner, self.data_dir, on_saved=self._on_settings_saved,
            )
            # ★ 动作条**先** pack（side=BOTTOM）再 pack 滚动区：反过来的话 expand 的滚动区
            # 会把它挤出可视范围，参数一多就得先滚到底才找得到「保存」。
            self._settings_page.build_action_bar(self._config_page).pack(side=tk.BOTTOM, fill=tk.X)
            scroller.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
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
        """转场态里「配置中心」变灰、鼠标不再变手型——点不动这件事要看得出来。"""
        for key, item in self._nav.items():
            disabled = self._locked and key == "settings"
            active = key == self._page and not disabled
            row_bg = theme.SIDEBAR_ACTIVE_BG if active else theme.SIDEBAR_BG
            item.configure(
                bg=row_bg,
                fg=theme.SIDEBAR_LINE if disabled
                else (theme.SIDEBAR_TITLE if active else theme.SIDEBAR_TEXT),
                cursor="" if disabled else "hand2",
            )
            item.master.configure(bg=row_bg)
            self._nav_marks[key].configure(bg=theme.GREEN_BRIGHT if active else row_bg)

    def _render_overlay(self) -> None:
        if self._locked and self._page == "settings":
            self._overlay_label.configure(text=self._busy_text or "正在切换服务状态，请稍候")
            self._overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._overlay.lift()
        else:
            self._overlay.place_forget()

    def _on_settings_saved(self, restart: bool) -> None:
        """保存完**留在配置中心**（用户 2026-08-15）。

        原来会跳到实时日志页——想法是「重启的过程在日志里看」，但代价是人被踹出正在改的
        那一页：接着想改第二项就得自己再点回来，改完还要确认刚填的存没存住。重启进度左侧
        状态栏本来就在报（正在启动→运行中），想看日志点一下就到，不必替人做这个决定。
        """
        if self._settings_saved_callback is not None:
            self._settings_saved_callback(restart)

    # ── 登录门 ────────────────────────────────────────────────────

    def require_login(self, provider: Any, *, version: str = "",
                      on_authenticated: Callable[[Any], None] | None = None,
                      allow_autologin: bool = True) -> None:
        """把登录页盖在整个窗口上。登进来之前，日志和配置一概看不到。

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
                                on_authenticated=done, version=version,
                                allow_autologin=allow_autologin)
        self._login.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._login.lift()

    def finish_login(self, identity: Any) -> None:
        self._identity = identity
        if self._login is not None:
            self._login.destroy()
            self._login = None
        self._render_identity()
        self._show_page("log")

    def logout(self) -> None:
        """退出登录 = **停掉服务** + 回到登录页（用户 2026-08-15 把两件事合成了一件）。

        整个过程是异步的，但**要等它真结束了再把登录页放出来**（用户 2026-08-16）：
        停机要等网关线程收口（最长 15s），撤销服务端会话是一次网络调用。原来点完立刻就
        切页，于是人已经在登录页上了，后台还在停机、还在撤会话——这时候重新登进去，撞上
        的是一个正在关的服务。现在点完先转圈说「正在退出登录…」，两件事都落地再切页。

        **在 tk 线程上等是不行的**：那十几秒里窗口一次都不重绘，看起来就是点完卡死了。
        所以用定时器轮询，界面照样能动。
        """
        if self._login is not None or self._logout_provider is None:
            return
        if self._logging_out:
            return  # 连点两下不该走两遍
        self._logging_out = True

        # 撤销服务端那边的会话（契约 §6）。**丢到后台线程**：它是一次网络调用。
        revoked = threading.Event()
        if self._logout_action is not None:
            threading.Thread(target=self._run_logout_action, args=(revoked,),
                             name="wx-ui-logout", daemon=True).start()
        else:
            revoked.set()

        self._identity = None
        self._render_identity()
        # ★ 这句提示这次**有人负责清掉**（`_finish_logout`），所以敢显式设。
        # 2026-08-15 踩过反面：设了却没有「完事」的回调，于是人都退回登录页了，
        # 侧栏还在转圈说「正在停止服务，请稍候」。
        self.set_busy(LOGOUT_BUSY_TEXT)
        if self.service.state in {ServiceState.RUNNING, ServiceState.STARTING}:
            self.service.stop_async()
        self._await_logout(revoked, time.monotonic() + LOGOUT_TIMEOUT_SECONDS)

    def _run_logout_action(self, revoked: threading.Event) -> None:
        try:
            self._logout_action()
        except Exception as exc:  # noqa: BLE001 - 撤不掉也得让人退出去
            _log.warning("撤销客户端会话失败", error=str(exc)[:120])
        finally:
            revoked.set()

    def _await_logout(self, revoked: threading.Event, deadline: float) -> None:
        """等「服务停了」和「会话撤了」都落地，再把登录页放出来。

        **超时也要放人走**：退不出去比多等几秒严重得多。等不到的那几种情况
        （网关卡在收口、业务端连不上）不该把人锁在一个已经登出的界面里。
        """
        service_settled = self.service.state not in {
            ServiceState.RUNNING, ServiceState.STARTING, ServiceState.STOPPING,
        }
        if (revoked.is_set() and service_settled) or time.monotonic() >= deadline:
            self._finish_logout()
            return
        self._later(LOGOUT_POLL_MS, lambda: self._await_logout(revoked, deadline))

    def _finish_logout(self) -> None:
        self._logging_out = False
        self.set_busy("")
        # ★ `allow_autologin=False`：这是人**主动**退的，别让登录页拿记住的密码又登回去。
        # 「第一次点退出登录又弹回主界面」就是这么来的，见 `login_page._maybe_autologin`。
        self.require_login(self._logout_provider, version=self._version,
                           on_authenticated=self._logout_callback,
                           allow_autologin=False)

    def set_login_context(self, provider: Any, *, version: str = "",
                          on_authenticated: Callable[[Any], None] | None = None,
                          on_logout: Callable[[], None] | None = None) -> None:
        """记下「退出登录之后拿什么再登一次」，以及退出时要向服务端做什么。
        require_login 之前调。"""
        self._logout_provider = provider
        self._logout_callback = on_authenticated
        self._logout_action = on_logout
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

    # ── 显示 / 隐藏 ───────────────────────────────────────────────

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()

    def set_quit_action(self, action: Callable[[], None] | None) -> None:
        """点 × 要真退出时走什么。由 `ui/__main__.py` 接到和托盘「退出」同一条收尾上。"""
        self._quit_action = action

    def close_requested(self) -> None:
        """点窗口右上角的 ×。**登没登录，这一下的意思是不一样的。**

        * 登进来了 → 缩回托盘。网关正替人干活，关窗口只是不想一直看着它。
        * 还在登录页 → **真退出**（用户 2026-08-16）。这时候什么都没在跑，缩进托盘只剩
          一个点开还是让你登录的图标：人以为关掉了，其实进程还在，开机自启下次还会再来
          一个。「关掉一个什么都不做的窗口」应该就是关掉它。

        退出走的是和托盘「退出」同一条路（`__main__.shutdown`）——该送的下线状态照送，
        网关该收的尾照收，不是把窗口一销了事。
        """
        if self.logged_in:
            self.hide()
            return
        if self._quit_action is None:
            # 没接退出动作（单测里的窗口就是这样）。**宁可什么都不做也别偷偷缩起来**：
            # 缩起来会让「点了 × 却没退出」变回一个没人看得见的 bug，而这里不响至少
            # 是当场就能发现的。真机上接没接由 tests/test_ui.py 的一条源码断言看着。
            _log.warning("登录页上点了关闭，但没有接退出动作")
            return
        self._quit_action()

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

    @property
    def log_dir(self) -> Path:
        """日志目录：一天一个文件（``gateway-2026-08-15.log``，见 ``logging_config``）。"""
        return self.data_dir / "logs"

    def open_log_dir(self) -> None:
        """日志页右上角「打开目录」：在资源管理器里打开日志目录。

        不直接开某一个文件（用户 2026-08-15）：日志按天分文件，要看的往往不是今天这份——
        「上周三那单怎么回事」得自己挑日期。开目录把选择权留给人，也顺手让人看见一共存了几天。
        """
        import subprocess
        import sys

        self.log_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(self.log_dir)])

    def open_log(self) -> None:
        """托盘「实时日志」：窗口露面，**并且停在日志页**。

        只 deiconify 是不够的——上次要是停在配置中心，点「实时日志」弹出来的还是配置中心，
        看起来就是菜单点了不算数。
        """
        self.show()
        self._show_page("log")

    def open_debug(self) -> None:
        """托盘「调试会话」：窗口露面，并且停在调试会话页。"""
        self.show()
        self._show_page("debug")

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
        # ★ 这一格**不再显示积压**（原来是「已连接 · 欠 N 批」）。用户 2026-08-21：
        #   「欠这个意思也不懂」——「欠」是记账口径不是人话，而且这格只有几个字的地方，
        #   说不清「读到了没回出去」和「消息丢了」的区别。改成积压一变就往实时日志写
        #   整整一行（`GatewayService._report_backlog` + `logging_config` 里那段话），
        #   070 那个「微信显示已读、可是没人回」的知情需求由那一行满足，信息只多不少。
        if state is not ServiceState.RUNNING:
            self._rows["gateway"].configure(text="未启动", fg=theme.GRAY)
        elif connected <= 0:
            # 连不上和还没连上是两回事：前者要红着让人去看日志，后者几秒后自己会变绿。
            failed = snapshot.connect_error
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
        self._poll_candidate_modes()
        self._poll_watch_removals()
        self._poll_watch_sync()
        self._render_watch_page()  # 停在配置中心时跟着刷；不在那页是个空操作

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
            self._report_probe(probe, None)
            if probe.get("logged_in"):
                status = ("已登录", theme.GREEN)
            elif probe.get("login_window"):
                status = ("待扫码", theme.ORANGE)
            else:
                # 不截断：`reason` 那几句是**照这格的宽度写**的（见
                # `uia_probe.probe.STATUS_CELL_BUDGET`）。截断出来的半句话比不显示更糟
                # ——「UIA 探测失败：Z:\」就是这么来的。
                status = (str(probe.get("reason") or "未登录"), theme.GRAY)
        except Exception as exc:  # noqa: BLE001 - 探测失败只影响这一格显示
            # 连 probe 都没进去（import 就炸了）：这里同样不摆异常原文，只说状态。
            _log.warning("微信探测线程异常", error_type=type(exc).__name__,
                         error=str(exc)[:300])
            status = ("微信读取异常", theme.GRAY)
            self._report_probe(None, exc)
        finally:
            self._probe_lock.release()
        self._wechat_status = status

    def _report_probe(self, probe: dict[str, Any] | None, error: BaseException | None) -> None:
        """把探测结果交给上报层。**它自己出错不能影响探测线程**——上报失败最多是企业端
        那一格显示旧值，探测线程死了才是这一格永远不再更新。"""
        if self._on_wechat_probe is None:
            return
        try:
            self._on_wechat_probe(probe, error)
        except Exception as exc:  # noqa: BLE001
            _log.debug("上报微信状态失败", error=str(exc)[:120])

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
        """选中的那档描一圈绿边、字也变绿（`docs/main.png`）；其余就是一行灰字。"""
        for name, chip in self._filters.items():
            active = name == self._active_filter
            edge = theme.GREEN if active else theme.CONTENT_BG
            chip.configure(bg=theme.CONTENT_BG, fg=theme.GREEN if active else theme.TEXT_MUTED,
                           highlightbackground=edge, highlightcolor=edge)

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
