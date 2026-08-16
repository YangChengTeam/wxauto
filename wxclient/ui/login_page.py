"""登录页（按 ``docs/login.png``）：左边深色品牌面板，右边白色表单。

**这是一道真门，不是欢迎页**：登录之前看不到日志和配置，服务也不起。

登的是**业务后端的招聘分身客户端接口**（邮箱 + 密码，不走邮箱验证码，契约见
`docs/招聘分身_客户端认证与状态接口_v1.1.md`）。原来那个「本机模式」已经删了——
服务端管不到的登录方式，撤销会话、停用账号对它一律无效。

两件必须做对的事：

1. **登录动作在后台线程上跑。** 它要发网络请求（最长 8 秒）；放在 tk 线程上就是
   「点了登录，窗口白一片」——桌面端在别处已经为这个栽过跟头（见 ``ui/__main__.py``
   的 shutdown 说明）。这里同样是主线程只管立刻能看见的部分，结果 ``after`` 回来。
2. **失败要说人话且留在原地。** 密码打错不该弹个模态框再把输入框清空，错误就贴在按钮
   上方，账号还在，改一下再来。

还没配业务端地址时（全新的一台机器），表单上会多出一格「服务器地址」：不填它连登录
请求都发不出去，而这时人还进不了配置中心。填一次就落到 `gateway.env`，之后不再出现。

「这个分身已在另一台电脑登录」（`409 ACTIVE_CLIENT_SESSION_EXISTS`）**不自动顶掉**：
弹一句问清楚，人点了确认才带 `replaceExistingSession=true` 再来一次——顶掉的是同事
正在用的那台机器，这个决定不能由客户端自己做。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import auth, settings_store, theme, widgets
from .auth import AuthError, ClientAuth, Identity

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
        auth_client: ClientAuth,
        *,
        data_dir: Path,
        on_authenticated: Callable[[Identity], None],
        version: str = "",
    ) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self._auth = auth_client
        self._data_dir = Path(data_dir)
        self._on_authenticated = on_authenticated
        self._version = version
        self._busy = False
        self._photo: tk.PhotoImage | None = None
        #: 没配业务端地址就得当场填一个，否则登录请求根本发不出去。
        self._needs_server = not auth_client.configured

        self._build_panel()
        self._build_form()
        self._maybe_autologin()

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

        # ★ 这儿原来有一行「本地服务未启动」。删了（用户 2026-08-15）：登录页上服务本来
        # 就该是停的（登录之后才起），把这句话摆在人第一眼看到的地方，只会让人以为
        # 有东西坏了、去找哪里能「启动」它——而这里根本没有那个按钮。

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

        tk.Label(form, text="登录", font=theme.FONT_LOGIN_TITLE, bg=theme.CONTENT_BG,
                 fg=theme.TEXT).pack(anchor="w")
        tk.Label(form, text="用劳小智的邮箱账号登录", font=theme.FONT_BODY,
                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack(anchor="w", pady=(8, 26))

        self._account = tk.StringVar(value=auth.last_account(self._data_dir))
        self._password = tk.StringVar()
        self._server = tk.StringVar(value=self._auth.api.base_url)
        # 默认记住：招聘分身是常驻这台电脑替人干活的，每天开机重登一次纯属折腾。
        self._remember = tk.BooleanVar(value=True)

        if self._needs_server:
            self._field(form, "服务器地址", self._server,
                        placeholder="如 https://<business-host>")
        account_entry = self._field(form, "邮箱", self._account, placeholder="请输入邮箱")
        self._field(form, "密码", self._password, secret=True, placeholder="请输入密码")

        row = tk.Frame(form, bg=theme.CONTENT_BG)
        row.pack(fill=tk.X, pady=(6, 0))
        tk.Checkbutton(row, text="记住登录状态", variable=self._remember, font=theme.FONT_SMALL,
                       bg=theme.CONTENT_BG, fg=theme.TEXT, activebackground=theme.CONTENT_BG,
                       selectcolor=theme.CONTENT_BG, highlightthickness=0,
                       cursor="hand2").pack(side=tk.LEFT)

        self._error = tk.Label(form, text="", font=theme.FONT_SMALL, bg=theme.CONTENT_BG,
                               fg=theme.RED, anchor="w", justify=tk.LEFT, wraplength=360)

        self._button = tk.Button(
            form, text="登录", command=self.submit,
            font=theme.FONT_BUTTON, bg=theme.GREEN_DEEP, fg="#ffffff",
            activebackground=theme.GREEN_DEEP, activeforeground="#ffffff",
            relief=tk.FLAT, cursor="hand2", pady=10, highlightthickness=0,
        )
        self._button.pack(fill=tk.X, pady=(22, 0))

        foot = tk.Frame(right, bg=theme.CONTENT_BG)
        foot.place(relx=0.5, rely=0.94, anchor="center")
        tk.Label(foot, text=f"版本 {self._version or '-'}  ·  劳小智招聘分身客户端",
                 font=theme.FONT_SMALL, bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack()

        for widget in (self, right, form):
            widget.bind("<Return>", lambda _e: self.submit())
        account_entry.focus_set()

    # ── 自动登录 ─────────────────────────────────────────────────

    def _maybe_autologin(self) -> None:
        """勾过「记住登录状态」的机器：把邮箱密码填回去并**直接登一次**。

        什么时候会走到这儿：Token 还在的话，`ClientAuth.restore()` 早就把人放进去了，
        压根不会建这个登录页。所以走到这儿意味着**Token 没了而人没变**——换了 Windows
        账号、数据目录被清、文件损坏、重装。那几种情况下让人再打一遍密码纯属白折腾。

        ★ 走不到这儿的两种情况，都是故意的：
        * **主动退出登录** → `store.clear()`，密码也没了，下次要自己输（他就是想下来）。
        * **会话被撤销** → 同上（见 `auth.ClientAuth.handle_revoked`）。不这么做的话，
          管理员刚把这台机器撤掉，它转头自己登回来了。

        自动登录**失败就停在这一页**，把话摆出来、密码留在框里让人改——不重试：
        密码错了重试一百次也是错的，而账号有 5 次连续失败就锁 15 分钟的闸。
        """
        email, password = self._auth.remembered_login()
        if not (email and password):
            return
        self._account.set(email)
        self._password.set(password)
        self._pill_text.configure(text="正在用记住的账号登录…")
        # 等界面先画出来：自动登录要发网络请求，人总得先看见这一页在做什么。
        # 走 `submit()` 而不是直接 `_start`：地址校验、去空格、防重复点击那几条，
        # 自动登录该和人点「登录」走同一条路。
        self.after(60, self.submit)

    def _pill_default(self) -> str:
        """顶上那颗胶囊。**只说这台机器上确定的事**——地址配没配、Token 能不能存住，
        至于业务端此刻通不通，只有真发一次登录请求才知道，别在这儿替它打包票。

        ★ 原来写的是「用邮箱密码登录，登录状态会记住」（用户 2026-08-15 说不够好）：
        一句话把下面那两个输入框和那个勾选框各复述了一遍——人看得见的东西不需要旁白。
        换成说这台电脑登录之后**会变成什么**：这才是登录页该回答的问题。
        """
        if self._needs_server:
            return "先填服务器地址，再用邮箱登录"
        if not self._auth.store.persistent:
            return "这台机器存不住登录状态，每次打开都要重新登录"
        return "登录后，这台电脑就是你的招聘分身"

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

        # 聚焦变绿的边框和灰字占位，和配置中心共用一份实现（`ui/widgets.py`）——
        # 各写各的迟早写成两个样子。
        widgets.focus_ring(box, entry)
        if placeholder:
            widgets.attach_placeholder(entry, var, placeholder)
        return entry

    # ── 提交 ─────────────────────────────────────────────────────

    def submit(self) -> None:
        if self._busy:
            return  # 连点两下不该发两次请求
        email = self._account.get().strip()
        password = self._password.get()
        if self._needs_server and not self._apply_server():
            return
        if not email or not password:
            self._show_error("邮箱和密码都要填")
            return
        if "@" not in email:
            # 只挡最明显的那一类（把用户名当邮箱填）。真正的校验在服务端，
            # 这里多拦一层只是省一次往返，不该自己发明一套邮箱规则。
            self._show_error("这看着不像一个邮箱")
            return
        self._start(email, password, replace=False)

    def _apply_server(self) -> bool:
        """把「服务器地址」当场存下来并生效。

        ★ 存的是**同一个键**（``WXCLIENT_GATEWAY_BUSINESS_API_URL``），配置中心里改的
        就是它——不另开一份「登录用的地址」，否则登录连一个后端、换票连另一个，
        而两处界面各自看着都对。
        """
        url = self._server.get().strip()
        if not url:
            self._show_error("先填服务器地址")
            return False
        if not url.startswith(("http://", "https://")):
            self._show_error("服务器地址要以 http:// 或 https:// 开头")
            return False
        values = settings_store.load(self._data_dir)
        values["WXCLIENT_GATEWAY_BUSINESS_API_URL"] = url
        try:
            settings_store.save(self._data_dir, values)
        except OSError as exc:
            self._show_error(f"存不下服务器地址：{exc}")
            return False
        settings_store.apply_to_environ(values, force=True)
        self._auth.api.base_url = url.rstrip("/")
        return True

    def _start(self, email: str, password: str, *, replace: bool) -> None:
        self._set_busy(True)
        self._show_error("")
        threading.Thread(target=self._work, args=(email, password, replace),
                         name="wx-ui-login", daemon=True).start()

    def _work(self, email: str, password: str, replace: bool) -> None:
        """后台线程：登录。**这里一律不碰控件**，结果 after 回 tk 线程。"""
        try:
            identity = self._auth.login(email, password, replace_existing=replace,
                                        remember=self._remember.get())
        except AuthError as exc:
            # 先把话取出来再进 lambda：except 块一结束 Python 就把 exc 删了，
            # 闭包里再去读它拿到的是 NameError（ruff F821 当场抓到过）。
            message, details = str(exc), dict(exc.details)
            if exc.needs_replace_confirm:
                self._later(lambda: self._ask_replace(email, password, details))
                return
            self._later(lambda: self._failed(message))
            return
        except Exception as exc:  # noqa: BLE001 - 没预料到的也得让人看见，不能只在日志里
            message = f"登录出错：{exc}"
            self._later(lambda: self._failed(message))
            return
        self._later(lambda: self._succeeded(identity))

    def _ask_replace(self, email: str, password: str, details: dict[str, Any]) -> None:
        """另一台电脑还占着这个分身。**问清楚再顶**（契约 §5.1）。

        顶掉之后旧设备上没执行完的动作不会转过来（服务端明写不转移），所以这句话必须
        让人看见「是哪台机器、最后一次活动是什么时候」，而不是一句轻飘飘的「确定吗」。
        """
        from tkinter import messagebox

        self._set_busy(False)
        if not details.get("canReplace", True):
            self._failed("这个招聘分身已在另一台电脑登录，且不允许在这里切换")
            return
        where = str(details.get("deviceName") or "另一台电脑")
        seen = str(details.get("lastSeenAt") or "")
        detail = f"最后活动：{seen}\n\n" if seen else ""
        if not messagebox.askyesno(
            "切换到这台电脑？",
            f"这个招聘分身正在「{where}」上登录。\n{detail}"
            "切换过来会让那台电脑立即退出登录，它上面还没执行的动作不会转移过来。",
            parent=self,
        ):
            self._show_error("没有切换，仍在另一台电脑上登录")
            return
        self._start(email, password, replace=True)

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
        # 记不记（Token、密码、邮箱三样）全由 `ClientAuth.login(remember=…)` 一处决定，
        # 这里不再补第二处落盘——分两处写过一次，结果是「不记住」只忘一半。
        self._set_busy(False)
        self._on_authenticated(identity)

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        # 自动登录没成，胶囊上那句「正在用记住的账号登录…」得撤回去——不然它会一直挂在
        # 那儿，和下面刚贴出来的红字各说各的。
        self._pill_text.configure(text=self._pill_default())
        self._show_error(message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        try:
            self._button.configure(
                text="正在登录…" if busy else "登录",
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
