"""界面配色与字号。所有窗口共用一份，改风格只改这里。

取自 `docs/wxui.png` 设计稿：左侧深色状态栏 + 右侧白色内容区。
"""

from __future__ import annotations

# 左侧状态栏
SIDEBAR_BG = "#1c1c1c"
SIDEBAR_LINE = "#333333"
SIDEBAR_TITLE = "#ffffff"
SIDEBAR_TEXT = "#e5e5e5"
SIDEBAR_MUTED = "#8b8b8b"
SIDEBAR_ACTIVE_BG = "#2a2a2a"

# 右侧内容区
CONTENT_BG = "#ffffff"
ROW_ALT_BG = "#fafafa"
ROW_LINE = "#ededed"
TEXT = "#333333"
TEXT_MUTED = "#6b7280"
FOOTER_BG = "#f7f7f7"

# 品牌色：取自 app 图标那颗橙（`assets/app-256.png`）。启动动画的转环用它——
# 用状态绿会让人以为「已经在跑了」，而那一刻服务其实还没起来。
BRAND = "#f97316"

# 状态色。绿色一族取自设计稿本身（`docs/login.png` 的输入框聚焦边 / 登录按钮，
# `docs/main.png` 的实时点），别再自己调——差一档人眼就看得出来跟稿子不是一套。
GREEN = "#1eb146"         # 主绿：聚焦边框、选中态、INFO 级别
GREEN_DEEP = "#019a3c"    # 实心绿：登录按钮底色
GREEN_BRIGHT = "#06d356"  # 亮绿：状态点、「实时更新」那颗点
ORANGE = "#fd940d"
RED = "#ef4444"
GRAY = "#9ca3af"

# 字体
UI = "Microsoft YaHei UI"
MONO = "Consolas"

FONT_TITLE = (UI, 13, "bold")
FONT_STATUS = (UI, 26, "bold")
FONT_HEADING = (UI, 14, "bold")
FONT_BODY = (UI, 10)
FONT_SMALL = (UI, 9)
FONT_LOG = (MONO, 9)
FONT_TABLE_HEAD = (UI, 9)

# 登录页（`docs/login.png`）：左边深色品牌面板 + 右边白色表单
PANEL_BG = "#1c1c1c"          # 和背景图 `assets/panel-bg.png` 的底色对齐，接缝看不出来
PANEL_SUBTLE = "#9aa0a6"
FIELD_BG = "#ffffff"
FIELD_LINE = "#d6d6d8"
PILL_BG = "#f2f3f5"

FONT_HERO = (UI, 30, "bold")
FONT_LOGIN_TITLE = (UI, 22, "bold")
FONT_INPUT = (UI, 11)
FONT_BUTTON = (UI, 12, "bold")

# 顶部告警条（`docs/main.png` 里那条橙色的）
BANNER_BG = "#fff7ed"

# 日志表（`docs/main.png`）：表头一条细线，警告/错误整行铺浅色底 + 左边一根竖色条
TABLE_HEAD_LINE = "#e4e3e3"
TABLE_ROW_LINE = "#ebebeb"   # 行与行之间那条：只有 1px，太淡就看不见了
WARN_ROW_BG = "#fdf7ee"
ERROR_ROW_BG = "#fef2f2"
