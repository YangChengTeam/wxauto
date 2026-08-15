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
  · 警告/错误整行铺一层浅底色，**左边再立一根细竖条**——扫一眼就知道哪儿不对；
  · 消息里的 ``key="value"`` 压成灰色，让人先读得懂那句人话，再看参数；
  · **一条日志占且只占一行**，长了就截断，鼠标停上去用气泡显示整条；
  · 行与行之间一条细分隔线，长日志和短日志混在一起也数得清是几条。

**为什么不换行**：日志的价值在于「一眼扫过去哪条不对」，一条 300 字的报错换成四行
之后，屏幕上就剩两条日志，扫不动了。截断 + 悬停气泡把「扫」和「读全」分成两件事。

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

#: 竖色条和右边文字之间的空当（像素）。没有它时间列会紧贴色条、整排字像是黏在左边框上
#: （用户 2026-08-15：「文字离左边不要贴着」）。
_BAR_GAP = 12

#: 左边那根竖色条的目标粗细（像素）。Text 里的「竖条」其实是行首一个上了色的空格，
#: 所以粗细 = 那个空格的字宽——用一个小号字体把它压到这么细（用户 2026-08-15：「橙色粗了」）。
_BAR_W = 4

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


def _bar_font(width: int) -> tuple[str, int]:
    """挑一个字号，让一个空格差不多正好 ``width`` 像素宽——竖条的粗细就是这个宽度。

    写死字号在不同 DPI 上会一台机器一个粗细，所以这里现量。字号只影响这一个空格，
    行高由同一行上更大的字撑着，压小它不会让行变矮。
    """
    for size in range(theme.FONT_LOG[1], 0, -1):
        if tkfont.Font(font=(theme.MONO, size)).measure(" ") <= width:
            return (theme.MONO, size)
    return (theme.MONO, 1)


