"""配置中心（原「参数设置」）。与实时日志页共用同一个壳。

保存只落文件、不热改运行中的服务——参数的生效点就是服务启动那一刻，含糊不得。
所以给了「保存并重启服务」，让「改完到底生效没有」这件事不留悬念。

★ 两处真相要对得上（用户 2026-08-15 裁决：「本地可以改 后端可下发」）：登录之后后端会
下发一份参数（``auth.py``），本机在这一页填的**盖过**它。两边都有值又不一样时，行尾会
标出「本机已改（后端：xxx）」——不标的话，「我在后端管理页改了怎么不生效」会变成悬案。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox

from . import settings_store, theme
from .settings_store import FIELDS, Field

_MASK = "········"


class SettingsPage(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        data_dir: Path,
        *,
        on_saved: Callable[[bool], None] | None = None,
        on_refetch: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self.data_dir = Path(data_dir)
        self.on_saved = on_saved
        self.on_refetch = on_refetch
        self._values = settings_store.effective(self.data_dir)
        self._overridden = settings_store.overridden(self.data_dir)
        self._vars: dict[str, tk.Variable] = {}
        self._secret_touched: dict[str, bool] = {}
        self._marks: dict[str, tk.Label] = {}

        head = tk.Frame(self, bg=theme.CONTENT_BG)
        head.pack(fill=tk.X, padx=24, pady=(0, 14))
        self._source_label = tk.Label(head, text="", font=theme.FONT_SMALL,
                                      bg=theme.CONTENT_BG, fg=theme.TEXT_MUTED)
        self._source_label.pack(side=tk.LEFT)
        if on_refetch is not None:
            refetch = tk.Label(head, text="重新拉取", font=theme.FONT_SMALL,
                               bg=theme.CONTENT_BG, fg=theme.GREEN, cursor="hand2")
            refetch.pack(side=tk.RIGHT)
            refetch.bind("<Button-1>", lambda _e: self.on_refetch and self.on_refetch())

        body = tk.Frame(self, bg=theme.CONTENT_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=24)
        body.columnconfigure(1, weight=1)

        for row, field in enumerate(FIELDS):
            self._add_row(body, row, field)
        self._render_marks()

        tk.Label(
            body,
            text=f"保存到：{settings_store.settings_path(self.data_dir)}",
            font=theme.FONT_SMALL,
            bg=theme.CONTENT_BG,
            fg=theme.TEXT_MUTED,
        ).grid(row=len(FIELDS), column=0, columnspan=3, sticky="w", pady=(16, 0))

        foot = tk.Frame(self, bg=theme.CONTENT_BG)
        foot.pack(fill=tk.X, padx=24, pady=16)
        self._button(foot, "保存并重启服务", lambda: self._save(restart=True), primary=True)
        self._button(foot, "仅保存", lambda: self._save(restart=False))

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None], *, primary: bool = False) -> None:
        button = tk.Button(
            parent,
            text=text,
            command=command,
            font=theme.FONT_SMALL,
            bg=theme.GREEN if primary else theme.CONTENT_BG,
            fg="#ffffff" if primary else theme.TEXT,
            activebackground=theme.GREEN if primary else theme.ROW_ALT_BG,
            activeforeground="#ffffff" if primary else theme.TEXT,
            relief=tk.FLAT,
            padx=18,
            pady=6,
            cursor="hand2",
            highlightthickness=0 if primary else 1,
            highlightbackground=theme.ROW_LINE,
        )
        button.pack(side=tk.RIGHT, padx=(10, 0))

    def _add_row(self, parent: tk.Misc, row: int, field: Field) -> None:
        tk.Label(
            parent,
            text=field.label,
            font=theme.FONT_BODY,
            bg=theme.CONTENT_BG,
            fg=theme.TEXT,
            anchor="e",
            width=14,
        ).grid(row=row, column=0, sticky="e", pady=6, padx=(0, 12))

        current = self._values.get(field.key, "")
        if field.kind == "bool":
            var: tk.Variable = tk.BooleanVar(value=settings_store.as_bool(current))
            tk.Checkbutton(
                parent,
                variable=var,
                text=field.help or "",
                font=theme.FONT_SMALL,
                bg=theme.CONTENT_BG,
                fg=theme.TEXT_MUTED,
                activebackground=theme.CONTENT_BG,
                selectcolor=theme.CONTENT_BG,
                highlightthickness=0,
            ).grid(row=row, column=1, columnspan=2, sticky="w", pady=6)
        else:
            var = tk.StringVar(value=_MASK if field.secret and current else current)
            entry = tk.Entry(
                parent,
                textvariable=var,
                font=(theme.MONO, 9),
                relief=tk.FLAT,
                bg=theme.ROW_ALT_BG,
                fg=theme.TEXT,
                insertbackground=theme.TEXT,
                highlightthickness=1,
                highlightbackground=theme.ROW_LINE,
                highlightcolor=theme.GREEN,
                show="*" if field.secret else "",
            )
            entry.grid(row=row, column=1, sticky="we", pady=6, ipady=4)
            if field.secret:
                # 掩码原样存回去等于把密钥改成一串点，所以要知道用户到底动没动它
                self._secret_touched[field.key] = False
                entry.bind("<Key>", lambda _e, key=field.key: self._mark_touched(key))
            mark = tk.Label(
                parent,
                text=field.help,
                font=theme.FONT_SMALL,
                bg=theme.CONTENT_BG,
                fg=theme.TEXT_MUTED,
                anchor="w",
            )
            mark.grid(row=row, column=2, sticky="w", padx=12)
            self._marks[field.key] = mark
        self._vars[field.key] = var

    def set_source(self, source: str) -> None:
        """说清这份参数是哪来的。空着不写比写错强，所以没拉到就直说没拉到。"""
        self._source_label.configure(
            text={"后端": "参数由后端下发，本机改动会盖在上面",
                  "本机": "还没接后端，这些参数都在这台机器上"}.get(source, "参数来源未知")
        )

    def _render_marks(self) -> None:
        """在被本机盖掉的那几行标出后端的原值。**只标不同的**，一样的没什么好说。"""
        for field in FIELDS:
            mark = self._marks.get(field.key)
            if mark is None:
                continue
            remote = self._overridden.get(field.key)
            if remote is None:
                mark.configure(text=field.help, fg=theme.TEXT_MUTED)
            else:
                shown = "········" if field.secret else remote
                mark.configure(text=f"本机已改（后端：{shown}）", fg=theme.ORANGE)

    def reload(self) -> None:
        """重新进这一页时按磁盘上的现值刷一遍输入框。

        页是复用的（见 ``MainWindow._ensure_settings_page``），不刷的话看到的还是上次
        打开时的快照——别人手工改过 settings 文件、或者刚保存过一轮，界面就和现实对不上。
        """
        self._values = settings_store.effective(self.data_dir)
        self._overridden = settings_store.overridden(self.data_dir)
        self._render_marks()
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

    def _mark_touched(self, key: str) -> None:
        self._secret_touched[key] = True

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
        return self._drop_same_as_remote(values)

    def _drop_same_as_remote(self, values: dict[str, str]) -> dict[str, str]:
        """和后端下发的一模一样的键，**不在本机留一份**。

        ★ 不这么做的话，这一页只要保存过一次，十几个键就全部变成「本机覆盖」——从此后端
        再下发什么都不生效，而界面上看不出任何异常（值确实是对的，只是冻在了保存那一刻）。
        本机文件里只该躺着「这台机器上的人真的改过的那几个」。
        """
        remote = settings_store.load_remote(self.data_dir)
        kinds = {field.key: field.kind for field in FIELDS}
        for key, value in list(values.items()):
            if key not in remote:
                continue
            same = (settings_store.as_bool(value) == settings_store.as_bool(remote[key])
                    if kinds.get(key) == "bool" else value == remote[key])
            if same:
                values.pop(key)
        return values

    def _save(self, *, restart: bool) -> None:
        values = self._collect()
        try:
            path = settings_store.save(self.data_dir, values)
        except OSError as exc:
            messagebox.showerror("保存失败", f"写入 {settings_store.FILE_NAME} 失败：{exc}", parent=self)
            return
        self._values = settings_store.effective(self.data_dir)
        self._overridden = settings_store.overridden(self.data_dir)
        self._render_marks()
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
