"""参数设置页。与实时日志页共用同一个壳（左侧状态栏 + 右侧白色内容区）。

保存只落文件、不热改运行中的服务——参数的生效点就是服务启动那一刻，含糊不得。
所以给了「保存并重启服务」，让「改完到底生效没有」这件事不留悬念。
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
    ) -> None:
        super().__init__(parent, bg=theme.CONTENT_BG)
        self.data_dir = Path(data_dir)
        self.on_saved = on_saved
        self._values = settings_store.effective(self.data_dir)
        self._vars: dict[str, tk.Variable] = {}
        self._secret_touched: dict[str, bool] = {}

        body = tk.Frame(self, bg=theme.CONTENT_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=24)
        body.columnconfigure(1, weight=1)

        for row, field in enumerate(FIELDS):
            self._add_row(body, row, field)

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
            if field.help:
                tk.Label(
                    parent,
                    text=field.help,
                    font=theme.FONT_SMALL,
                    bg=theme.CONTENT_BG,
                    fg=theme.TEXT_MUTED,
                ).grid(row=row, column=2, sticky="w", padx=12)
        self._vars[field.key] = var

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
        return values

    def _save(self, *, restart: bool) -> None:
        values = self._collect()
        try:
            path = settings_store.save(self.data_dir, values)
        except OSError as exc:
            messagebox.showerror("保存失败", f"写入 {settings_store.FILE_NAME} 失败：{exc}", parent=self)
            return
        self._values = values
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
