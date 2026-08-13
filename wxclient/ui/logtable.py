"""实时日志视图：一个只读的 ``tk.Text`` 控制台，按 时间 / 级别 / 正文 三段着色。

**为什么不再用「一行一堆控件」。** 早先这里是 Canvas 里塞一串行 Frame，每行三个 Label
加一条 1px 分隔线。做出来是好看，代价有三条：
  · 每行 5 个真控件，满屏 500 行就是两千多个控件，tk 的布局器扛不住，日志一多就发卡；
  · 文字选不中、复制不出来——排障时想把一行日志贴给别人都做不到；
  · 行数一封顶就得销毁最旧的控件，滚动和重排的成本全压在日志最忙的时候。

换成单个 Text 之后：整个视图就一个控件，几千行也不喘；``tag_config`` 能给任意字符区间
上色，所以「时间灰、级别按严重程度着色、正文黑」照样做得到，还白捡了选中/复制/查找。
行与行之间用 ``spacing`` 拉开，比 1px 硬线更耐看，也不会在高分屏上糊成一团。

警告和错误整行铺一层浅底色——扫一眼就知道哪儿不对，不用逐行读级别列。
"""

from __future__ import annotations

import tkinter as tk
from collections import deque
from collections.abc import Iterable

from . import theme
from .logline import LEVEL_FILTERS, LogLine

#: 视图里最多留多少行。Text 撑得住，但内存和滚动条精度没必要无限扩张；
#: 超出的部分仍在 LogBuffer 和日志文件里，只是界面不再显示。
MAX_LINES = 3000

#: 级别列宽（字符）。定宽才能让正文对齐成一列，不定宽的日志看着就是乱的。
_LEVEL_WIDTH = 5

#: 严重程度 → (级别字颜色, 整行底色)。底色 None = 不铺。
_SEVERITY_STYLE = {
    "info": (theme.GREEN, None),
    "warn": (theme.ORANGE, "#fff8ec"),
    "error": (theme.RED, "#fef2f2"),
    "debug": (theme.GRAY, None),
}


class LogTable(tk.Frame):
    def __init__(self, parent: tk.Misc, max_lines: int = MAX_LINES) -> None:
        super().__init__(parent, bg=theme.ROW_LINE, padx=1, pady=1)
        self._max_lines = max_lines
        # 留一份数据：换过滤档位时要按新条件重画，不能指望从 Text 里把结构化字段抠回来。
        self._lines: deque[LogLine] = deque(maxlen=max_lines)
        self._allowed = LEVEL_FILTERS[0][1]

        self._text = tk.Text(
            self, bg=theme.CONTENT_BG, fg=theme.TEXT, font=theme.FONT_LOG,
            wrap=tk.WORD, relief=tk.FLAT, highlightthickness=0, bd=0,
            padx=18, pady=10, spacing1=3, spacing3=5, cursor="arrow",
            # 正文缩进到级别列右边：一条日志换行之后仍然对齐，不会跑到时间列底下去
            tabs=("150p", "205p"), insertwidth=0,
        )
        self._scroll = tk.Scrollbar(self, orient=tk.VERTICAL, command=self._text.yview, width=12)
        self._text.configure(yscrollcommand=self._scroll.set)
        self._scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._text.tag_configure("time", foreground=theme.TEXT_MUTED)
        self._text.tag_configure("msg", foreground=theme.TEXT, font=theme.FONT_BODY)
        for severity, (color, row_bg) in _SEVERITY_STYLE.items():
            self._text.tag_configure(f"level_{severity}", foreground=color, font=(theme.MONO, 9, "bold"))
            if row_bg:
                self._text.tag_configure(f"row_{severity}", background=row_bg)
        # 只读，但要留着选中和 Ctrl+C——排障时能把日志贴给别人是刚需。
        self._text.configure(state=tk.DISABLED)
        self._text.bind("<Key>", self._swallow_keys)

    # -- 只读但可复制 ----------------------------------------------------

    def _swallow_keys(self, event: tk.Event) -> str | None:
        """吞掉编辑类按键，放行复制/全选/翻页。"""
        allowed = {"c", "C", "a", "A"}
        if event.state & 0x4 and event.keysym in allowed:  # Ctrl+C / Ctrl+A
            return None
        if event.keysym in {"Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End"}:
            return None
        return "break"

    # -- 内容 ------------------------------------------------------------

    def append(self, lines: list[LogLine]) -> None:
        """一次画一批。**别一行一次地调**——成串日志会把界面压死。"""
        if not lines:
            return
        self._lines.extend(lines)
        visible = [item for item in lines if item.severity in self._allowed]
        if visible:
            self._write(visible)

    def clear(self) -> None:
        self._lines.clear()
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)

    def set_level_filter(self, allowed: frozenset[str]) -> None:
        """换过滤档位，按新条件整屏重画。"""
        if allowed == self._allowed:
            return
        self._allowed = allowed
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)
        self._write([item for item in self._lines if item.severity in self._allowed])

    @property
    def line_count(self) -> int:
        return len(self._lines)

    # -- 渲染 ------------------------------------------------------------

    def _write(self, items: Iterable[LogLine]) -> None:
        stick = self._at_bottom()  # 用户往回翻时别把他拽回底部
        self._text.configure(state=tk.NORMAL)
        for item in items:
            start = self._text.index("end-1c")
            severity = item.severity
            self._text.insert(tk.END, f"{item.time or '':<19}\t", ("time",))
            self._text.insert(tk.END, f"{item.level or '':<{_LEVEL_WIDTH}}\t", (f"level_{severity}",))
            self._text.insert(tk.END, f"{item.message}\n", ("msg",))
            if _SEVERITY_STYLE.get(severity, (None, None))[1]:
                self._text.tag_add(f"row_{severity}", start, self._text.index("end-1c"))
        self._trim()
        self._text.configure(state=tk.DISABLED)
        if stick:
            self._text.see(tk.END)

    def _trim(self) -> None:
        """行数封顶。Text 里 delete 整段比逐行销毁控件便宜得多。"""
        overflow = int(self._text.index("end-1c").split(".")[0]) - self._max_lines
        if overflow > 0:
            self._text.delete("1.0", f"{overflow + 1}.0")

    def _at_bottom(self) -> bool:
        top, bottom = self._text.yview()
        return bottom > 0.999 or top == bottom  # 内容没占满时也算在底部
