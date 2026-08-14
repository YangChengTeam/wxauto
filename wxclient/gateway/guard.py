"""确定性守门——**不依赖模型自觉**的那一层。

三件事：拒绝识别、频次上限、工作时间窗。全是纯规则，模型判断只作补充。

为什么拒绝识别要放在网关侧而不是只交给 AI 端（``CLAUDE.md`` §5.2）：
漏判一次拒绝的代价是继续骚扰一个明确说过「别联系我」的人。这个代价不能押在
一次模型调用上——网关先用确定性词表拦一道，AI 端的语义判断作为第二道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

# 明确的**拒绝联系**表达——命中即永久停止对该联系人的一切自动化动作并拉黑。
# 宁可多拦，不可漏拦：被误拦的后果只是少发一条消息。
#
# ★ 这里只放「别联系我」这一档（需求文档 §7 的唯一硬性规则）。
#   「已经找到工作了」「暂时没兴趣」是**软收口**，不进这道闸——那种人以后还要触达，
#   永久拉黑是过度处置。软收口交给 AI 端（skill `wechat_reply` 的两档判定）：
#   它会礼貌收个尾、把漏斗标成 closed，且**不提案拉黑**。
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
        r"stop\b",
        r"unsubscribe",
    )
)

# 「不需要」这类词单独出现才算拒绝；出现在「不需要经验吗」这种问句里不算。
_QUESTION_MARKS = ("?", "？")


def looks_like_rejection(text: str) -> bool:
    """确定性**硬拒绝**判定。命中即应停止对该联系人的一切自动化动作并拉黑。

    软收口（「已经找到工作了」「没兴趣」）**不在这里判**——交给 AI 端礼貌收尾、不拉黑。
    """
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
    """是否在允许动作的时间窗内。跨午夜（如 22:00-02:00）也支持。

    ★ 比的是**本机墙上时间**。网关内部一律用 UTC 传时间，可「09:00-21:00」说的显然是
    专员那台机器上的九点到九点——直接拿 UTC 的 `.time()` 比，东八区就成了 17:00–次日 05:00：
    白天不发、半夜猛发，正好是风控最看不惯的样子。带时区的先换算到本地，naive 的按本地看待。
    """
    start, end = parse_active_hours(window)
    current = (now.astimezone() if now.tzinfo is not None else now).time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


# ────────────────────────── 分动作频次（072）──────────────────────────
#
# 需求文档 §13.3 要求加好友 / 私聊 / 朋友圈 / 群**各自**配额，并取平台、企业、HR、
# 账号健康几档里**最严**的那个。原来只有一个 `max_sends_per_hour` 总闸管所有写操作，
# 于是「今天一条消息没发」就能换来「连加 30 个好友」——而加好友恰恰是风控最敏感的动作
# （2026-08-12 自动退登事故）。
#
# 总闸保留：分档通过不代表总量可以放开，两道闸都要过（这就是「取最严」的落地形态）。

#: 写类动作的分档。值要跟 `CommandSpec.action` 与 `OutboundSender` 用的一致。
ACTION_MESSAGE = "message"        # 私聊一个人
ACTION_GROUP = "group_message"    # 发到群里
ACTION_FRIEND = "friend"          # 加好友 / 通过好友申请
ACTION_MOMENTS = "moments"        # 发朋友圈

ACTION_LABELS = {
    ACTION_MESSAGE: "私聊",
    ACTION_GROUP: "群消息",
    ACTION_FRIEND: "加好友",
    ACTION_MOMENTS: "朋友圈",
}


@dataclass(frozen=True)
class RateLimit:
    """一档配额：``window_seconds`` 内最多 ``max_count`` 次。``max_count <= 0`` = 禁止这类动作。"""

    max_count: int
    window_seconds: float

    def since(self, now: datetime) -> datetime:
        return now - timedelta(seconds=self.window_seconds)


HOUR = 3600.0
DAY = 86400.0

#: 出厂默认。都比「人手动能做到的量」低一档——宁可挡住也不要把号跑没了。
DEFAULT_ACTION_LIMITS: dict[str, RateLimit] = {
    ACTION_MESSAGE: RateLimit(20, HOUR),
    ACTION_GROUP: RateLimit(6, HOUR),
    # 加好友是最招风控的一类：一小时 5 个已经不算保守。
    ACTION_FRIEND: RateLimit(5, HOUR),
    # 朋友圈按天算——一小时发 3 条和一天发 3 条完全是两回事。
    ACTION_MOMENTS: RateLimit(3, DAY),
}


def action_limits_from(settings) -> dict[str, RateLimit]:
    """把 ``GatewaySettings`` 里的分档配置翻成配额表。

    收成一个函数是因为守门有两个构造点（被动回复的 ``OutboundSender`` 和主动指令的
    ``CommandExecutor``）——两边各抄一份映射，迟早会有一边漏改，而漏改的方向恰恰是「放宽」。
    """
    return {
        ACTION_MESSAGE: RateLimit(settings.max_messages_per_hour, HOUR),
        ACTION_GROUP: RateLimit(settings.max_group_messages_per_hour, HOUR),
        ACTION_FRIEND: RateLimit(settings.max_friend_actions_per_hour, HOUR),
        ACTION_MOMENTS: RateLimit(settings.max_moments_per_day, DAY),
    }


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
        action_limits: dict[str, RateLimit] | None = None,
    ) -> None:
        self._store = store
        self._max_per_hour = max_sends_per_hour
        self._active_hours = active_hours
        self._action_limits = dict(DEFAULT_ACTION_LIMITS)
        if action_limits:
            self._action_limits.update(action_limits)
        parse_active_hours(active_hours)  # 提前校验，别等到要发消息才炸

    def check(
        self,
        account_id: str,
        peer_id: str,
        *,
        now: datetime,
        action: str = ACTION_MESSAGE,
    ) -> GuardDecision:
        """``action`` 决定走哪一档配额（072）。总闸与分档**两道都要过**。"""
        if self._store.is_blacklisted(account_id, peer_id):
            return GuardDecision.deny("该联系人已在黑名单")
        if not within_active_hours(now, self._active_hours):
            return GuardDecision.deny(f"不在允许发送的时间窗 {self._active_hours}")

        # 分档：加好友额度用完了不该因为「今天没怎么发消息」就放行。
        limit = self._action_limits.get(action)
        if limit is not None:
            label = ACTION_LABELS.get(action, action)
            if limit.max_count <= 0:
                return GuardDecision.deny(f"本机没有开放「{label}」这类动作")
            used = self._store.sends_since(account_id, limit.since(now), action=action)
            if used >= limit.max_count:
                span = "24 小时" if limit.window_seconds >= DAY else "一小时"
                return GuardDecision.deny(
                    f"「{label}」在过去{span}内已做 {used} 次，达到上限 {limit.max_count}"
                )

        # 总闸：分档各自没超，加起来也不能没边。
        sent = self._store.sends_in_last_hour(account_id, now)
        if sent >= self._max_per_hour:
            return GuardDecision.deny(f"该账号一小时内已发送 {sent} 条，达到上限")
        return GuardDecision.allow()
