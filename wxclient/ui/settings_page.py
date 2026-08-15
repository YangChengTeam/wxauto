"""配置中心（原「参数设置」）。与实时日志页共用同一个壳，版式照 `docs/setting.png`。

保存只落文件、不热改运行中的服务——参数的生效点就是服务启动那一刻，含糊不得。
所以给了「保存并重启服务」，让「改完到底生效没有」这件事不留悬念。

★ 只有这一份真相（2026-08-15）：正式的客户端接口**不下发配置**（见
`docs/招聘分身_客户端认证与状态接口_v1.md` 的接口清单），所以原来那层「后端下发 +
本机覆盖」连同标注一起删了——两份配置就会有两个答案，而人只会相信自己看见的那个。

版式（设计稿）：参数按「连接 / 调度 / 启动」分三段，每段一个标题 + 一句说明 + 一条横线；
每行是「右对齐标签 + 输入框 + 灰字说明」；底部一条固定的动作条（保存状态 + 仅保存 +
保存并重启服务），**不跟着滚**——一屏放不下的时候，按钮不能跟着滚出视野。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox

from . import settings_store, theme, widgets
from .settings_store import FIELDS, SECTIONS, Field, Section

_MASK = "········"

#: 标签列宽（字符）。三段共用同一个宽度，各段的输入框才会左边对齐成一条线。
_LABEL_WIDTH = 12


class SettingsPage(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        data_dir: Path,
        *,
        on_saved: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self.data_dir = Path(data_dir)
        self.on_saved = on_saved
        self._values = settings_store.effective(self.data_dir)
        self._vars: dict[str, tk.Variable] = {}
        self._secret_touched: dict[str, bool] = {}
        self._marks: dict[str, tk.Label] = {}
        self._dirty = False
        self._testing = False
        self._test_label: tk.Label | None = None
        self._state_text: tk.Label | None = None
        self._state_dot: tk.Label | None = None

        # ★ 这里曾经有一行「参数由后端下发…」和一个「重新拉取」，2026-08-15 用户要求撤掉：
        # 后端那头还没有，摆着只会让人以为「点一下就能同步」。参数在哪儿生效看下面的
        # 「保存到：…」那行就够了。
        for section in SECTIONS:
            self._build_section(section)

        tk.Label(
            self,
            text=f"保存到：{settings_store.settings_path(self.data_dir)}",
            font=theme.FONT_SMALL,
            bg=theme.CONTENT_BG,
            fg=theme.TEXT_MUTED,
        ).pack(anchor="w", padx=24, pady=(18, 4))

    # ── 版式 ─────────────────────────────────────────────────────

    def _build_section(self, section: Section) -> None:
        wrap = tk.Frame(self, bg=theme.CONTENT_BG)
        wrap.pack(fill=tk.X, padx=24, pady=(20, 0))

        head = tk.Frame(wrap, bg=theme.CONTENT_BG)
        head.pack(fill=tk.X)
        tk.Label(head, text=section.title, font=theme.FONT_HEADING,
                 bg=theme.CONTENT_BG, fg=theme.TEXT).pack(side=tk.LEFT)
        tk.Label(head, text=section.desc, font=theme.FONT_SMALL,
                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack(side=tk.LEFT, padx=(14, 0))
        if section.key == "conn":
            # 「测到端口通不通」是这台机器唯一能自证的事，鉴权对不对只有真连上才知道，
            # 所以结果文案里必须把这层边界说清楚（见 `_run_test`）。
            widgets.ghost_button(head, "测试连接", self._test_connection).pack(side=tk.RIGHT)
            self._test_label = tk.Label(head, text="", font=theme.FONT_SMALL,
                                        bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
            self._test_label.pack(side=tk.RIGHT, padx=(0, 12))

        tk.Frame(wrap, bg=theme.ROW_LINE, height=1).pack(fill=tk.X, pady=(10, 6))

        body = tk.Frame(wrap, bg=theme.CONTENT_BG)
        body.pack(fill=tk.X)
        fields = settings_store.fields_of(section.key)
        for index, field in enumerate(fields):
            row, slot = divmod(index, section.columns)
            self._add_row(body, row, slot * 3, field)
        for slot in range(section.columns):
            body.columnconfigure(slot * 3 + 1, weight=1, uniform="field")

    def _add_row(self, parent: tk.Misc, row: int, column: int, field: Field) -> None:
        tk.Label(parent, text=field.label, font=theme.FONT_BODY, bg=theme.CONTENT_BG,
                 fg=theme.TEXT, anchor="e", width=_LABEL_WIDTH,
                 ).grid(row=row, column=column, sticky="e", pady=7, padx=(0, 14))

        current = self._values.get(field.key, "")
        if field.kind == "bool":
            var: tk.Variable = tk.BooleanVar(value=settings_store.as_bool(current))
            cell = tk.Frame(parent, bg=theme.CONTENT_BG)
            cell.grid(row=row, column=column + 1, columnspan=2, sticky="w", pady=7)
            widgets.ToggleSwitch(cell, var).pack(side=tk.LEFT)
            mark = tk.Label(cell, text=field.help, font=theme.FONT_SMALL,
                            bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, anchor="w")
            mark.pack(side=tk.LEFT, padx=(12, 0))
        else:
            var = tk.StringVar(value=_MASK if field.secret and current else current)
            cell = tk.Frame(parent, bg=theme.CONTENT_BG)
            cell.grid(row=row, column=column + 1, sticky="we", pady=7)
            self._field_box(cell, field, var)
            mark = tk.Label(parent, text=field.help, font=theme.FONT_SMALL,
                            bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED, anchor="w")
            mark.grid(row=row, column=column + 2, sticky="w", padx=14)

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

        entry = tk.Entry(box, textvariable=var, font=theme.FONT_BODY, relief=tk.FLAT, bd=0,
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

    def build_action_bar(self, parent: tk.Misc) -> tk.Frame:
        """底部动作条。**由外面 pack 在页面底部**，不进滚动区——参数一多，按钮就会跟着
        滚出视野，人只好先滚到底才能保存。设计稿上它也是钉在底下的一条。
        """
        bar = tk.Frame(parent, bg=theme.CONTENT_BG)
        tk.Frame(bar, bg=theme.ROW_LINE, height=1).pack(fill=tk.X)
        inner = tk.Frame(bar, bg=theme.CONTENT_BG)
        inner.pack(fill=tk.X, padx=24, pady=12)

        self._state_dot = tk.Label(inner, text="✓", font=theme.FONT_BODY,
                                   bg=theme.CONTENT_BG, fg=theme.GREEN)
        self._state_dot.pack(side=tk.LEFT)
        self._state_text = tk.Label(inner, text="", font=theme.FONT_SMALL,
                                    bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._state_text.pack(side=tk.LEFT, padx=(8, 0))

        primary = tk.Label(inner, text="保存并重启服务", font=theme.FONT_SMALL,
                           bg=theme.GREEN_DEEP, fg="#ffffff", padx=22, pady=8, cursor="hand2")
        primary.pack(side=tk.RIGHT)
        primary.bind("<Button-1>", lambda _e: self._save(restart=True))

        widgets.ghost_button(inner, "仅保存", lambda: self._save(restart=False)).pack(
            side=tk.RIGHT, padx=(0, 12), ipady=1)
        # 这句摆在按钮外面（`docs/setting2.png`）：它是「点下去会发生什么」的提醒，
        # 不是按钮的一部分——重启会断开微信/AI 端连接几秒，点之前就该知道。
        tk.Label(inner, text="重启时连接将短暂中断", font=theme.FONT_SMALL,
                 bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED).pack(side=tk.RIGHT, padx=(0, 16))

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
        for field in FIELDS:
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
        for field in FIELDS:
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
        values = dict(self._values)
        for field in FIELDS:
            var = self._vars[field.key]
            if field.kind == "bool":
                values[field.key] = "1" if bool(var.get()) else "0"
                continue
            if field.secret and not self._secret_touched.get(field.key, False):
                continue  # 没动过就保留原值，别把掩码写回去
            values[field.key] = str(var.get()).strip()
        return values

    def _save(self, *, restart: bool) -> None:
        values = self._collect()
        try:
            path = settings_store.save(self.data_dir, values)
        except OSError as exc:
            messagebox.showerror("保存失败", f"写入 {settings_store.FILE_NAME} 失败：{exc}", parent=self)
            return
        self._values = settings_store.effective(self.data_dir)
        self._render_dirty()
        if self.on_saved is not None:
            self.on_saved(restart)
        messagebox.showinfo(
            "已保存",
            f"参数已写入：\n{path}\n\n"
            + (
                "服务正在后台按新参数重启，左侧状态栏会一路转到「运行中」。"
                if restart
                else "下次启动服务时生效。"
            ),
            parent=self,
        )
