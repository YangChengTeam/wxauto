"""把一行渲染好的日志拆成 时间 / 级别 / 角色 / 正文 四段，供界面按列显示。

拆不开的行（多行异常栈、第三方直接 print 的东西）原样当正文，绝不丢——
界面上少一行日志比多一行难查得多。

**角色那一段是可选的**：老日志文件里没有它，第三方库直接写进来的行也没有。认不出来就
留空，不去猜。判据同样是确定性的——第二个方括号里必须**正好**是 `logrole.ROLES` 里的
一个词才算角色，否则当正文（正文本来就可能以 `[` 开头，猜错会把半句话吃掉）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .. import logrole

# 形如：2026-08-12 16:38:47 [信息] [AI端] 网关启动，accounts=1
#      （角色那一段可能没有：老日志、第三方库直接写的行）
_PATTERN = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[(?P<level>[^\]]+)\]"
    r"(?:\s+\[(?P<role>[^\]]+)\])?\s?(?P<message>.*)$"
)

# 中文级别标签（ChineseLogRenderer 出的）→ 界面上的短标
_LEVELS = {
    "调试": "DEBUG",
    "信息": "INFO",
    "警告": "WARN",
    "错误": "ERROR",
    "严重": "FATAL",
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARN",
    "error": "ERROR",
    "critical": "FATAL",
}


#: 界面上的级别过滤档位：显示名 → 放行的严重程度。放这儿而不是放视图里，
#: 是因为它描述的是「严重程度怎么分档」，跟用什么控件画无关（也就能在无 GUI 的机器上测）。
LEVEL_FILTERS: tuple[tuple[str, frozenset[str]], ...] = (
    ("全部", frozenset({"debug", "info", "warn", "error"})),
    ("警告", frozenset({"warn", "error"})),
    ("错误", frozenset({"error"})),
)


@dataclass(frozen=True)
class LogLine:
    time: str
    level: str
    message: str
    #: 这行说的是跟谁打交道：微信 / AI端 / 业务后端 / 本机。认不出来是空串（老日志、
    #: 第三方库直接写进来的行），界面上那一格就留白——不猜。
    role: str = ""

    @property
    def severity(self) -> str:
        """给界面挑颜色用：info / warn / error / debug。"""
        if self.level in {"ERROR", "FATAL"}:
            return "error"
        if self.level == "WARN":
            return "warn"
        if self.level == "DEBUG":
            return "debug"
        return "info"


def parse(line: str) -> LogLine:
    match = _PATTERN.match(line.rstrip("\n"))
    if match is None:
        return LogLine(time="", level="", message=line.rstrip("\n"))
    level = match.group("level").strip()
    role = (match.group("role") or "").strip()
    message = match.group("message")
    if role and role not in logrole.ROLES:
        # 第二个方括号不是角色，那它就是正文的一部分——原样接回去，一个字都不能吞。
        message = f"[{role}] {message}"
        role = ""
    return LogLine(
        time=match.group("time"),
        level=_LEVELS.get(level, _LEVELS.get(level.lower(), level.upper())),
        message=message,
        role=role,
    )
