"""自己画的弹框（替掉 `tkinter.messagebox`）。

★ 2026-08-16 用户要求：「弹框改成自定义的和界面视觉风格一致，在这个窗口的中心」。

系统弹框的问题不是丑：它是**另一个程序的东西**——灰底、宋体、系统按钮、屏幕正中蹦出来，
和这套界面（白底、雅黑、绿色描边按钮）没有一处对得上。人正在这个窗口里做事，冷不丁弹出
一个长得不一样的框，第一反应是「这是谁弹的」。所以这里自己画一个，并且**落在触发它的
那个窗口的正中**，视线不用跳。

三条硬要求，改的时候别丢：

* **模态**：`grab_set()` + `wait_window()`。问「要不要顶掉另一台电脑」这种问题，答案没出来
  之前不能让人接着点别的。
* **键盘能用**：回车＝确认，Esc＝取消。自画标题栏意味着**没有右上角那个 ×**，Esc 就是
  唯一的「我不想做这件事」的快捷路，丢了它这个框就只能用鼠标关。
  为此 `show()` 必须 `focus_force()`：`overrideredirect` 的窗口不会自动拿到键盘焦点，
  拿不到的话上面两个绑定一次都不会触发。
* **默认值一律取「不做那件事」**：弹框问的都是有后果的事（顶掉别人的登录），
  任何一种「没明确点确定」的关闭方式都得回 False。
"""

from __future__ import annotations

import tkinter as tk

from . import theme

#: 弹框宽度。文字靠 wraplength 折行，高度由内容撑。
WIDTH = 460
#: 正文最多折到多宽（左右各留 28 的内边距）。
WRAP = WIDTH - 56


class _Dialog:
    """一个模态小窗。别直接用，走下面的 `ask()` / `tell()`。"""

    def __init__(self, parent: tk.Misc, title: str, message: str, *,
                 confirm: str, cancel: str | None, tone: str) -> None:
        self.parent = parent.winfo_toplevel()
        self.result = False

        self.top = tk.Toplevel(self.parent)
        self.top.withdraw()  # 先藏着：摆好位置再露面，否则会看见它从左上角跳过来
        self.top.transient(self.parent)
        self.top.resizable(False, False)
        self.top.configure(bg=theme.ROW_LINE)  # 外面这一圈就是 1px 边框
        # 自己画标题栏：系统标题栏是灰的，和这套白底界面接不上。
        self.top.overrideredirect(True)

        body = tk.Frame(self.top, bg=theme.CONTENT_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        head = tk.Frame(body, bg=theme.CONTENT_BG)
        head.pack(fill=tk.X, padx=28, pady=(22, 0))
        # 一条竖色条当「这是什么级别的事」的标记：出错是红的，其余是绿的。
        tk.Frame(head, bg=theme.RED if tone == "error" else theme.GREEN,
                 width=3, height=18).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(head, text=title, font=theme.FONT_HEADING, bg=theme.CONTENT_BG,
                 fg=theme.TEXT, anchor="w").pack(side=tk.LEFT)

        tk.Label(body, text=message, font=theme.FONT_BODY, bg=theme.CONTENT_BG,
                 fg=theme.TEXT_MUTED, justify=tk.LEFT, anchor="w",
                 wraplength=WRAP).pack(fill=tk.X, padx=28, pady=(14, 22))

        foot = tk.Frame(body, bg=theme.FOOTER_BG)
        foot.pack(fill=tk.X)
        row = tk.Frame(foot, bg=theme.FOOTER_BG)
        row.pack(anchor="e", padx=20, pady=14)
        # 确认按钮实心绿、取消按钮描边——和登录页、配置中心那两处同一套语言。
        self.confirm = tk.Label(row, text=confirm, font=theme.FONT_SMALL,
                                bg=theme.GREEN_DEEP, fg="#ffffff", padx=18, pady=7,
                                cursor="hand2")
        self.confirm.bind("<Button-1>", lambda _e: self._close(True))
        self.confirm.pack(side=tk.RIGHT)
        self.cancel = None
        if cancel is not None:
            self.cancel = tk.Label(row, text=cancel, font=theme.FONT_SMALL,
                                   bg=theme.FOOTER_BG, fg=theme.TEXT, padx=18, pady=6,
                                   cursor="hand2", highlightthickness=1,
                                   highlightbackground=theme.FIELD_LINE,
                                   highlightcolor=theme.FIELD_LINE, bd=0)
            self.cancel.bind("<Button-1>", lambda _e: self._close(False))
            self.cancel.pack(side=tk.RIGHT, padx=(0, 10))

        self.top.bind("<Return>", lambda _e: self._close(True))
        self.top.bind("<Escape>", lambda _e: self._close(False))
        self.top.protocol("WM_DELETE_WINDOW", lambda: self._close(False))

    def center(self) -> None:
        """摆到**触发它的那个窗口**正中，不是屏幕正中。

        窗口缩在托盘里（或还没映射）时退回屏幕正中——那种情况下「父窗口的中心」是个
        1×1 的假坐标，照它摆会飞到左上角去。
        """
        self.top.update_idletasks()
        width = self.top.winfo_reqwidth()
        height = self.top.winfo_reqheight()
        if self.parent.winfo_ismapped() and self.parent.winfo_width() > 1:
            x = self.parent.winfo_rootx() + (self.parent.winfo_width() - width) // 2
            y = self.parent.winfo_rooty() + (self.parent.winfo_height() - height) // 2
        else:
            x = (self.top.winfo_screenwidth() - width) // 2
            y = (self.top.winfo_screenheight() - height) // 2
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")

    def show(self) -> bool:
        self.center()
        self.top.deiconify()
        self.top.lift()
        self.top.grab_set()  # 模态：答案出来之前别让人去点别的
        # ★ 必须 force：overrideredirect 的窗口不会自动拿到键盘焦点，
        # 不 force 的话回车和 Esc 那两条绑定一次都不会触发。
        self.top.focus_force()
        self.confirm.focus_set()
        self.top.wait_window()
        return self.result

    def _close(self, result: bool) -> None:
        self.result = result
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()


def ask(parent: tk.Misc, title: str, message: str, *,
        confirm: str = "确定", cancel: str = "取消") -> bool:
    """问一句要不要。关窗口 / Esc / 点取消都回 False。"""
    return _Dialog(parent, title, message, confirm=confirm, cancel=cancel, tone="info").show()


def tell(parent: tk.Misc, title: str, message: str, *,
         ok: str = "知道了", tone: str = "info") -> None:
    """只是告诉一声。``tone="error"`` 时标题旁那条色条变红。"""
    _Dialog(parent, title, message, confirm=ok, cancel=None, tone=tone).show()
