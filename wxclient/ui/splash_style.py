"""启动动画的尺寸与配色（**不 import tkinter**）。

分出来有两个实际理由：① 打包用的 `packaging/make_splash.py` 要照着同一套数画静态首屏，
但它跑在没有 tkinter 的环境里（Linux 打包机、CI）；② 这几个纯函数能在无图形环境里
直接测——`wxclient/ui/splash.py` 一 import 就要 tkinter，Linux 上根本进不去。
"""

from __future__ import annotations

from . import theme

#: 窗口尺寸。比常见的启动图小一圈：它只活几秒，占太大反而突兀。
WIDTH = 380
HEIGHT = 230

#: 图标画多大（环心那颗）。要比环的直径小一圈，不然把环压住了。
LOGO_PX = 56

#: 彗尾环：半径、线宽、尾巴由几段组成、每帧转多少度、多少毫秒一帧。
RING_RADIUS = 46
RING_WIDTH = 4
COMET_SEGMENTS = 12
STEP_DEGREES = 7
FRAME_MS = 40

#: 淡出：几帧、每帧多久。做完再销毁，免得「啪」一下消失。
FADE_STEPS = 6
FADE_MS = 25

#: 整窗不透明度。**必须是 1.0**：冻结包里 PyInstaller 那张静态首屏没有透明度可调，
#: 会动的这个只要不是 1.0，接力那一下就会「亮一下」。原来 0.92 是为了让深色块压在桌面上
#: 没那么硬，代价是每次启动都看得见一次跳变——不值（用户 2026-08-17 反馈体验不好）。
OPACITY = 1.0

#: 彗尾每一段张开多少度。留 2 度的缝，段与段之间才看得出是一节一节的。
COMET_EXTENT = 360 // COMET_SEGMENTS - 2


def blend(start: str, end: str, ratio: float) -> str:
    """两个 ``#rrggbb`` 之间取一个色。``ratio=0`` 给 start，``1`` 给 end。

    彗尾就是靠它从底色渐变到品牌绿——Tk 画布没有渐变，只能自己算每一段的颜色。
    """
    ratio = min(1.0, max(0.0, ratio))
    pairs = [(int(start[i:i + 2], 16), int(end[i:i + 2], 16)) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(a + (b - a) * ratio):02x}" for a, b in pairs)


def comet_colors(segments: int = COMET_SEGMENTS) -> list[str]:
    """彗尾每一段的颜色：头最亮（品牌绿），越往尾越接近底色。"""
    if segments <= 1:
        return [theme.BRAND]
    return [
        blend(theme.SIDEBAR_ACTIVE_BG, theme.BRAND, index / (segments - 1))
        for index in range(segments)
    ]


def comet_arcs(angle: int = 0) -> list[tuple[int, int, str]]:
    """彗尾每一段的 (起始角, 张角, 颜色)。角度按 **Tk 画布**的规矩：3 点钟为 0，逆时针。

    ★ tk 那个会动的启动窗和 PIL 画的静态首屏**照同一份数**画。分成两处各写各的，
    结果就是两张图长得不一样，接力那一下人眼一定看得出来——用户 2026-08-17 反馈的
    「第一帧是个实心的 loading，后面还要动一下才开始转」正是这么来的：
    静态图当时只画了一段 90° 的弧，动图画的是整圈渐变。
    """
    step = 360 // COMET_SEGMENTS
    return [((angle + index * step) % 360, COMET_EXTENT, color)
            for index, color in enumerate(comet_colors())]


def window_position(screen_width: int, screen_height: int) -> tuple[int, int]:
    """启动窗摆在哪。**屏幕正中**——PyInstaller 那张静态首屏就是这么摆的
    （它的 Tcl 模板里写死了居中），会动的这个只要摆在别处，接力那一下整块就会挪一下。

    原来这里是 ``0.42 * 屏高``（视觉重心比几何中心高一点更好看）。好看输给不跳。
    """
    return (max(0, (screen_width - WIDTH) // 2), max(0, (screen_height - HEIGHT) // 2))
