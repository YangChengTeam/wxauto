"""日志表格：时间 / 级别 / 正文 三列，行间细分隔线（照 ``docs/wxui.png``）。

为什么不用 ``tk.Text``：Text 只能整行上色，做不出「时间灰、级别绿、正文黑」的分列着色，
更做不出行间那条 1px 分隔线。所以用 Canvas 里放一串行 Frame——每行三个 Label 加一条
分隔线，颜色随便配。

代价是每行都是真控件，所以**有行数上限**：超了就把最旧的行销毁。上限外的日志仍然在
``LogBuffer`` 和日志文件里，只是界面不再画它——界面卡死比少显示几屏旧日志严重得多。
"""

from __future__ import annotations

import tkinter as tk

from . import theme
from .logline import LogLine

MAX_ROWS = 500

_SEVERITY_COLOR = {
    "info": theme.GREEN,
    "warn": theme.ORANGE,
    "error": theme.RED,
    "debug": theme.GRAY,
}


class LogTable(tk.Frame):
    def __init__(self, parent: tk.Misc, max_rows: int = MAX_ROWS) -> None:
        super().__init__(parent, bg=theme.ROW_LINE, padx=1, pady=1)
        self._max_rows = max_rows
        self._rows: list[tk.Frame] = []
        self._scroll_pending = False

        self._canvas = tk.Canvas(self, bg=theme.CONTENT_BG, highlightthickness=0, bd=0)
        self._scroll = tk.Scrollbar(self, orient=tk.VERTICAL, command=self._canvas.yview, width=12)
        self._canvas.configure(yscrollcommand=self._scroll.set)
        self._scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._body = tk.Frame(self._canvas, bg=theme.CONTENT_BG)
        self._window = self._canvas.create_window((0, 0), window=self._body, anchor="nw")

        self._body.bind("<Configure>", self._on_body_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)

    # -- 布局 ----------------------------------------------------------

    def _on_body_resize(self, _event: tk.Event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_resize(self, event: tk.Event) -> None:
        self._canvas.itemconfigure(self._window, width=event.width)

    def _on_wheel(self, event: tk.Event) -> None:
        if self._canvas.winfo_ismapped():
            self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _at_bottom(self) -> bool:
        top, bottom = self._scroll.get()
        return bottom > 0.999 or top == bottom  # 内容没占满时也算在底部

    # -- 内容 ----------------------------------------------------------

    def append(self, lines: list[LogLine]) -> None:
        """一次画一批。**别一行一次地调**——每次调用都要重排一遍，成串日志会把界面压死。"""
        if not lines:
            return
        stick = self._at_bottom()  # 用户往回翻时别把他拽回底部
        # 一批里超过上限的部分连建都不用建：反正建出来立刻就要销毁。
        if len(lines) > self._max_rows:
            lines = lines[-self._max_rows:]
        for item in lines:
            self._rows.append(self._make_row(item))
        while len(self._rows) > self._max_rows:
            self._rows.pop(0).destroy()
        if stick and not self._scroll_pending:
            self._scroll_pending = True
            self.after_idle(self._scroll_to_end)

    def clear(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()
        self._scroll_pending = False

    def _scroll_to_end(self) -> None:
        self._scroll_pending = False
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._canvas.yview_moveto(1.0)

    def _make_row(self, item: LogLine) -> tk.Frame:
        # 每多一个控件就多一份布局开销 × 行数上限。原来一行 5 个（行框 + 内层框 + 3 个
        # Label + 分隔线），内层框只是用来加 padding 的——padding 直接挂到 Label 上，
        # 一行省一个控件，满屏就是省 500 个。行框本身当分隔线：底色用线色、内容区留 1px。
        row = tk.Frame(self._body, bg=theme.ROW_LINE)
        row.pack(fill=tk.X)

        line = tk.Frame(row, bg=theme.CONTENT_BG)
        line.pack(fill=tk.X, pady=(0, 1))

        tk.Label(line, text=item.time, font=theme.FONT_LOG, bg=theme.CONTENT_BG,
                 fg=theme.TEXT_MUTED, width=20, anchor="w").pack(side=tk.LEFT, padx=(18, 0), pady=7)
        tk.Label(line, text=item.level, font=theme.FONT_LOG, bg=theme.CONTENT_BG,
                 fg=_SEVERITY_COLOR.get(item.severity, theme.TEXT_MUTED),
                 width=6, anchor="w").pack(side=tk.LEFT, pady=7)
        tk.Label(line, text=item.message, font=theme.FONT_BODY, bg=theme.CONTENT_BG,
                 fg=theme.TEXT, anchor="w", justify=tk.LEFT).pack(
                     side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 18), pady=7)
        return row