class LogTable(tk.Frame):
    def __init__(self, parent: tk.Misc, max_lines: int = MAX_LINES) -> None:
        super().__init__(parent, bg=theme.ROW_LINE, padx=1, pady=1)
        self._max_lines = max_lines
        # 留一份数据：换过滤档位时要按新条件重画，不能指望从 Text 里把结构化字段抠回来。
        self._lines: deque[LogLine] = deque(maxlen=max_lines)
        self._allowed = LEVEL_FILTERS[0][1]
        # 屏幕上现在画着的是哪几条（过滤之后的）。气泡要靠它把「第几行」还原成整条日志——
        # 从 Text 里把截断过的正文抠回来是抠不全的。
        self._shown: deque[LogLine] = deque()
        self._tip: tk.Toplevel | None = None
        self._tip_label: tk.Label | None = None
        self._tip_line = -1

        mono = tkfont.Font(font=theme.FONT_LOG)
        mono_bold = tkfont.Font(font=(theme.MONO, theme.FONT_LOG[1], "bold"))
        self._bar_font = _bar_font(_BAR_W)
        self._bar_w = tkfont.Font(font=self._bar_font).measure(" ")
        # 三个制表位：时间列起点（色条 + 空当）、级别列起点、消息列起点。
        # ★时间列也要走制表位，不能「紧跟色条往后排」——色条那个空格在不同级别上宽度不一样
        # （见下面 bar_ tag），紧跟着排就会一行一个起点，时间列参差不齐
        # （用户 2026-08-15：「不同级别的信息对齐」）。
        text_tab = self._bar_w + _BAR_GAP
        time_tab = text_tab + mono.measure(_TIME_SAMPLE) + _GAP
        msg_tab = time_tab + mono_bold.measure(_LEVEL_SAMPLE) + _GAP

        self._build_head(text_tab, time_tab, msg_tab)

        body = tk.Frame(self, bg=theme.CONTENT_BG)
        body.pack(fill=tk.BOTH, expand=True)
        self._text = tk.Text(
            body, bg=theme.CONTENT_BG, fg=theme.TEXT, font=theme.FONT_LOG,
            # 不换行：一条日志一行，超出右边界的部分截掉，靠气泡看全（见 `_on_motion`）
            wrap=tk.NONE, relief=tk.FLAT, highlightthickness=0, bd=0,
            padx=_PAD_X, pady=6, spacing1=7, spacing3=8, cursor="arrow",
            tabs=(str(text_tab), str(time_tab), str(msg_tab)), insertwidth=0,
        )
        self._scroll = tk.Scrollbar(body, orient=tk.VERTICAL, command=self._text.yview, width=12)
        self._text.configure(yscrollcommand=self._scroll.set)
        self._scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._text.tag_configure("time", foreground=theme.TEXT_MUTED)
        self._text.tag_configure("msg", foreground=theme.TEXT, font=theme.FONT_BODY)
        self._text.tag_configure("kv", foreground=theme.TEXT_MUTED, font=theme.FONT_BODY)
        # 分隔线：一整行只有一个空格，上下留白清零，看到的就是一条细线。
        # 行距（widget 上的 spacing1/spacing3）必须在这个 tag 上归零，否则这条线要占十几像素。
        # ★字号用**负数**：Tk 里正数是「磅」，1 磅在 96 DPI 上就有 1.33 px，缩放一开更粗，
        # 于是这条「1px 细线」实际画出来三四像素（用户 2026-08-15：「分割线太粗」）。
        # 负数是「像素」，写多少就是多少，跟 DPI 无关。
        self._text.tag_configure("sep", background=theme.ROW_LINE, font=(theme.MONO, -1),
                                 spacing1=0, spacing3=0)
        for severity, (color, row_bg) in _SEVERITY_STYLE.items():
            self._text.tag_configure(f"level_{severity}", foreground=color,
                                     font=(theme.MONO, theme.FONT_LOG[1], "bold"))
            # 色条那个空格**每一级都要上 tag**：只给 warn/error 上的话，info/debug 的空格
            # 会按正文字号排版（宽出好几像素），那一行的时间列就跟着右移，各级别对不齐。
            # 不铺底色的级别只上字体、不上背景——看上去就是没有竖条。
            self._text.tag_configure(f"bar_{severity}", font=self._bar_font,
                                     **({"background": color} if row_bg else {}))
            if row_bg:
                self._text.tag_configure(f"row_{severity}", background=row_bg)
        # 只读，但要留着选中和 Ctrl+C——排障时能把日志贴给别人是刚需。
        self._text.configure(state=tk.DISABLED)
        self._text.bind("<Key>", self._swallow_keys)
        self._text.bind("<Motion>", self._on_motion)
        self._text.bind("<Leave>", lambda _e: self._hide_tip())

    def _build_head(self, text_tab: int, time_tab: int, msg_tab: int) -> None:
        """表头。不跟着滚——滚上去之后没人记得住第二列是什么。"""
        head = tk.Frame(self, bg=theme.CONTENT_BG, height=34)
        head.pack(fill=tk.X)
        head.pack_propagate(False)
        for text, offset in (("时间", text_tab), ("级别", time_tab), ("消息", msg_tab)):
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
        self._shown.clear()
        self._hide_tip()
        self._text.configure(state=tk.NORMAL)
        self._text.delete("1.0", tk.END)
        self._text.configure(state=tk.DISABLED)

    def set_level_filter(self, allowed: frozenset[str]) -> None:
        """换过滤档位，按新条件整屏重画。"""
        if allowed == self._allowed:
            return
        self._allowed = allowed
        self._shown.clear()
        self._hide_tip()
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
            # 行首这个空格就是左边那根竖色条：普通行只压窄不着色，看不出来
            self._text.insert(tk.END, " ", (f"bar_{severity}",))
            after_bar = self._text.index("end-1c")
            # 先跳到时间列的制表位，让文字和色条之间空出一段（列宽由制表位说了算，
            # 所以时间不用再补空格对齐）
            self._text.insert(tk.END, f"\t{item.time or ''}\t", ("time",))
            level = f"● {item.level}" if item.level else ""
            self._text.insert(tk.END, f"{level}\t", (f"level_{severity}",))
            self._insert_message(item.message)
            if row_bg:
                self._text.tag_add(f"row_{severity}", after_bar, self._text.index("end-1c"))
            # 每条日志后面跟一条分隔线（它自己占一个 Text 行，所以第 k 条日志在第 2k+1 行）
            self._text.insert(tk.END, " \n", ("sep",))
            self._shown.append(item)
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
        """条数封顶。Text 里 delete 整段比逐行销毁控件便宜得多。

        一条日志占两个 Text 行（正文 + 分隔线），所以删的时候要成对删，
        否则分隔线和正文会错位，气泡也会指到隔壁那条上去。
        """
        overflow = len(self._shown) - self._max_lines
        if overflow <= 0:
            return
        self._text.delete("1.0", f"{overflow * 2 + 1}.0")
        for _ in range(overflow):
            self._shown.popleft()

    # -- 悬停气泡 --------------------------------------------------------

    def _record_at(self, line: int) -> LogLine | None:
        """Text 第 ``line`` 行是哪一条日志。分隔线所在的偶数行不算。"""
        if line % 2 == 0:
            return None
        index = (line - 1) // 2
        return self._shown[index] if 0 <= index < len(self._shown) else None

    def _is_clipped(self, line: int) -> bool:
        """这一行有没有被右边界截掉。行尾那个字看不见 = 截了。"""
        return self._text.bbox(f"{line}.end-1c") is None

    def tip_text(self, record: LogLine) -> str:
        """气泡里显示的整条日志。截断的是显示，不是内容。"""
        return "  ".join(part for part in (record.time, record.level, record.message) if part)

    def _on_motion(self, event: tk.Event) -> None:
        line = int(self._text.index(f"@{event.x},{event.y}").split(".")[0])
        if line == self._tip_line:
            return  # 同一行上晃鼠标不重画，省得气泡跟着抖
        self._tip_line = line
        record = self._record_at(line)
        if record is None or not self._is_clipped(line):
            self._hide_tip(keep_line=True)
            return
        self._show_tip(self.tip_text(record), event.x_root, event.y_root)

    def _show_tip(self, text: str, x_root: int, y_root: int) -> None:
        if self._tip is None:
            tip = tk.Toplevel(self._text)
            tip.wm_overrideredirect(True)  # 没有标题栏的小浮层
            tip.attributes("-topmost", True)
            label = tk.Label(tip, text="", font=theme.FONT_SMALL, bg="#303133", fg="#ffffff",
                             justify=tk.LEFT, anchor="w", padx=10, pady=6, wraplength=760)
            label.pack()
            self._tip, self._tip_label = tip, label
        self._tip_label.configure(text=text)
        self._tip.wm_geometry(f"+{x_root + 14}+{y_root + 20}")
        self._tip.deiconify()
        self._tip.lift()

    def _hide_tip(self, keep_line: bool = False) -> None:
        if not keep_line:
            self._tip_line = -1
        if self._tip is not None:
            self._tip.withdraw()

    def destroy(self) -> None:
        # 气泡是独立 Toplevel，表没了它还会孤零零地飘在屏幕上
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None
        super().destroy()

    def _at_bottom(self) -> bool:
        top, bottom = self._text.yview()
        return bottom > 0.999 or top == bottom  # 内容没占满时也算在底部
