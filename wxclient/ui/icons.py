"""应用图标：exe、桌面快捷方式、任务栏、右下角托盘**用同一份**。

图标取自 `design/laoxiaozhi-icon-set/`（劳小智正式图标套件，和网站后台同一个），
导出成 `assets/app.ico`（多尺寸，Windows 自己按 DPI 挑）+ `assets/app-256.png`
（托盘要的是 PIL 图像，直接读 PNG 最省事）。

托盘那颗**状态色**不能丢——它是唯一能一眼看出「服务在不在跑」的地方。所以托盘图标 =
品牌图标 + 右下角一个小圆角标，而不是把品牌图标换成一个纯色圆点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .service import ServiceState

ASSETS = Path(__file__).with_name("assets")
ICO_PATH = ASSETS / "app.ico"
PNG_PATH = ASSETS / "app-256.png"

_DOT_RUNNING = (26, 127, 55)
_DOT_STOPPED = (87, 96, 106)
_DOT_FAILED = (207, 34, 46)


def status_color(state: ServiceState) -> tuple[int, int, int]:
    if state is ServiceState.RUNNING:
        return _DOT_RUNNING
    if state is ServiceState.FAILED:
        return _DOT_FAILED
    return _DOT_STOPPED


def ico_path() -> str | None:
    """给 tkinter 的 ``iconbitmap`` / PyInstaller 的 ``icon=`` 用。装歪了就返回 None。"""
    return str(ICO_PATH) if ICO_PATH.exists() else None


def build_icon_image(state: ServiceState, size: int = 64) -> Any:
    """托盘图标：品牌图标 + 右下角状态角标。图标文件缺了就退回旧的纯色圆点。"""
    from PIL import Image, ImageDraw

    if PNG_PATH.exists():
        image = Image.open(PNG_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
    else:
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((2, 2, size - 2, size - 2), fill=(13, 17, 23, 255))

    draw = ImageDraw.Draw(image)
    # 角标压在右下角，直径约 1/3——16px 的托盘里还看得清，又不糊住品牌图形
    d = max(6, size // 3)
    box = (size - d - 1, size - d - 1, size - 1, size - 1)
    draw.ellipse(box, fill=(255, 255, 255, 255))          # 白圈让角标在任何底色上都分得开
    inset = max(1, d // 6)
    draw.ellipse(
        (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset),
        fill=(*status_color(state), 255),
    )
    return image


def logo_photo(master: Any, size: int) -> Any | None:
    """品牌图标缩到 ``size`` 见方，给界面里的小图标位用（弹框标题、以后别处也能用）。

    和 `cover_photo` 一样走 PIL：`PhotoImage.subsample` 只能整数倍，256 缩到 24 缩不出来
    ——要么 32（差一圈）要么糊。返回 None 表示这台机器上取不到图，调用方**只放标题**，
    别留一块空白占位（那看起来像图裂了）。
    """
    if size <= 0 or not PNG_PATH.exists():
        return None
    try:
        from PIL import Image, ImageTk
    except ImportError:
        return None
    try:
        image = Image.open(PNG_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(image, master=master)
    except Exception:  # noqa: BLE001 - 一个图标而已，不该让弹框弹不出来
        return None


PANEL_BG_PATH = ASSETS / "panel-bg.png"


def cover_photo(master: Any, path: Path, width: int, height: int) -> Any | None:
    """按 CSS ``background-size: cover`` 的规矩把图铺满一块区域：等比放大到刚好盖住，
    多出来的居中裁掉。返回 tk 用的 PhotoImage；铺不了就返回 None（调用方退回纯色底）。

    ★ 为什么必须运行时算：登录页左栏（约 590x720）和侧栏（300x高）宽高比差得远，
    预先切好一张贴上去，要么拉不满、要么只看得到构图的一角——用户 2026-08-15 反馈的
    「背景有问题，应使用 background-size: cover」正是这个。

    用 PIL 而不是 tk 自带的缩放：``PhotoImage.zoom/subsample`` 只能整数倍，缩出来
    要么糊要么跳。PIL 本来就在依赖里（托盘图标、pywechat 都用），而且这段发生在登录页
    建出来之后，不在「双击到看见第一屏」那条要命的路径上（见 splash.py）。
    """
    if width <= 1 or height <= 1 or not path.exists():
        return None
    try:
        from PIL import Image, ImageTk
    except ImportError:
        return None
    try:
        source = Image.open(path).convert("RGB")
        scale = max(width / source.width, height / source.height)
        size = (max(width, round(source.width * scale)), max(height, round(source.height * scale)))
        resized = source.resize(size, Image.LANCZOS)
        left, top = (resized.width - width) // 2, (resized.height - height) // 2
        return ImageTk.PhotoImage(resized.crop((left, top, left + width, top + height)),
                                  master=master)
    except Exception:  # noqa: BLE001 - 背景铺不上不该让人登不进去
        return None


def bot_avatar_photo(master: Any, size: int = 34) -> Any | None:
    """AI 那一侧的头像：一张圆脸机器人（照 `docs/chat.png`）。

    ★ **别拿品牌图标当头像。** 2026-08-17 真机截图里就是这个：`logo_photo` 是细线条的
    橙色标志，缩到 26px 只剩几道红杠，用户第一眼说的是「图像是红线」。品牌图标是给
    托盘/任务栏那种「认应用」的位置用的，头像要的是「认说话的人」。

    画而不是切图：一张 PNG 只对一个尺寸清楚，头像以后可能跟着 DPI 变。4 倍超采样再缩，
    tk 自己画的圆在 34px 上锯齿很明显。取不到 PIL 就返回 None，调用方退回文字头像。
    """
    if size <= 0:
        return None
    try:
        from PIL import Image, ImageDraw, ImageTk
    except ImportError:
        return None
    try:
        ss = 4
        edge = (154, 161, 171, 255)   # 描边：比正文浅，不跟文字抢
        ink = (61, 68, 77, 255)       # 五官
        box = size * ss
        image = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        u = box / 100                 # 百分比坐标，换尺寸不用重算
        draw.ellipse((3 * u, 3 * u, 97 * u, 97 * u), outline=edge, width=max(1, int(4 * u)))
        draw.line((50 * u, 22 * u, 50 * u, 32 * u), fill=ink, width=max(1, int(4 * u)))
        draw.ellipse((45 * u, 15 * u, 55 * u, 25 * u), fill=ink)          # 天线
        draw.ellipse((30 * u, 40 * u, 42 * u, 52 * u), fill=ink)          # 左眼
        draw.ellipse((58 * u, 40 * u, 70 * u, 52 * u), fill=ink)          # 右眼
        draw.arc((34 * u, 50 * u, 66 * u, 74 * u), start=20, end=160,
                 fill=ink, width=max(1, int(4 * u)))                      # 嘴
        return ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS), master=master)
    except Exception:  # noqa: BLE001 - 一个头像而已，不该让整页渲不出来
        return None
