"""启动动画：双击到主窗口出现之间，屏幕上不能什么都没有。

★ 为什么要有它：冻结包冷启动实测 4.5 秒（优化前 12.3 秒），这几秒里屏幕上一片空白——
用户的第一反应是「双击了没反应」，于是再双击一次（单实例守卫会挡住，但体验已经坏了）。
一个立刻出现的动画把「在启动」这件事说清楚，比再快一秒有用得多。

★ 为什么不用图片轮播/GIF：多一份素材、多一份 PIL 解码开销，而启动动画恰恰要在
「什么都还没 import」的时刻就出来。这里只用 tkinter 画布画：品牌深色底 + 图标 +
一圈品牌绿的彗尾环，一行阶段文字。素材只有本来就有的 `assets/app-256.png`
（Tk 8.6 自带 PNG 解码，不经 PIL）。

配色全部取自 `theme.py`，和主窗口左侧深色栏是同一套——不是另画一个风格。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

from . import theme
from .splash_style import (
    COMET_SEGMENTS,
    FADE_MS,
    FADE_STEPS,
    FRAME_MS,
    HEIGHT,
    LOGO_PX,
    RING_RADIUS,
    RING_WIDTH,
    STEP_DEGREES,
    WIDTH,
    comet_colors,
)


def _close_frozen_splash() -> None:
    """把 PyInstaller 那张静态首屏关掉——会动的这个已经接手了。

    `pyi_splash` 只在冻结包里存在，源码跑没有这个模块，import 失败是正常情况。
    """
    try:
        import pyi_splash  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        pyi_splash.close()
    except Exception:  # noqa: BLE001 - 关不掉也不该拦住启动
        pass


class Splash:
    """无边框启动窗。``stage()`` 换文字，``close()`` 淡出销毁。

    可以从任何线程调用（内部都走 ``after`` 回 tk 线程）——网关是在后台线程上装配的，
    阶段提示正是从那边来的。
    """

    def __init__(self, root: tk.Tk, *, icon_png: Path | None = None) -> None:
        self._root = root
        self._closed = False
        self._angle = 0
        self._colors = comet_colors()
        self._photo: tk.PhotoImage | None = None

        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)  # 无边框：启动图不该有标题栏和关闭按钮
        self._win.configure(bg=theme.SIDEBAR_BG)
        self._center()
        try:
            self._win.attributes("-topmost", True)
        except tk.TclError:  # pragma: no cover - 少数环境不支持
            pass

        self._canvas = tk.Canvas(
            self._win, width=WIDTH, height=HEIGHT,
            bg=theme.SIDEBAR_BG, highlightthickness=0,
        )
        self._canvas.pack(fill="both", expand=True)
        # 一圈细边：纯深色块贴在浅色桌面上会糊成一片，描一下边才像个卡片
        self._canvas.create_rectangle(
            0, 0, WIDTH - 1, HEIGHT - 1, outline=theme.SIDEBAR_LINE, width=1,
        )

        cx, cy = WIDTH // 2, 84
        self._canvas.create_oval(
            cx - RING_RADIUS, cy - RING_RADIUS, cx + RING_RADIUS, cy + RING_RADIUS,
            outline=theme.SIDEBAR_ACTIVE_BG, width=RING_WIDTH,
        )
        self._arcs = [
            self._canvas.create_arc(
                cx - RING_RADIUS, cy - RING_RADIUS, cx + RING_RADIUS, cy + RING_RADIUS,
                start=0, extent=360 // COMET_SEGMENTS - 2, style="arc",
                outline=color, width=RING_WIDTH,
            )
            for color in self._colors
        ]
        self._place_icon(icon_png, cx, cy)

        self._canvas.create_text(
            cx, 152, text="劳小智微信助手",
            fill=theme.SIDEBAR_TITLE, font=(theme.UI, 15, "bold"),
        )
        self._stage_id = self._canvas.create_text(
            cx, 182, text="正在启动…",
            fill=theme.SIDEBAR_MUTED, font=theme.FONT_SMALL,
        )
        self._tick()
        _close_frozen_splash()

    # ────────────────────────── 对外 ──────────────────────────

    def stage(self, text: str) -> None:
        """换一行阶段提示（「正在装配网关…」）。已经关掉了就当没说。"""
        if self._closed:
            return
        self._root.after(0, lambda: self._set_stage(text))

    def close(self) -> None:
        """淡出并销毁。重复调用安全——退出路径上不止一处会喊它。"""
        if self._closed:
            return
        self._closed = True
        self._root.after(0, lambda: self._fade(FADE_STEPS))

    # ────────────────────────── 内部 ──────────────────────────

    def _center(self) -> None:
        screen_w = self._win.winfo_screenwidth()
        screen_h = self._win.winfo_screenheight()
        x = max(0, (screen_w - WIDTH) // 2)
        # 略高于正中：人的视觉重心比几何中心高一点，正中会显得偏下
        y = max(0, int(screen_h * 0.42) - HEIGHT // 2)
        self._win.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")

    def _place_icon(self, icon_png: Path | None, cx: int, cy: int) -> None:
        """图标放环心。读不出来就算了——启动动画不值得为一张图挡住启动。"""
        if icon_png is None or not icon_png.exists():
            return
        try:
            image = tk.PhotoImage(file=str(icon_png))   # Tk 8.6 自带 PNG，不用 PIL
            factor = max(1, image.width() // LOGO_PX)   # 256 → 64 这一档（整数倍缩）
            self._photo = image.subsample(factor, factor)
            self._canvas.create_image(cx, cy, image=self._photo)
        except tk.TclError:
            self._photo = None

    def _set_stage(self, text: str) -> None:
        try:
            self._canvas.itemconfigure(self._stage_id, text=text)
        except tk.TclError:
            pass

    def _tick(self) -> None:
        """转一格。窗口没了就自己停——不要在销毁后还排下一帧。"""
        if self._closed:
            return
        try:
            for index, arc in enumerate(self._arcs):
                self._canvas.itemconfig(
                    arc, start=(self._angle + index * (360 // COMET_SEGMENTS)) % 360
                )
            self._angle = (self._angle + STEP_DEGREES) % 360
            self._root.after(FRAME_MS, self._tick)
        except tk.TclError:
            self._closed = True

    def _fade(self, left: int) -> None:
        try:
            if left <= 0:
                self._win.destroy()
                return
            self._win.attributes("-alpha", left / FADE_STEPS)
            self._root.after(FADE_MS, lambda: self._fade(left - 1))
        except tk.TclError:
            pass
