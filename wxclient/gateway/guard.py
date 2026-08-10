"""确定性守门——**不依赖模型自觉**的那一层。

三件事：拒绝识别、频次上限、工作时间窗。全是纯规则，模型判断只作补充。

为什么拒绝识别要放在网关侧而不是只交给 AI 端（``CLAUDE.md`` §5.2）：
漏判一次拒绝的代价是继续骚扰一个明确说过「别联系我」的人。这个代价不能押在
一次模型调用上——网关先用确定性词表拦一道，AI 端的语义判断作为第二道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time

# 明确的拒绝表达。宁可多拦，不可漏拦——被误拦的后果只是少发一条消息。
_REJECT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"别(再)?(加|联系|发|骚扰|打扰)",
        r"不要(再)?(加|联系|发|骚扰|打扰)",
        r"别烦我",
        r"删了?我",
        r"把我删",
        r"拉黑",
        r"举报",
        r"投诉你",
        r"报警",
        r"骚扰",
        r"退订",
        r"停止(发送|推送|联系)",
        r"取消关注",
        r"没兴趣",
        r"不感兴趣",
        r"不需要[了，。!！]*$",
        r"不找(工作|活)",
        r"已经?(找到|上班|入职)",
        r"stop\b",
        r"unsubscribe",
    )
)

# 「不需要」这类词单独出现才算拒绝；出现在「不需要经验吗」这种问句里不算。
_QUESTION_MARKS = ("?", "？")


def looks_like_rejection(text: str) -> bool:
    """确定性拒绝判定。命中即应停止对该联系人的一切自动化动作。"""
    if not text:
        return False
    normalized = text.strip()
    # 疑问句里的否定词多半是在问条件，不是在拒绝（「不需要经验吗」）。
    if normalized.endswith(_QUESTION_MARKS):
        return False
    return any(pattern.search(normalized) for pattern in _REJECT_PATTERNS)


def parse_active_hours(window: str) -> tuple[time, time]:
    """解析 ``"09:00-21:00"``。格式不对直接抛——配置错了就别启动。"""
    try:
        start_raw, end_raw = window.split("-", 1)
        start = time.fromisoformat(start_raw.strip())
        end = time.fromisoformat(end_raw.strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"active_hours 格式应为 HH:MM-HH:MM，收到 {window!r}") from exc
    return start, end


def within_active_hours(now: datetime, window: str) -> bool:
    """是否在允许发送的时间窗内。跨午夜（如 22:00-02:00）也支持。"""
    start, end = parse_active_hours(window)
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


@dataclass(frozen=True)
class GuardDecision:
    """守门结论。``allowed=False`` 时 ``reason`` 说明为什么，供留痕。"""

    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> GuardDecision:
        return cls(True)

    @classmethod
    def deny(cls, reason: str) -> GuardDecision:
        return cls(False, reason)


class OutboundGuard:
    """出站守门：任何一条要发出去的消息都要过这里。"""

    def __init__(
        self,
        store,  # GatewayStore —— 避免循环导入，用鸭子类型
        *,
        max_sends_per_hour: int = 30,
        active_hours: str = "09:00-21:00",
    ) -> None:
        self._store = store
        self._max_per_hour = max_sends_per_hour
        self._active_hours = active_hours
        parse_active_hours(active_hours)  # 提前校验，别等到要发消息才炸

    def check(self, account_id: str, peer_id: str, *, now: datetime) -> GuardDecision:
        if self._store.is_blacklisted(account_id, peer_id):
            return GuardDecision.deny("该联系人已在黑名单")
        if not within_active_hours(now, self._active_hours):
            return GuardDecision.deny(f"不在允许发送的时间窗 {self._active_hours}")
        sent = self._store.sends_in_last_hour(account_id, now)
        if sent >= self._max_per_hour:
            return GuardDecision.deny(f"该账号一小时内已发送 {sent} 条，达到上限")
        return GuardDecision.allow()
