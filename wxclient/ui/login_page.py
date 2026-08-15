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

HERO_PARTS = (("让", theme.SIDEBAR_TITLE), ("微信服务", theme.GREEN), ("稳定在线", theme.SIDEBAR_TITLE))
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

        # 背景图是**预先切好尺寸的**（`assets/login-bg.png`，900x1000）：Tk 8.6 自带 PNG
        # 解码，不用把 PIL 拉进启动路径（和启动动画一个道理，见 splash.py）。窗口比图大就
        # 露出同色底，肉眼看不出来——底色是照着图的底色调平过的。
        from . import icons

        path = icons.ASSETS / "login-bg.png"
        if path.exists():
            try:
                self._photo = tk.PhotoImage(master=canvas, file=str(path))
                canvas.create_image(0, 0, anchor="nw", image=self._photo)
            except tk.TclError:
                pass  # 图挂了不该让人登不上

        canvas.bind("<Configure>", self._render_panel_text)

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

        account_entry = self._field(form, "账号", self._account)
        self._field(form, "密码", self._password, secret=True)
        if self._setup_mode:
            self._field(form, "确认密码", self._confirm, secret=True)

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
            font=theme.FONT_BUTTON, bg=theme.GREEN, fg="#ffffff",
            activebackground=theme.GREEN, activeforeground="#ffffff",
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

    def _field(self, parent: tk.Misc, label: str, var: tk.StringVar, *, secret: bool = False) -> tk.Entry:
        tk.Label(parent, text=label, font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                 fg=theme.TEXT).pack(anchor="w", pady=(0, 6))
        box = tk.Frame(parent, bg=theme.FIELD_LINE, padx=1, pady=1)
        box.pack(fill=tk.X, pady=(0, 16))
        entry = tk.Entry(box, textvariable=var, font=theme.FONT_INPUT, relief=tk.FLAT,
                         bg=theme.FIELD_BG, fg=theme.TEXT, insertbackground=theme.TEXT,
                         highlightthickness=0, show="*" if secret else "")
        entry.pack(fill=tk.X, ipady=8, padx=10)
        entry.bind("<Return>", lambda _e: self.submit())
        if secret:
            # 「显示密码」的眼睛：打错密码是登录失败里最常见的一种，能看一眼比重打三遍强。
            eye = tk.Label(box, text="👁", font=theme.FONT_SMALL, bg=theme.FIELD_BG,
                           fg=theme.TEXT_MUTED, cursor="hand2", padx=8)
            eye.pack(side=tk.RIGHT, fill=tk.Y)
            eye.bind("<Button-1>", lambda _e, e=entry: e.configure(show="" if e.cget("show") else "*"))
        return entry

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
                bg=theme.GRAY if busy else theme.GREEN,
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
