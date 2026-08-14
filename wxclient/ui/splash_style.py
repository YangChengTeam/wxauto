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

#: 整窗不透明度（1.0 = 实心）。稍微透一点，深色块压在桌面上没那么硬。
#: 别再往下调：白字压在花桌面上会开始发糊，而它统共只活几秒，看不清就白做了。
#: 只对会动的这个 tk 启动窗生效——冻结包里 PyInstaller 那张静态首屏没有透明度可调。
OPACITY = 0.92


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
