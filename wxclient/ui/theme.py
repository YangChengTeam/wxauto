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

# 状态色
GREEN = "#22c55e"
ORANGE = "#f59e0b"
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
