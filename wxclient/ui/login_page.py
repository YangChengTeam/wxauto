"""登录页（按 ``docs/login.png``）：左边深色品牌面板，右边白色表单。

**这是一道真门，不是欢迎页**：登录之前看不到日志、配置和计划，参数也还没拉下来
（谁能用这台机器 = 谁的参数，见 ``auth.py``）。

两件必须做对的事：

1. **登录动作在后台线程上跑。** 它要发网络请求（最长 8 秒）；放在 tk 线程上就是
   「点了登录，窗口白一片」——桌面端在别处已经为这个栽过跟头（见 ``ui/__main__.py``
   的 shutdown 说明）。这里同样是主线程只管立刻能看见的部分，结果 ``after`` 回来。
2. **失败要说人话且留在原地。** 密码打错不该弹个模态框再把输入框清空，错误就贴在按钮
   上方，账号还在，改一下再来。

首次运行（本机模式且还没设过账号）会切成「设置管理员账号」：多一个确认密码框，
按钮变成「设置并进入」。不给「跳过」——留后门等于没有这道门。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import auth, theme
from .auth import AuthError, AuthProvider, Identity

#: 左侧品牌面板占窗口宽度的比例。设计稿是 790/1570 ≈ 0.5。
PANEL_RATIO = 0.5

#: 改完窗口大小多久才重铺背景。铺一次要按新尺寸重采样整张图（几十毫秒），
#: 拖动窗口边框时每一像素都铺一次会卡；等手停下来再铺。
_BG_REDRAW_MS = 90

HERO_PARTS = (("让", theme.SIDEBAR_TITLE), ("微信服务", theme.GREEN_DEEP), ("稳定在线", theme.SIDEBAR_TITLE))
HERO_SUB = "连接、监听与消息处理，一处完成"
BRAND_LINE = "劳小智微信服务"


class LoginPage(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        provider: AuthProvider,
        *,
        data_dir: Path,
        on_authenticated: Callable[[Identity], None],
        version: str = "",
    ) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self._provider = provider
        self._data_dir = Path(data_dir)
        self._on_authenticated = on_authenticated
        self._version = version
        self._busy = False
        self._photo: tk.PhotoImage | None = None
        self._setup_mode = self._needs_setup()

        self._build_panel()
        self._build_form()

    def _needs_setup(self) -> bool:
        try:
            return bool(self._provider.needs_setup())
        except Exception:  # noqa: BLE001 - provider 坏了也得让登录页画出来
            return False

    # ── 左：品牌面板 ─────────────────────────────────────────────

    def _build_panel(self) -> None:
        canvas = tk.Canvas(self, bg=theme.PANEL_BG, highlightthickness=0, bd=0)
        canvas.place(relx=0, rely=0, relwidth=PANEL_RATIO, relheight=1)
        self._canvas = canvas
        self._bg_size = (0, 0)
        self._bg_job: str | None = None
        canvas.bind("<Configure>", self._on_panel_resize)

    def _on_panel_resize(self, event: Any) -> None:
        self._render_panel_text(event)  # 字立刻跟上，图慢一拍无所谓
        size = (event.width, event.height)
        if size == self._bg_size:
            return
        self._bg_size = size
        if self._bg_job is not None:
            try:
                self.after_cancel(self._bg_job)
            except tk.TclError:
                pass
        self._bg_job = self.after(_BG_REDRAW_MS, self._render_background)

    def _render_background(self) -> None:
        """把底图按 cover 铺满左栏（用户 2026-08-15：「应使用 background-size: cover」）。

        原来是贴一张预先切好的图在左上角：面板宽高比和图对不上，于是能看到一条明显的
        接缝，构图也只露出一角。现在按当前面板尺寸等比放大 + 居中裁剪，改窗口大小也跟着变。
        """
        self._bg_job = None
        from . import icons

        width, height = self._bg_size
        photo = icons.cover_photo(self._canvas, icons.PANEL_BG_PATH, width, height)
        if photo is None:
            return  # 铺不上就是一块纯色面板，字照样看得清
        self._photo = photo  # 必须自己留引用，否则被 GC 掉就成了一块空白
        self._canvas.delete("bg")
        self._canvas.create_image(0, 0, anchor="nw", image=photo, tags="bg")
        self._canvas.tag_lower("bg")  # 图垫底，字压在上面

    def _render_panel_text(self, event: Any = None) -> None:
        """面板上的字跟着面板尺寸重排。**每次都重画，不做增量**——就四行字。"""
        canvas = self._canvas
        canvas.delete("text")
        width = canvas.winfo_width() if event is None else event.width
        height = canvas.winfo_height() if event is None else event.height
        if width <= 1 or height <= 1:
            return
        left = max(28, int(width * 0.12))

        canvas.create_text(left, int(height * 0.36), anchor="w", text=BRAND_LINE,
                           fill=theme.SIDEBAR_TEXT, font=theme.FONT_HEADING, tags="text")

        # 三段不同颜色拼一行大标题：canvas 没有富文本，只能量着宽度一段段摆。
        import tkinter.font as tkfont

        hero_font = tkfont.Font(font=theme.FONT_HERO)
        x = left
        y = int(height * 0.46)
        for part, color in HERO_PARTS:
            canvas.create_text(x, y, anchor="w", text=part, fill=color,
                               font=theme.FONT_HERO, tags="text")
            x += hero_font.measure(part)

        canvas.create_text(left, int(height * 0.54), anchor="w", text=HERO_SUB,
                           fill=theme.PANEL_SUBTLE, font=theme.FONT_BODY, tags="text")

        # 底部那行状态：这台机器上的服务在不在。由 `set_local_status` 现填，
        # 写死「已就绪」等于在登录页上撒谎。
        canvas.create_text(left, int(height * 0.9), anchor="w", text="●",
                           fill=self._status_color, font=(theme.UI, 12), tags="text")
        canvas.create_text(left + 22, int(height * 0.9), anchor="w", text=self._status_text,
                           fill=theme.SIDEBAR_TEXT, font=theme.FONT_BODY, tags="text")

    _status_text = "本地服务未启动"
    _status_color = theme.GRAY

    def set_local_status(self, text: str, color: str) -> None:
        self._status_text = text
        self._status_color = color
        try:
            self._render_panel_text()
        except tk.TclError:
            pass

    # ── 右：表单 ─────────────────────────────────────────────────

    def _build_form(self) -> None:
        right = tk.Frame(self, bg=theme.CONTENT_BG)
        right.place(relx=PANEL_RATIO, rely=0, relwidth=1 - PANEL_RATIO, relheight=1)

        form = tk.Frame(right, bg=theme.CONTENT_BG)
        form.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.66)

        self._pill = tk.Frame(form, bg=theme.PILL_BG)
        self._pill.pack(fill=tk.X, pady=(0, 34))
        self._pill_dot = tk.Label(self._pill, text="●", font=(theme.UI, 9),
                                  bg=theme.PILL_BG, fg=theme.GREEN)
        self._pill_dot.pack(side=tk.LEFT, padx=(12, 6), pady=8)
        self._pill_text = tk.Label(self._pill, text=self._pill_default(), font=theme.FONT_SMALL,
                                   bg=theme.PILL_BG, fg=theme.TEXT)
        self._pill_text.pack(side=tk.LEFT)

        tk.Label(form, text="设置管理员账号" if self._setup_mode else "登录",
                 font=theme.FONT_LOGIN_TITLE, bg=theme.CONTENT_BG,
                 fg=theme.TEXT).pack(anchor="w")
        tk.Label(form, text=("这台机器第一次使用，先设一个本机账号"
                             if self._setup_mode else "使用管理员账号继续"),
                 font=theme.FONT_BODY, bg=theme.CONTENT_BG,
                 fg=theme.TEXT_MUTED).pack(anchor="w", pady=(8, 26))

        self._account = tk.StringVar(value=auth.last_account(self._data_dir))
        self._password = tk.StringVar()
        self._confirm = tk.StringVar()
        self._remember = tk.BooleanVar(value=bool(self._account.get()))

        account_entry = self._field(form, "账号", self._account, placeholder="请输入账号")
        self._field(form, "密码", self._password, secret=True, placeholder="请输入密码")
        if self._setup_mode:
            self._field(form, "确认密码", self._confirm, secret=True, placeholder="再输一遍")

        row = tk.Frame(form, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X, pady=(6, 0))
        tk.Checkbutton(row, text="记住账号", variable=self._remember, font=theme.FONT_SMALL,
                       bg=theme.CONTENT_BG, fg=theme.TEXT, activebackground=theme.CONTENT_BG,
                       selectcolor=theme.CONTENT_BG, highlightthickness=0,
                       cursor="hand2").pack(side=tk.LEFT)

        self._error = tk.Label(form, text="", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                               fg=theme.RED, anchor="w", justify=tk.LEFT, wraplength=360)

        self._button = tk.Button(
            form, text="设置并进入" if self._setup_mode else "登录", command=self.submit,
            font=theme.FONT_BUTTON, bg=theme.GREEN_DEEP, fg="#ffffff",
            activebackground=theme.GREEN_DEEP, activeforeground="#ffffff",
            relief=tk.FLAT, cursor="hand2", pady=10, highlightthickness=0,
        )
        self._button.pack(fill=tk.X, pady=(22, 0))

        foot = tk.Frame(right, bg=theme.CONTENT_BG)
        foot.place(relx=0.5, rely=0.94, anchor="center")
        tk.Label(foot, text=f"版本 {self._version or '-'}  ·  {self._provider.name}模式",
                 font=theme.FONT_SMALL, bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack()

        for widget in (self, right, form):
            widget.bind("<Return>", lambda _e: self.submit())
        account_entry.focus_set()

    def _pill_default(self) -> str:
        return "本机模式：账号和参数都在这台机器上" if self._provider.name == "本机" \
            else "已连上后端，可以登录"

    def _field(self, parent: tk.Misc, label: str, var: tk.StringVar, *,
               secret: bool = False, placeholder: str = "") -> tk.Entry:
        """一行输入框：外面一圈 1px 边（聚焦变绿），里面是**唯一**一个白底 Entry。

        ★ 原来的做法是拿一个灰底 Frame 当边框、Entry 只 ``fill=X`` 塞进去——Entry 撑不满
        竖直方向，Frame 的灰底就从上下露出来一大块；密码框那个眼睛又是在 Entry 之后
        ``side=RIGHT``，跟已经占满整行的 Entry 抢地方，于是整行错位。用户 2026-08-15
        反馈的「文本框是乱的」就是这个。现在：**眼睛先占位，Entry 再 expand 吃掉剩下的**，
        边框改用 Frame 自己的 highlight（不占内容区），上下留白靠 Entry 的 pady。
        """
        tk.Label(parent, text=label, font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                 fg=theme.TEXT).pack(anchor="w", pady=(0, 6))
        box = tk.Frame(parent, bg=theme.FIELD_BG, highlightthickness=1,
                       highlightbackground=theme.FIELD_LINE, highlightcolor=theme.FIELD_LINE)
        box.pack(fill=tk.X, pady=(0, 16))

        entry = tk.Entry(box, textvariable=var, font=theme.FONT_INPUT, relief=tk.FLAT, bd=0,
                         bg=theme.FIELD_BG, fg=theme.TEXT, insertbackground=theme.TEXT,
                         highlightthickness=0, show="*" if secret else "")
        if secret:
            # 「显示密码」的眼睛：打错密码是登录失败里最常见的一种，能看一眼比重打三遍强。
            # **必须先 pack**：side=RIGHT 要在 Entry 吃掉整行之前把自己的位置占下来。
            eye = tk.Label(box, text="👁", font=theme.FONT_INPUT, bg=theme.FIELD_BG,
                           fg=theme.TEXT_MUTED, cursor="hand2")
            eye.pack(side=tk.RIGHT, padx=(0, 12))
            eye.bind("<Button-1>",
                     lambda _e, e=entry: e.configure(show="" if e.cget("show") else "*"))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 8), pady=9)
        entry.bind("<Return>", lambda _e: self.submit())

        # 聚焦时边框变绿：设计稿上「当前在填哪一格」就是靠它标出来的。
        entry.bind("<FocusIn>", lambda _e: box.configure(highlightbackground=theme.GREEN,
                                                         highlightcolor=theme.GREEN), add="+")
        entry.bind("<FocusOut>", lambda _e: box.configure(highlightbackground=theme.FIELD_LINE,
                                                          highlightcolor=theme.FIELD_LINE), add="+")
        if placeholder:
            self._attach_placeholder(entry, var, placeholder)
        return entry

    def _attach_placeholder(self, entry: tk.Entry, var: tk.StringVar, text: str) -> None:
        """灰字提示。

        ★ 用一个**盖在上面的 Label**，不往 StringVar 里塞占位文字——塞进去的话，
        用户不点这一格直接回车，提交上去的账号就成了「请输入账号」。这种坑一旦埋下，
        表现是「密码明明没打错却登不进去」，查起来极贵。
        """
        hint = tk.Label(entry.master, text=text, font=theme.FONT_INPUT,
                        bg=theme.FIELD_BG, fg=theme.TEXT_MUTED)
        hint.bind("<Button-1>", lambda _e: entry.focus_set())

        def sync(*_args: object) -> None:
            try:
                if var.get() or entry.focus_get() is entry:
                    hint.place_forget()
                else:
                    hint.place(in_=entry, x=1, rely=0.5, anchor="w")
            except (tk.TclError, KeyError):
                pass  # 控件正在销毁

        var.trace_add("write", lambda *_: sync())
        entry.bind("<FocusIn>", lambda _e: sync(), add="+")
        entry.bind("<FocusOut>", lambda _e: sync(), add="+")
        entry.after(0, sync)

    # ── 提交 ─────────────────────────────────────────────────────

    def submit(self) -> None:
        if self._busy:
            return  # 连点两下不该发两次请求
        account = self._account.get().strip()
        password = self._password.get()
        if not account or not password:
            self._show_error("账号和密码都要填")
            return
        if self._setup_mode and password != self._confirm.get():
            self._show_error("两次输入的密码不一样")
            return

        self._set_busy(True)
        self._show_error("")
        threading.Thread(target=self._work, args=(account, password),
                         name="wx-ui-login", daemon=True).start()

    def _work(self, account: str, password: str) -> None:
        """后台线程：登录。**这里一律不碰控件**，结果 after 回 tk 线程。"""
        try:
            if self._setup_mode:
                identity = self._provider.create(account, password)  # type: ignore[attr-defined]
            else:
                identity = self._provider.login(account, password)
        except AuthError as exc:
            # 先把话取出来再进 lambda：except 块一结束 Python 就把 exc 删了，
            # 闭包里再去读它拿到的是 NameError（ruff F821 当场抓到过）。
            message = str(exc)
            self._later(lambda: self._failed(message))
            return
        except Exception as exc:  # noqa: BLE001 - 没预料到的也得让人看见，不能只在日志里
            message = f"登录出错：{exc}"
            self._later(lambda: self._failed(message))
            return
        self._later(lambda: self._succeeded(identity))

    def _later(self, callback: Callable[[], None]) -> None:
        """把结果丢回 tk 线程。

        ``after`` 从别的线程调，只有在 **mainloop 正在跑** 的时候才是安全的；窗口已经销毁
        （TclError）或者压根没进过 mainloop（RuntimeError: main thread is not in main loop）
        都得咽掉——这条路径上唯一的后果是「登录结果没人接」，比甩一屏 traceback 强。
        """
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def _succeeded(self, identity: Identity) -> None:
        if self._remember.get():
            auth.remember_account(self._data_dir, identity.account)
        else:
            auth.forget_account(self._data_dir)
        self._set_busy(False)
        self._on_authenticated(identity)

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self._show_error(message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self._button.configure(
                text=("正在设置…" if self._setup_mode else "正在登录…") if busy
                else ("设置并进入" if self._setup_mode else "登录"),
                bg=theme.GRAY if busy else theme.GREEN_DEEP,
                cursor="watch" if busy else "hand2",
            )
        except tk.TclError:
            pass

    def _show_error(self, message: str) -> None:
        self._error.configure(text=message)
        if message:
            self._error.pack(anchor="w", pady=(10, 0), before=self._button)
        else:
            self._error.pack_forget()
