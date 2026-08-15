"""实时日志视图：一个只读的 ``tk.Text`` 控制台，按 时间 / 级别 / 消息 三列着色。

**为什么不再用「一行一堆控件」。** 早先这里是 Canvas 里塞一串行 Frame，每行三个 Label
加一条 1px 分隔线。做出来是好看，代价有三条：
  · 每行 5 个真控件，满屏 500 行就是两千多个控件，tk 的布局器扛不住，日志一多就发卡；
  · 文字选不中、复制不出来——排障时想把一行日志贴给别人都做不到；
  · 行数一封顶就得销毁最旧的控件，滚动和重排的成本全压在日志最忙的时候。

换成单个 Text 之后：整个视图就一个控件，几千行也不喘；``tag_config`` 能给任意字符区间
上色，所以「时间灰、级别按严重程度着色、消息黑」照样做得到，还白捡了选中/复制/查找。

样式对着 `docs/main.png` 走：
  · 顶上一条不滚动的表头（时间 / 级别 / 消息）+ 一条细分隔线，列头和数据列对齐；
  · 级别列是「圆点 + 大写级别」，颜色按严重程度；
  · 警告/错误整行铺一层浅底色，**左边再立一根竖色条**——扫一眼就知道哪儿不对；
  · 消息里的 ``key="value"`` 压成灰色，让人先读得懂那句人话，再看参数。

列位置不写死像素：用当前字体现量（``tkfont.measure``），换 DPI / 换字号都不会错位，
表头 Label 也是拿同一组数字 ``place`` 出来的，所以表头和正文永远对得上。
"""

from __future__ import annotations

import re
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from collections.abc import Iterable

from . import theme
from .logline import LEVEL_FILTERS, LogLine

#: 视图里最多留多少行。Text 撑得住，但内存和滚动条精度没必要无限扩张；
#: 超出的部分仍在 LogBuffer 和日志文件里，只是界面不再显示。
MAX_LINES = 3000

#: 左边留白（像素）。竖色条从这儿开始立，色条左边是一条白边——和设计稿一致。
_PAD_X = 14

#: 列间距（像素）：时间列和级别列后面各留这么宽的空当。
_GAP = 26

#: 严重程度 → (颜色, 整行底色)。底色 None = 不铺，也就不立左边那根竖条。
_SEVERITY_STYLE = {
    "info": (theme.GREEN, None),
    "warn": (theme.ORANGE, theme.WARN_ROW_BG),
    "error": (theme.RED, theme.ERROR_ROW_BG),
    "debug": (theme.GRAY, None),
}

#: 日志里的结构化参数：``account="zk_weixin"``。灰掉它们，人话才浮得出来。
_KV = re.compile(r'\S+="[^"]*"')

#: 量列宽用的样板串。时间取最长的一种写法，级别取最长的那个标签。
_TIME_SAMPLE = "2026-08-15 13:29:13"
_LEVEL_SAMPLE = "● DEBUG"


