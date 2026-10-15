"""配置中心（原「参数设置」）。与实时日志页共用同一个壳，版式照 `docs/setting.png`。

保存只落文件、不热改运行中的服务——参数的生效点就是服务启动那一刻，含糊不得。
所以给了「保存并重启服务」，让「改完到底生效没有」这件事不留悬念。

★ 只有这一份真相（2026-08-15）：正式的客户端接口**不下发配置**（见
`docs/招聘分身_客户端认证与状态接口_v1.1.md` 的接口清单），所以原来那层「后端下发 +
本机覆盖」连同标注一起删了——两份配置就会有两个答案，而人只会相信自己看见的那个。

参数按「连接 / 调度 / 启动」分三段。标签、输入框、说明纵向成组；空间充足时两组并排，
窄窗口或大字号时改为单列，说明换行，不挤占输入宽度。底部一条固定的动作条（保存状态 + 仅保存 +
保存并重启服务），**不跟着滚**——一屏放不下的时候，按钮不能跟着滚出视野。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import font as tkfont

from . import dialog, settings_store, theme, widgets
from .settings_store import FIELDS, SECTIONS, Field, Section

_MASK = "········"


class SettingsPage(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        data_dir: Path,
        *,
        on_saved: Callable[[bool], None] | None = None,
        version: str = "",
        on_check_update: Callable[[], None] | None = None,
        sections: tuple[str, ...] = settings_store.SETTINGS_SECTIONS,
    ) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self.data_dir = Path(data_dir)
        self.on_saved = on_saved
        self._version = version
        self._on_check_update = on_check_update
        self._update_note: tk.Label | None = None
        self._values = settings_store.effective(self.data_dir)
        self._vars: dict[str, tk.Variable] = {}
        self._secret_touched: dict[str, bool] = {}
        self._marks: dict[str, tk.Label] = {}
        self._dirty = False
        self._testing = False
        self._test_label: tk.Label | None = None
        self._state_text: tk.Label | None = None
        self._state_dot: tk.Label | None = None
        #: 这一页管哪几段。配置中心和「演示」页是同一套控件的两份，各管各的键，存到同一个文件。
        self._sections = tuple(sections)
        self._fields = tuple(f for f in FIELDS if f.section in self._sections)
        #: 带 `locked_value` 的格子：(输入框, 真值, 关着时显示的地板)。
        self._lockable: dict[str, tuple[tk.Entry, tk.StringVar, tk.StringVar]] = {}
        self._field_frames: dict[str, tk.Frame] = {}
        self._layout_columns: dict[tk.Frame, int] = {}
        self._field_font = tkfont.Font(self, font=theme.FONT_BODY)

        # ★ 这里曾经有一行「参数由后端下发…」和一个「重新拉取」，2026-08-15 用户要求撤掉：
        # 后端那头还没有，摆着只会让人以为「点一下就能同步」。参数在哪儿生效看下面的
        # 「保存到：…」那行就够了。
        for section in SECTIONS:
            if section.key in self._sections:
                self._build_section(section)
        if "conn" in self._sections:
            self._build_about()
        toggle = self._vars.get(settings_store.DEMO_TOGGLE_KEY)
        if toggle is not None and self._lockable:
            toggle.trace_add("write", lambda *_: self._render_locks())
        self._render_locks()

        tk.Label(
            self,
            text=f"保存到：{settings_store.settings_path(self.data_dir)}",
            font=theme.FONT_SMALL,
            bg=theme.CONTENT_BG,
            fg=theme.TEXT_MUTED,
        ).pack(anchor="w", padx=24, pady=(18, 4))

    # ── 版式 ─────────────────────────────────────────────────────

    def _build_about(self) -> None:
        """「关于」：当前版本 + 手动「检查更新」。自动查在后台默默做（`updater.py`），
        这里是人想**现在就知道**时的那一下——结果写在按钮旁边，不弹窗。
        """
        wrap = tk.Frame(self, bg=theme.CONTENT_BG)
        wrap.pack(fill=tk.X, padx=24, pady=(20, 0))
        head = tk.Frame(wrap, bg=theme.CONTENT_BG)
        head.pack(fill=tk.X)
        tk.Label(head, text="关于", font=theme.FONT_HEADING,
                 bg=theme.CONTENT_BG, fg=theme.TEXT).pack(side=tk.LEFT)
        tk.Label(head, text=f"当前版本 v{self._version}" if self._version else "当前版本未知",
                 font=theme.FONT_SMALL, bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack(
            side=tk.LEFT, padx=(14, 0))
        widgets.ghost_button(head, "检查更新", self._check_update).pack(side=tk.RIGHT)
        self._update_note = tk.Label(head, text="", font=theme.FONT_SMALL,
                                     bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._update_note.pack(side=tk.RIGHT, padx=(0, 12))
        tk.Frame(wrap, bg=theme.ROW_LINE, height=1).pack(fill=tk.X, pady=(10, 6))

    def _check_update(self) -> None:
        if self._on_check_update is None:
            self.show_update_note("这份没有接更新器", theme.TEXT_MUTED)
            return
        self._on_check_update()

    def show_update_note(self, text: str, color: str) -> None:
        if self._update_note is not None:
            self._update_note.configure(text=text, fg=color)

    def _build_section(self, section: Section) -> None:
        wrap = tk.Frame(self, bg=theme.CONTENT_BG)
        wrap.pack(fill=tk.X, padx=24, pady=(20, 0))

        head = tk.Frame(wrap, bg=theme.CONTENT_BG)
        head.pack(fill=tk.X)
        tk.Label(head, text=section.title, font=theme.FONT_HEADING,
                 bg=theme.CONTENT_BG, fg=theme.TEXT).pack(side=tk.LEFT)
        if section.key == "conn":
            # 「测到端口通不通」是这台机器唯一能自证的事，鉴权对不对只有真连上才知道，
            # 所以结果文案里必须把这层边界说清楚（见 `_run_test`）。
            widgets.ghost_button(head, "测试连接", self._test_connection).pack(side=tk.RIGHT)
            self._test_label = tk.Label(head, text="", font=theme.FONT_SMALL,
                                        bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
            self._test_label.pack(side=tk.RIGHT, padx=(0, 12))

        if section.desc:
            desc = tk.Label(wrap, text=section.desc, font=theme.FONT_SMALL,
                            bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, anchor="w", justify=tk.LEFT)
            desc.pack(fill=tk.X, pady=(4, 0))
            desc.bind("<Configure>", lambda e: desc.configure(wraplength=max(1, e.width)))

        tk.Frame(wrap, bg=theme.ROW_LINE, height=1).pack(fill=tk.X, pady=(10, 6))

        body = tk.Frame(wrap, bg=theme.CONTENT_BG)
        body.pack(fill=tk.X)
        fields = settings_store.fields_of(section.key)
        for field in fields:
            self._add_field(body, field)
        body.bind("<Configure>", lambda _e: self._layout_fields(body, fields, section.columns))
        self._layout_fields(body, fields, section.columns)

    def _layout_fields(self, body: tk.Frame, fields: tuple[Field, ...], max_columns: int) -> None:
        # 按实际字体量宽：Windows 高缩放下不能仍用固定像素阈值硬塞两列。
        min_width = self._field_font.measure("0" * 32) + 32
        columns = max_columns if body.winfo_width() >= min_width * max_columns + 24 else 1
        if self._layout_columns.get(body) == columns:
            return
        self._layout_columns[body] = columns
        for slot in range(max_columns):
            body.columnconfigure(slot, weight=1 if slot < columns else 0,
                                 uniform="field" if slot < columns else "")
        for index, field in enumerate(fields):
            row, slot = divmod(index, columns)
            padding = (12 if slot else 0, 12 if slot < columns - 1 else 0)
            self._field_frames[field.key].grid(row=row, column=slot, sticky="new",
                                              padx=padding, pady=8)

    def _add_field(self, parent: tk.Misc, field: Field) -> None:
        group = tk.Frame(parent, bg=theme.CONTENT_BG)
        group.columnconfigure(0, weight=1)
        self._field_frames[field.key] = group
        label = tk.Label(group, text=field.label, font=theme.FONT_BODY, bg=theme.CONTENT_BG,
                         fg=theme.TEXT, anchor="w", justify=tk.LEFT)
        label.grid(row=0, column=0, sticky="we")

        current = self._values.get(field.key, "")
        if field.kind == "bool":
            var: tk.Variable = tk.BooleanVar(value=settings_store.as_bool(current))
            cell = tk.Frame(group, bg=theme.CONTENT_BG)
            cell.grid(row=1, column=0, sticky="we", pady=(8, 0))
            widgets.ToggleSwitch(cell, var).pack(side=tk.LEFT)
        else:
            var = tk.StringVar(value=_MASK if field.secret and current else current)
            cell = tk.Frame(group, bg=theme.CONTENT_BG)
            cell.grid(row=1, column=0, sticky="we", pady=(4, 0))
            entry = self._field_box(cell, field, var)
            if field.locked_value:
                self._lockable[field.key] = (entry, var, tk.StringVar(value=field.locked_value))

        mark = tk.Label(group, text=field.help, font=theme.FONT_SMALL,
                        bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, anchor="w", justify=tk.LEFT)
        mark.grid(row=2, column=0, sticky="we", pady=(4, 0))

        def wrap_text(event: tk.Event) -> None:
            width = max(1, event.width)
            if int(mark.cget("wraplength")) != width:
                mark.configure(wraplength=width)
                label.configure(wraplength=width)

        group.bind("<Configure>", wrap_text)

        self._marks[field.key] = mark
        self._vars[field.key] = var
        var.trace_add("write", lambda *_: self._render_dirty())

    def _field_box(self, cell: tk.Frame, field: Field, var: tk.Variable) -> tk.Entry:
        """一格输入框。边框画在外层 Frame 上，聚焦变绿；单位和眼睛贴在它右边。"""
        box = tk.Frame(cell, bg=theme.FIELD_BG, highlightthickness=1,
                       highlightbackground=theme.FIELD_LINE, highlightcolor=theme.FIELD_LINE)
        box.pack(side=tk.LEFT, fill=tk.X, expand=True)

        if field.unit:
            tk.Label(box, text=field.unit, font=theme.FONT_SMALL, bg=theme.FIELD_BG,
                     fg=theme.TEXT_MUTED, padx=10).pack(side=tk.RIGHT, fill=tk.Y)
            tk.Frame(box, bg=theme.FIELD_LINE, width=1).pack(side=tk.RIGHT, fill=tk.Y)

        entry = tk.Entry(box, textvariable=var, font=theme.FONT_BODY, width=1, relief=tk.FLAT, bd=0,
                         bg=theme.FIELD_BG, fg=theme.TEXT, insertbackground=theme.TEXT,
                         highlightthickness=0, show="*" if field.secret else "")
        if field.secret:
            eye = tk.Label(box, text="👁", font=theme.FONT_BODY, bg=theme.FIELD_BG,
                           fg=theme.TEXT_MUTED, cursor="hand2")
            eye.pack(side=tk.RIGHT, padx=(0, 10))
            eye.bind("<Button-1>",
                     lambda _e, e=entry: e.configure(show="" if e.cget("show") else "*"))
            # 掩码原样存回去等于把密钥改成一串点，所以要知道用户到底动没动它
            self._secret_touched[field.key] = False
            entry.bind("<Key>", lambda _e, key=field.key: self._mark_touched(key))
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 8), pady=7)
        widgets.focus_ring(box, entry)
        if field.placeholder and isinstance(var, tk.StringVar):
            widgets.attach_placeholder(entry, var, field.placeholder, font=theme.FONT_BODY)
        return entry

    def _render_locks(self) -> None:
        """演示页：开关关着 → 三格灰着显示**正在生效的地板**、不能改；开着 → 显示并编辑演示值。

        换的是输入框挂的变量，不是变量的值：关着时存盘仍按真值（上次填的演示值）走，
        不会把地板数字写进演示键里。
        """
        toggle = self._vars.get(settings_store.DEMO_TOGGLE_KEY)
        on = bool(toggle.get()) if toggle is not None else False
        for entry, real, floor in self._lockable.values():
            entry.configure(textvariable=real if on else floor,
                            state=tk.NORMAL if on else "readonly",
                            fg=theme.TEXT if on else theme.TEXT_MUTED,
                            readonlybackground=theme.FIELD_BG)

    def build_action_bar(self, parent: tk.Misc) -> tk.Frame:
        """底部动作条。**由外面 pack 在页面底部**，不进滚动区——参数一多，按钮就会跟着
        滚出视野，人只好先滚到底才能保存。设计稿上它也是钉在底下的一条。
        """
        bar = tk.Frame(parent, bg=theme.CONTENT_BG)
        tk.Frame(bar, bg=theme.ROW_LINE, height=1).pack(fill=tk.X)
        inner = tk.Frame(bar, bg=theme.CONTENT_BG)
        inner.pack(fill=tk.X, padx=24, pady=12)
        state = tk.Frame(inner, bg=theme.CONTENT_BG)
        state.pack(fill=tk.X)

        self._state_dot = tk.Label(state, text="✓", font=theme.FONT_BODY,
                                   bg=theme.CONTENT_BG, fg=theme.GREEN)
        self._state_dot.pack(side=tk.LEFT)
        self._state_text = tk.Label(state, text="", font=theme.FONT_SMALL,
                                    bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._state_text.pack(side=tk.LEFT, padx=(8, 0))

        actions = tk.Frame(inner, bg=theme.CONTENT_BG)
        actions.pack(fill=tk.X, pady=(8, 0))
        primary = tk.Label(actions, text="保存并重启服务", font=theme.FONT_SMALL,
                           bg=theme.GREEN_DEEP, fg="#ffffff", padx=22, pady=8, cursor="hand2")
        primary.pack(side=tk.RIGHT)
        primary.bind("<Button-1>", lambda _e: self._save(restart=True))

        widgets.ghost_button(actions, "仅保存", lambda: self._save(restart=False)).pack(
            side=tk.RIGHT, padx=(0, 12), ipady=1)

        self._render_dirty()
        return bar

    # ── 状态 ─────────────────────────────────────────────────────

    def _render_dirty(self) -> None:
        """底下那行「所有更改均已保存 / 有未保存的更改」。

        照**实际差异**算，不是「碰过就算脏」——点开一个下拉又原样点回去还报「未保存」，
        人就会开始不信这行字。
        """
        self._dirty = self._pending_changes()
        if self._state_text is None or self._state_dot is None:
            return
        if self._dirty:
            self._state_dot.configure(text="●", fg=theme.ORANGE)
            self._state_text.configure(text="有未保存的更改")
        else:
            self._state_dot.configure(text="✓", fg=theme.GREEN)
            self._state_text.configure(text="所有更改均已保存")

    def _pending_changes(self) -> bool:
        for field in self._fields:
            var = self._vars.get(field.key)
            if var is None:
                continue
            saved = self._values.get(field.key, "")
            if field.kind == "bool":
                if bool(var.get()) != settings_store.as_bool(saved):
                    return True
                continue
            if field.secret and not self._secret_touched.get(field.key, False):
                continue
            if str(var.get()).strip() != saved:
                return True
        return False

    def reload(self) -> None:
        """重新进这一页时按磁盘上的现值刷一遍输入框。

        页是复用的（见 ``MainWindow._ensure_settings_page``），不刷的话看到的还是上次
        打开时的快照——别人手工改过 settings 文件、或者刚保存过一轮，界面就和现实对不上。
        """
        self._values = settings_store.effective(self.data_dir)
        for field in self._fields:
            var = self._vars.get(field.key)
            if var is None:
                continue
            current = self._values.get(field.key, "")
            if field.kind == "bool":
                var.set(settings_store.as_bool(current))
                continue
            if field.secret:
                self._secret_touched[field.key] = False  # 又是没动过的状态，掩码不能写回去
                var.set(_MASK if current else "")
                continue
            var.set(current)
        self._render_locks()
        self._render_dirty()

    def _mark_touched(self, key: str) -> None:
        self._secret_touched[key] = True

    # ── 动作 ─────────────────────────────────────────────────────

    def _test_connection(self) -> None:
        """测一下地址通不通。点了就立刻有反馈，别让人对着一个没反应的按钮猜。"""
        if self._testing:
            return
        import threading

        targets = [
            ("AI 端", str(self._vars["WXCLIENT_GATEWAY_AI_WS_URL"].get()).strip()),
            ("业务端", str(self._vars["WXCLIENT_GATEWAY_BUSINESS_API_URL"].get()).strip()),
        ]
        targets = [(name, url) for name, url in targets if url]
        if not targets:
            self._show_test("先填上 AI 端地址", theme.ORANGE)
            return
        self._testing = True
        self._show_test("正在测…", theme.TEXT_MUTED)
        threading.Thread(target=self._run_test, args=(targets,),
                         name="wx-ui-conntest", daemon=True).start()

    def _run_test(self, targets: list[tuple[str, str]]) -> None:
        """后台线程：只测到「TCP/TLS 能不能连上」，**不测鉴权**，文案里也这么说。

        说满了更害人——「测试连接：通过」而实际内部 key 是错的，人会拿着这句话去怀疑别处。
        """
        results = [f"{name}{self._probe(url)}" for name, url in targets]
        text = " · ".join(results) + "（只测网络可达，鉴权看日志）"
        bad = not all(item.endswith("可达") for item in results)
        try:
            self.after(0, lambda: self._finish_test(text, theme.RED if bad else theme.GREEN))
        except (tk.TclError, RuntimeError):
            pass  # 页面已经没了

    @staticmethod
    def _probe(url: str, timeout: float = 5.0) -> str:
        import socket
        import ssl
        from urllib.parse import urlsplit

        parts = urlsplit(url if "//" in url else f"//{url}")
        host = parts.hostname
        if not host:
            return "地址不像个网址"
        secure = parts.scheme in {"wss", "https"}
        port = parts.port or (443 if secure else 80)
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                if secure:
                    context = ssl.create_default_context()
                    with context.wrap_socket(sock, server_hostname=host):
                        pass
        except socket.gaierror:
            # 单独拎出来：域名压根解析不了，和「解析得到但连不上」是两种毛病，
            # 报同一句话会让人去查防火墙，其实是地址打错了
            return "找不到这台主机"
        except OSError as exc:
            return f"连不上（{exc.__class__.__name__}）"
        return "可达"

    def _show_test(self, text: str, color: str) -> None:
        if self._test_label is not None:
            self._test_label.configure(text=text, fg=color)

    def _finish_test(self, text: str, color: str) -> None:
        self._testing = False
        self._show_test(text, color)

    def _collect(self) -> dict[str, str]:
        # 从磁盘重读一遍打底：另一页（配置中心 / 演示）刚存过的键不能被这页的旧快照盖回去。
        values = settings_store.effective(self.data_dir)
        for field in self._fields:
            var = self._vars[field.key]
            if field.kind == "bool":
                values[field.key] = "1" if bool(var.get()) else "0"
                continue
            if field.secret and not self._secret_touched.get(field.key, False):
                continue  # 没动过就保留原值，别把掩码写回去
            values[field.key] = str(var.get()).strip()
        return values

    def _save(self, *, restart: bool) -> None:
        values = settings_store.apply_demo_presets(self._collect())
        problems = settings_store.validate(values)
        if problems:
            # 网关地板以下的数：以前悄悄抬回去只打日志，界面上还显示你填的——「改了没反应」。
            # 现在不存、说清楚哪格为什么（用户 2026-09-18）。
            dialog.tell(self, "有几个数不能这么填",
                        "\n".join(problems.values()) + "\n\n改好再保存。", tone="error")
            return
        try:
            path = settings_store.save(self.data_dir, values)
        except OSError as exc:
            dialog.tell(self, "保存失败",
                        f"写入 {settings_store.FILE_NAME} 失败：{exc}", tone="error")
            return
        self._values = settings_store.effective(self.data_dir)
        self._render_dirty()
        if self.on_saved is not None:
            self.on_saved(restart)
        dialog.tell(
            self,
            "已保存",
            f"参数已写入：\n{path}\n\n"
            + (
                "服务正在后台按新参数重启，左侧状态栏会一路转到「运行中」。"
                if restart
                else "下次启动服务时生效。"
            ),
        )
