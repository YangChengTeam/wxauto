"""生成 `wxclient/ui/assets/splash.png`——**冻结包**的第一屏。

    python packaging/make_splash.py

★ 为什么还要一张静态图：`wxclient/ui/splash.py` 那个会动的启动窗是 tkinter 画的，
而 tkinter 要等 Python 起来才有。冻结包里「双击 → Python 起来」这段（实测 1~2 秒）
连 tk 都还不存在，只有 PyInstaller 自带的 Splash 能顶上——它要的就是一张图。

所以两段接力：这张静态图先出来（PyInstaller 画），Python 一起来就换成会动的那个
（`ui/splash.py` 一初始化就把静态图关掉）。两张长得一样，看上去是同一个东西在动。

图是生成的、不是手画的：改配色只改 `theme.py`，重跑这个脚本即可。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw, ImageFont

from wxclient.ui import theme
from wxclient.ui.splash_style import (
    HEIGHT,
    LOGO_PX,
    RING_RADIUS,
    RING_WIDTH,
    WIDTH,
)

OUT = REPO / "wxclient" / "ui" / "assets" / "splash.png"
LOGO = REPO / "wxclient" / "ui" / "assets" / "app-256.png"

#: 找一个能写中文的字体。Windows 上是雅黑，打包机（Linux/CI）上退回 Noto。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _font(size: int) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def build() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), theme.SIDEBAR_BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=theme.SIDEBAR_LINE, width=1)

    cx, cy = WIDTH // 2, 84
    box = (cx - RING_RADIUS, cy - RING_RADIUS, cx + RING_RADIUS, cy + RING_RADIUS)
    draw.ellipse(box, outline=theme.SIDEBAR_ACTIVE_BG, width=RING_WIDTH)
    # 静态的那一段「彗头」：动图里它在转，这里定格在右上角
    draw.arc(box, start=-70, end=20, fill=theme.BRAND, width=RING_WIDTH)

    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA").resize((LOGO_PX, LOGO_PX), Image.LANCZOS)
        image.paste(logo, (cx - LOGO_PX // 2, cy - LOGO_PX // 2), logo)

    draw.text((cx, 152), "劳小智微信助手", font=_font(16),
              fill=theme.SIDEBAR_TITLE, anchor="mm")
    return image


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    build().save(OUT)
    print(f"已生成 {OUT.relative_to(REPO)}（{OUT.stat().st_size / 1024:.1f} KB）")