class LogTable(tk.Frame):
    def __init__(self, parent: tk.Misc, max_lines: int = MAX_LINES) -> None:
        super().__init__(parent, bg=theme.ROW_LINE, padx=1, pady=1)
        self._max_lines = max_lines
        # 留一份数据：换过滤档位时要按新条件重画，不能指望从 Text 里把结构化字段抠回来。
        self._lines: deque[LogLine] = deque(maxlen=max_lines)
        self._allowed = LEVEL_FILTERS[0][1]

        mono = tkfont.Font(font=theme.FONT_LOG)
        mono_bold = tkfont.Font(font=(theme.MONO, theme.FONT_LOG[1], "bold"))
        self._bar_w = mono.measure(" ")                     # 竖色条 = 一个空格宽
        time_tab = self._bar_w + mono.measure(_TIME_SAMPLE) + _GAP
        msg_tab = time_tab + mono_bold.measure(_LEVEL_SAMPLE) + _GAP

        self._build_head(time_tab, msg_tab)

        body = tk.Frame(self, bg=theme.CONTENT_BG)
        body.pack(fill=tk.BOTH, expand=True)
        self._text = tk.Text(
            body, bg=theme.CONTENT_BG, fg=theme.TEXT, font=theme.FONT_LOG,
            wrap=tk.WORD, relief=tk.FLAT, highlightthickness=0, bd=0,
            padx=_PAD_X, pady=6, spacing1=7, spacing3=8, cursor="arrow",
            # 消息缩进到消息列：一条日志换行之后仍然对齐，不会跑到时间列底下去
            tabs=(str(time_tab), str(msg_tab)), insertwidth=0,
        )
        self._scroll = tk.Scrollbar(body, orient=tk.VERTICAL, command=self._text.yview, width=12)
        self._text.configure(yscrollcommand=self._scroll.set)
        self._scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._text.tag_configure("time", foreground=theme.TEXT_MUTED)
        self._text.tag_configure("msg", foreground=theme.TEXT, font=theme.FONT_BODY,
                                 lmargin2=msg_tab)
        self._text.tag_configure("kv", foreground=theme.TEXT_MUTED, font=theme.FONT_BODY,
                                 lmargin2=msg_tab)
        for severity, (color, row_bg) in _SEVERITY_STYLE.items():
            self._text.tag_configure(f"level_{severity}", foreground=color,
                                     font=(theme.MONO, theme.FONT_LOG[1], "bold"))
            if row_bg:
                self._text.tag_configure(f"row_{severity}", background=row_bg)
                self._text.tag_configure(f"bar_{severity}", background=color)
        # 只读，但要留着选中和 Ctrl+C——排障时能把日志贴给别人是刚需。
        self._text.configure(state=tk.DISABLED)
        self._text.bind("<Key>", self._swallow_keys)

    def _build_head(self, time_tab: int, msg_tab: int) -> None:
        """表头。不跟着滚——滚上去之后没人记得住第二列是什么。"""
        head = tk.Frame(self, bg=theme.CONTENT_BG, height=34)
        head.pack(fill=tk.X)
        head.pack_propagate(False)
        for text, offset in (("时间", self._bar_w), ("级别", time_tab), ("消息", msg_tab)):
            tk.Label(head, text=text, font=theme.FONT_TABLE_HEAD, bg=theme.CONTENT_BG,
                     fg=theme.TEXT_MUTED).place(x=_PAD_X + offset, rely=0.5, anchor="w")
        tk.Frame(self, bg=theme.TABLE_HEAD_LINE, height=1).pack(fill=tk.X)

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
            severity = item.severity
            row_bg = _SEVERITY_STYLE.get(severity, (None, None))[1]
            start = self._text.index("end-1c")
            # 行首这个空格就是左边那根竖色条：普通行不着色，看不出来
            self._text.insert(tk.END, " ", (f"bar_{severity}",) if row_bg else ())
            after_bar = self._text.index("end-1c")
            self._text.insert(tk.END, f"{item.time or '':<19}\t", ("time",))
            level = f"● {item.level}" if item.level else ""
            self._text.insert(tk.END, f"{level}\t", (f"level_{severity}",))
            self._insert_message(item.message)
            if row_bg:
                self._text.tag_add(f"row_{severity}", after_bar, self._text.index("end-1c"))
                self._text.tag_add(f"bar_{severity}", start, after_bar)
        self._trim()
        self._text.configure(state=tk.DISABLED)
        if stick:
            self._text.see(tk.END)

    def _insert_message(self, message: str) -> None:
        """人话用正文色，``key="value"`` 压成灰色。"""
        cursor = 0
        for match in _KV.finditer(message):
            if match.start() > cursor:
                self._text.insert(tk.END, message[cursor:match.start()], ("msg",))
            self._text.insert(tk.END, match.group(), ("kv",))
            cursor = match.end()
        self._text.insert(tk.END, f"{message[cursor:]}\n", ("msg",))

    def _trim(self) -> None:
        """行数封顶。Text 里 delete 整段比逐行销毁控件便宜得多。"""
        overflow = int(self._text.index("end-1c").split(".")[0]) - self._max_lines
        if overflow > 0:
            self._text.delete("1.0", f"{overflow + 1}.0")

    def _at_bottom(self) -> bool:
        top, bottom = self._text.yview()
        return bottom > 0.999 or top == bottom  # 内容没占满时也算在底部
