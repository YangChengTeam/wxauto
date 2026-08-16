"""生成安装向导的两张图（`packaging/assets/` 下的 BMP）。

    python packaging/make_wizard_images.py

Inno Setup 只认 BMP，而且**只认这两个位置**：左侧那条竖幅（欢迎页和完成页）
和右上角那枚小标（中间每一页）。默认那两张是 Inno 自带的蓝色抽象图——装谁都长一样，
一眼就是「拿模板打的包」。

图跟着 `wxclient/ui/theme.py` 和登录页那张底图走：装的时候看到的和装完打开看到的
是同一套配色、同一张背景。这不是好看不好看的问题——**同一个东西在两个地方长得不一样，
人会怀疑自己装错了。**

每张出三个尺寸：Windows 在 125% / 150% 缩放下会挑更大的那张。只给一张的表现是
高分屏上糊成一片，而那恰恰是新电脑的默认设置。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image, ImageDraw, ImageFont

from wxclient.ui import theme

OUT_DIR = REPO / "packaging" / "assets"
LOGO = REPO / "wxclient" / "ui" / "assets" / "app-256.png"
PANEL_BG = REPO / "wxclient" / "ui" / "assets" / "panel-bg.png"

#: 竖幅与小标的基准尺寸（Inno 的 96 DPI 基准）。倍数用于高 DPI。
LARGE_BASE = (164, 314)
SMALL_BASE = (55, 55)
SCALES = (1.0, 1.5, 2.0)

#: 底图压暗多少。原图是给登录页当背景的，直接拿来当竖幅会把字吃掉。
DIM = 0.55

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


def _cover(path: Path, width: int, height: int) -> Image.Image | None:
    """把底图按 cover 铺满（等比放大 + 居中裁剪），和登录页那块面板同一个算法。"""
    if not path.exists():
        return None
    image = Image.open(path).convert("RGB")
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(1, round(image.width * scale)),
                            max(1, round(image.height * scale))), Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def build_large(width: int, height: int) -> Image.Image:
    """左侧竖幅：底图压暗 + 图标 + 产品名 + 一句话说明。"""
    image = _cover(PANEL_BG, width, height)
    if image is None:
        image = Image.new("RGB", (width, height), theme.PANEL_BG)
    else:
        # 压暗：直接和底色混，比调亮度快也更好控——要的就是「字压得住」这一个效果。
        image = Image.blend(image, Image.new("RGB", (width, height), theme.PANEL_BG), 1 - DIM)

    draw = ImageDraw.Draw(image)
    k = width / LARGE_BASE[0]  # 所有尺寸按基准等比缩放，三张图长得一模一样

    logo_px = round(46 * k)
    if LOGO.exists():
        logo = Image.open(LOGO).convert("RGBA").resize((logo_px, logo_px), Image.LANCZOS)
        image.paste(logo, (round(22 * k), round(30 * k)), logo)

    draw.text((round(22 * k), round(96 * k)), "劳小智", font=_font(round(19 * k)),
              fill=theme.SIDEBAR_TITLE)
    draw.text((round(22 * k), round(122 * k)), "微信助手", font=_font(round(19 * k)),
              fill=theme.GREEN_BRIGHT)

    # 一条短横线 + 一句说明。竖幅上信息越少越稳——它是背景，不是说明书。
    draw.rectangle((round(22 * k), round(156 * k), round(52 * k), round(156 * k) + max(1, round(2 * k))),
                   fill=theme.GREEN_BRIGHT)
    for index, line in enumerate(("连接、监听与", "消息处理", "一处完成")):
        draw.text((round(22 * k), round((174 + index * 18) * k)), line,
                  font=_font(round(11 * k)), fill=theme.PANEL_SUBTLE)

    return image


def build_small(size: int) -> Image.Image:
    """右上角小标：深色底 + 图标。别放字——55px 上任何中文都是一团墨。"""
    image = Image.new("RGB", (size, size), theme.PANEL_BG)
    if LOGO.exists():
        px = round(size * 0.72)
        logo = Image.open(LOGO).convert("RGBA").resize((px, px), Image.LANCZOS)
        offset = (size - px) // 2
        image.paste(logo, (offset, offset), logo)
    return image


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for scale in SCALES:
        suffix = "" if scale == 1.0 else f"@{scale:g}x"

        large = build_large(round(LARGE_BASE[0] * scale), round(LARGE_BASE[1] * scale))
        large_path = OUT_DIR / f"wizard-large{suffix}.bmp"
        large.save(large_path)
        written.append(large_path)

        small = build_small(round(SMALL_BASE[0] * scale))
        small_path = OUT_DIR / f"wizard-small{suffix}.bmp"
        small.save(small_path)
        written.append(small_path)

    for path in written:
        print(f"已生成 {path.relative_to(REPO)}（{path.stat().st_size / 1024:.1f} KB）")


if __name__ == "__main__":
    main()
