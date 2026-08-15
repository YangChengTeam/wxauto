"""几个界面上反复出现的小控件。

放这儿的标准只有一条：**同一个东西在两个页面上都要**。登录页和配置中心都要「灰字占位」
和「聚焦变绿的输入框」，配置中心还要设计稿上那种绿色胶囊开关和描边按钮——各写一份迟早
写成两个样子，所以收在一处。

样式全部取自 `docs/login.png` / `docs/setting.png`，改风格只改 `theme.py` 和这里。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from . import theme


def attach_placeholder(entry: tk.Entry, var: tk.StringVar, text: str,
                       *, font: tuple = theme.FONT_INPUT) -> tk.Label:
    """灰字占位。

    ★ 用一个**盖在上面的 Label**，不往 StringVar 里塞占位文字——塞进去的话，用户不点
    这一格直接提交，交上去的值就成了「用于换票，可选」这句提示本身。这种坑一旦埋下，
    表现是「明明没填却被当成填了」，查起来极贵。
    """
    hint = tk.Label(entry.master, text=text, font=font,
                    bg=entry.cget("bg"), fg=theme.TEXT_MUTED)
    hint.bind("<Button-1>", lambda _e: entry.focus_set())

    def sync(*_args: object) -> None:
        try:
            if var.get() or entry.focus_get() is entry:
                hint.place_forget()
            else:
                hint.place(in_=entry, x=1, rely=0.5, anchor="w")
        except (tk.TclError, KeyError):
            pass  # 控件正在销毁

    var.trace_add("write", lambda *_: sync())
    entry.bind("<FocusIn>", lambda _e: sync(), add="+")
    entry.bind("<FocusOut>", lambda _e: sync(), add="+")
    entry.after(0, sync)
    return hint


def focus_ring(box: tk.Frame, entry: tk.Entry) -> None:
    """聚焦时输入框描一圈绿边——设计稿上「当前在填哪一格」就是靠它标出来的。

    边框画在**外面那个 Frame 的 highlight** 上：Entry 自己的 highlight 会占掉内容区，
    文字就贴着框了。
    """
    entry.bind("<FocusIn>", lambda _e: box.configure(highlightbackground=theme.GREEN,
                                                     highlightcolor=theme.GREEN), add="+")
    entry.bind("<FocusOut>", lambda _e: box.configure(highlightbackground=theme.FIELD_LINE,
                                                      highlightcolor=theme.FIELD_LINE), add="+")


def ghost_button(parent: tk.Misc, text: str, command: Callable[[], None],
                 *, bg: str = theme.CONTENT_BG, enabled: bool = True) -> tk.Label:
    """描边按钮（白底 + 绿框 + 绿字）。设计稿上「测试连接」「选择文件」「仅保存」都是这一款。

    用 Label 而不是 Button：tk 的 Button 在 Windows 上自带一圈没法关掉的立体边，
    和设计稿那种扁平描边对不上。

    ``enabled=False`` 是「预留位」：灰框灰字、鼠标不变手型，**但点击照样会调 command**——
    留给它去说清「这个还没接上」。摆一个点了什么都不发生的按钮，比没有还糟。
    """
    color = theme.GREEN if enabled else theme.TEXT_MUTED
    edge = theme.GREEN if enabled else theme.ROW_LINE
    button = tk.Label(parent, text=text, font=theme.FONT_SMALL, bg=bg, fg=color,
                      padx=14, pady=6, cursor="hand2" if enabled else "arrow",
                      highlightthickness=1, highlightbackground=edge,
                      highlightcolor=edge, bd=0)
    button.bind("<Button-1>", lambda _e: command())
    return button


class ToggleSwitch(tk.Canvas):
    """绿色胶囊开关（`docs/setting.png` 的「开机即启动服务」那种）。

    tk 没有开关控件，Checkbutton 在 Windows 上是个方框，和设计稿差得远。这里用 Canvas
    画：两个圆 + 一个矩形拼出胶囊，白色小圆点当把手。值仍然挂在传进来的 ``BooleanVar`` 上，
    所以读写方式和 Checkbutton 完全一样，页面那边不用为它特判。
    """

    WIDTH = 44
    HEIGHT = 22
    OFF_COLOR = "#d0d3d6"

    def __init__(self, parent: tk.Misc, variable: tk.BooleanVar, *,
                 bg: str = theme.CONTENT_BG) -> None:
        super().__init__(parent, width=self.WIDTH, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._var = variable
        self.bind("<Button-1>", lambda _e: self.toggle())
        variable.trace_add("write", lambda *_: self._render())
        self._render()

    def toggle(self) -> None:
        self._var.set(not bool(self._var.get()))

    def _render(self) -> None:
        try:
            self.delete("all")
        except tk.TclError:
            return  # 正在销毁
        on = bool(self._var.get())
        color = theme.GREEN if on else self.OFF_COLOR
        radius = self.HEIGHT // 2
        self.create_oval(0, 0, self.HEIGHT, self.HEIGHT, fill=color, outline=color)
        self.create_oval(self.WIDTH - self.HEIGHT, 0, self.WIDTH, self.HEIGHT,
                         fill=color, outline=color)
        self.create_rectangle(radius, 0, self.WIDTH - radius, self.HEIGHT,
                              fill=color, outline=color)
        knob = self.WIDTH - self.HEIGHT + 2 if on else 2
        self.create_oval(knob, 2, knob + self.HEIGHT - 4, self.HEIGHT - 2,
                         fill="#ffffff", outline="#ffffff")
