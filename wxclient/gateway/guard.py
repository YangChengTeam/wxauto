"""确定性守门——**不依赖模型自觉**的那一层。

三件事：拒绝识别、频次上限、工作时间窗。全是纯规则，模型判断只作补充。

为什么拒绝识别要放在网关侧而不是只交给 AI 端（``CLAUDE.md`` §5.2）：
漏判一次拒绝的代价是继续骚扰一个明确说过「别联系我」的人。这个代价不能押在
一次模型调用上——网关先用确定性词表拦一道，AI 端的语义判断作为第二道。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from .pacing import (
    MIN_FRIEND_INTERVAL_SECONDS,
    MIN_READ_INTERVAL_SECONDS,
)

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
    except Exception as exc:
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
ACTION_MOMENTS_LIKE = "moments_like"  # 给朋友圈点赞
ACTION_MOMENTS_COMMENT = "moments_comment"  # 给朋友圈评论

ACTION_LABELS = {
    ACTION_MESSAGE: "私聊",
    ACTION_GROUP: "群消息",
    ACTION_FRIEND: "加好友",
    ACTION_MOMENTS: "朋友圈",
    ACTION_MOMENTS_LIKE: "朋友圈点赞",
    ACTION_MOMENTS_COMMENT: "朋友圈评论",
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
    # 点赞同样按天：一天 20 个赞是真人刷朋友圈的量（用户 2026-09-07 定的数）。
    # 一条指令最多点 20 个，所以这档实际上就是「一天一趟」的形状。
    ACTION_MOMENTS_LIKE: RateLimit(20, DAY),
    # 评论比点赞重一个量级：一段公开文字挂在别人动态下面，所有共同好友都看得见。
    # 默认一天 5 条，可在配置中心改（用户 2026-09-07：「做成可配置」）。
    ACTION_MOMENTS_COMMENT: RateLimit(5, DAY),
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
        ACTION_MOMENTS_LIKE: RateLimit(settings.max_moment_likes_per_day, DAY),
        ACTION_MOMENTS_COMMENT: RateLimit(settings.max_moment_comments_per_day, DAY),
    }


#: 读类动作的最小间隔（秒）。读不打扰任何人，但一样是「机器在动微信」——
#: 半夜每秒翻一次联系人，风控看的就是这个。与 `pacing.py` 的轮询下限同源同值（直接引用那两个常量，不再各抄一份）：
#: 那边管的是常驻循环，这里管的是 AI 端随时下发的读指令，绕开哪一边红线都不成立。
READ_ACTION = "read"                  # 读消息 / 翻聊天记录 / 查联系人 / 看朋友圈
READ_FRIENDS_ACTION = "read_friends"  # 查新好友申请（加好友相关，节拍必须慢得多）

READ_INTERVALS: dict[str, float] = {
    READ_ACTION: MIN_READ_INTERVAL_SECONDS,
    READ_FRIENDS_ACTION: MIN_FRIEND_INTERVAL_SECONDS,
}

FRIEND_WRITE_ACTION = "friend_write"  # 主动加好友（搜陌生号，风控最敏感的那一下）
MOMENTS_LIKE_WRITE_ACTION = "moments_like_write"  # 开朋友圈刷一趟点赞
MOMENTS_COMMENT_WRITE_ACTION = "moments_comment_write"  # 开朋友圈评一趟

#: 写类动作也要过最小间隔的，登记在这儿：**指令名** → （节拍账, 最小间隔秒, 人话名字）。
#:
#: ★ 按**指令名**登记，不是按 `action`：`accept_new_friends` 和 `add_friend` 共用
#:   `ACTION_FRIEND` 那本**配额**，但风险形状完全不同——通过别人发来的申请不用搜号，
#:   给它套 300 秒的闸只会让攒着的申请通不完。这道闸只管「主动去搜一个陌生号」。
#:
#: ★ 自己一本账，**不与 `READ_FRIENDS_ACTION` 合账**。合过一次，是错的：复查循环
#:   （`friendcheck.py`）只要还有人待复查就每 300 秒摸一次那本账，合账等于 AI 端下发的
#:   加好友**永远**排在它后面被拒——而被拒的指令是直接丢掉、不排队的。
#:   两条搜索确实不该挨着做，但让路的必须是**能重来的那一方**：复查是死循环，这轮
#:   不查下轮再查；加好友只有这一次机会。所以是复查躲加好友（见 `friendcheck.py`），
#:   不是加好友等复查。
#:
#: 间隔秒数**不写死在这张表里**：默认 `MIN_FRIEND_INTERVAL_SECONDS`，实际取
#: `OutboundGuard(write_intervals=...)`，由 `GatewaySettings.friend_interval_seconds` 灌进来。
#:
#: 配额（5 次/小时）**保留**：那是总量，这是节奏，两道各管一头。
#: 原来只有配额，于是 5 次全挤进一分钟也照样放行——真机上就挤成了 81 秒两次。
WRITE_PACING: dict[str, tuple[str, str]] = {
    "add_friend": (FRIEND_WRITE_ACTION, "加好友"),
    # 点赞一趟就是「打开朋友圈刷一遍」。配额管一天点几个，这本账管两趟之间隔多久——
    # 连着两趟开关朋友圈窗口，中间隔几秒，不是人会做的事。
    "like_moments": (MOMENTS_LIKE_WRITE_ACTION, "朋友圈点赞"),
    "comment_moments": (MOMENTS_COMMENT_WRITE_ACTION, "朋友圈评论"),
}

#: 两趟朋友圈点赞之间的默认最小间隔（秒）。
MIN_MOMENTS_LIKE_INTERVAL_SECONDS = 600.0
#: 两趟朋友圈评论之间的默认最小间隔（秒）。
MIN_MOMENTS_COMMENT_INTERVAL_SECONDS = 600.0

#: 各本写侧节拍账的默认间隔（秒）。可被 `OutboundGuard(write_intervals=...)` 覆盖。
WRITE_INTERVALS: dict[str, float] = {
    FRIEND_WRITE_ACTION: MIN_FRIEND_INTERVAL_SECONDS,
    MOMENTS_LIKE_WRITE_ACTION: MIN_MOMENTS_LIKE_INTERVAL_SECONDS,
    MOMENTS_COMMENT_WRITE_ACTION: MIN_MOMENTS_COMMENT_INTERVAL_SECONDS,
}


def write_intervals_from(settings) -> dict[str, float]:
    """把 ``GatewaySettings`` 里的节拍配置翻成间隔表（对应 `action_limits_from`）。"""
    return {
        FRIEND_WRITE_ACTION: float(settings.friend_interval_seconds),
        MOMENTS_LIKE_WRITE_ACTION: float(settings.moment_like_interval_seconds),
        MOMENTS_COMMENT_WRITE_ACTION: float(settings.moment_comment_interval_seconds),
    }


@dataclass
class Reservation:
    """一次**已经占住的**额度。做完按实际用量结算，没用上的还回去。

    为什么要有它：原来是「先 check 再动微信 再 record_send」，两次数之间隔着一整个
    微信动作。并发下发时一批指令全读到同一个旧计数后集体放行（实测并发 20 条加好友
    done=20/rejected=0，串行才是 5/15）；一条 `accept_new_friends` 通过 5 个人也只记
    1 次，加好友红线实际被放宽 5 倍。占住再做，这两种放宽同时消失。
    """

    granted: int = 0
    reason: str = ""
    temporary: bool = False
    """这次拒绝会不会自己过去。时间窗、配额用完 = 会（等一等就行）；黑名单、本机没开放
    这类动作 = 不会。上层据此决定**这批消息是留着重投还是就此销账**——留错了会在额度
    恢复的那一刻把攒了一天的话一次性喷出去，正是风控要看的东西（审计 P0-4）。"""
    _store: object | None = None
    _rowids: list[int] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.granted > 0

    def settle(self, actually_did: int) -> None:
        """按真实用量结算：预留 5 实际通过 2 → 还回 3。"""
        keep = max(min(int(actually_did), self.granted), 0)
        surplus, self._rowids = self._rowids[keep:], self._rowids[:keep]
        self.granted = keep
        if surplus and self._store is not None:
            self._store.release_sends(surplus)

    def release(self) -> None:
        """一件都没做成（动作抛错 / 被上层拒了）——全额还回去。"""
        self.settle(0)


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
        write_intervals: dict[str, float] | None = None,
    ) -> None:
        self._store = store
        self._max_per_hour = max_sends_per_hour
        self._active_hours = active_hours
        self._action_limits = dict(DEFAULT_ACTION_LIMITS)
        if action_limits:
            self._action_limits.update(action_limits)
        # 写侧节拍（`pace_write`）。没给就用默认，别让「没配」等于「没有闸」。
        self._write_intervals = dict(WRITE_INTERVALS)
        if write_intervals:
            self._write_intervals.update(write_intervals)
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
        held = self._takeover_denial(account_id, peer_id)
        if held is not None:
            return GuardDecision.deny(held)
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

    def reserve(
        self,
        account_id: str,
        peer_id: str,
        *,
        now: datetime,
        action: str = ACTION_MESSAGE,
        count: int = 1,
        clamp: bool = False,
    ) -> Reservation:
        """**占住 `count` 次额度再返回**。占不到 → `allowed=False` + 理由。

        `clamp=True`：额度不够就减量放行（`accept_new_friends` 的「最多 N 个」）。
        默认全有或全无——一段回复拆成三条，发出两条比一条不发更糟。

        黑名单与时间窗是纯判断、不涉及计数，先在事务外判掉，理由也更具体。
        """
        # ★★ 091 候选人级接管闸，排在**所有**判断之前。人在自己跟这个候选人时，
        #   一条都不许自动发给他。
        #   `temporary=True` 是这一档的关键：接管两小时**不等于**丢这个人——
        #   上层据此把这批消息 defer 掉重投，而不是销账（[[settle-only-when-said]]）。
        held = self._takeover_denial(account_id, peer_id)
        if held is not None:
            return Reservation(reason=held, temporary=True)
        if self._store.is_blacklisted(account_id, peer_id):
            return Reservation(reason="该联系人已在黑名单")
        if not within_active_hours(now, self._active_hours):
            return Reservation(reason=f"不在允许发送的时间窗 {self._active_hours}", temporary=True)

        limit = self._action_limits.get(action)
        label = ACTION_LABELS.get(action, action)
        if limit is not None and limit.max_count <= 0:
            return Reservation(reason=f"本机没有开放「{label}」这类动作")

        checks: list[tuple[str, str | None, datetime, int]] = []
        if limit is not None:
            span = "24 小时" if limit.window_seconds >= DAY else "一小时"
            checks.append((f"「{label}」在过去{span}内", action, limit.since(now), limit.max_count))
        checks.append(("该账号一小时内", None, now - timedelta(hours=1), self._max_per_hour))

        rowids, granted, binding = self._store.reserve_sends(
            account_id, peer_id, now, action, count, checks, clamp=clamp,
        )
        if granted <= 0:
            where, used, cap = binding or (f"「{label}」", 0, 0)
            return Reservation(reason=f"{where}已做 {used} 次，达到上限 {cap}", temporary=True)
        return Reservation(granted=granted, _store=self._store, _rowids=rowids)

    def pace_write(self, account_id: str, op: str, *, now: datetime) -> GuardDecision:
        """写类指令的**最小间隔**闸。放行的同时把这一次记下（原子）。

        只有登记在 `WRITE_PACING` 里的指令才受管；别的原样放行——
        发消息有它自己的节奏（对方在等回话），不该被这条 300 秒的闸卡住。

        ★ 和 `reserve` 的先后：**先占额度、再过这道闸**。反过来的话，一次被节拍挡下的
          尝试会白占一次配额，而那一趟微信一下都没动。挡下时调用方负责 `release()`。

        ★ 间隔是**一个固定值**，不随失败翻倍。曾经有过一档连续失败退避
          （300→600→1200→2400），2026-08-28 按用户要求删了：等太久。想更慢就把
          `friend_interval_seconds` 调大——一个看得见、改得动的数，好过一个自己会长的数。
        """
        entry = WRITE_PACING.get(op)
        if entry is None:
            return GuardDecision.allow()
        bucket, label = entry
        floor = self._write_intervals.get(bucket, MIN_FRIEND_INTERVAL_SECONDS)
        if floor <= 0:
            return GuardDecision.allow()
        wait = self._store.try_touch_read(account_id, bucket, now, floor)
        if wait <= 0:
            return GuardDecision.allow()
        return GuardDecision.deny(
            f"「{label}」要隔 {int(floor)} 秒才做一次，还要等 {wait:g} 秒"
        )

    def _takeover_denial(self, account_id: str, peer_id: str) -> str | None:
        """**这个候选人**正被人工接管就给一句拒绝理由，否则 None（091 / docs/19 v2 §1）。

        ★ **一个人一把闸**：这里判的是 `peer_id`（会话名）那一行，不是整台机器。
          分身级那把总闸已随 089 拆除——「招聘老师坐下来自己聊」要停哪些人，
          由他自己在名单上逐个按，不再有一个按钮停掉全部。

        ★ **这里只挂写侧**（``check`` / ``reserve``），``allow_read`` 原样不动——
          但**读也已经停了**，停在更上游：`wechat_io.QueueMessageSource._drop_handed_off`
          直接把接管中的会话从这一轮名单里摘掉（用户裁决 2026-08-28，改了原来的口径）。
          原口径是「接管期间读消息必须继续，否则后端那条『未回复跟进』取消不掉」——
          那条代价现在是**明知故犯**地接受了，理由是招聘老师自己在聊的时候，
          助手不该再去开他那个聊天窗口。这道写侧的闸留着：它是第二条路，
          名单摘漏了（陌生人临时准入、名字对不上）也照样发不出去。

        ★ 读不到（老库没这张表、库锁住了）一律当**没接管**。这条闸 fail 的方向要想清楚：
          读失败就当接管，等于任何一次库抖动都让这个人停摆，而且界面上那个按钮也救不回来
          （它写的是同一张表）。真正的兜底是招聘老师自己按的那一下——显式、看得见。
        """
        try:
            row = self._store.candidate_mode(account_id, peer_id)
        except Exception:  # noqa: BLE001 —— 读不到当没接管，理由见 docstring
            return None
        if not row.taken_over:
            return None
        return f"该候选人已由人工接管{f'：{row.reason}' if row.reason else ''}"

    def allow_read(self, account_id: str, action: str, *, now: datetime) -> GuardDecision:
        """读类动作的节拍闸：够慢才放行，放行的同时把这一次记下（原子）。

        读不受配额与黑名单约束（它不打扰任何人），但**受时间窗和最小间隔约束**——
        原来读类只要理由里没有「时间窗」就一律放行，于是串行 50 条 `check_new_friends`
        全部通过，≥300s 查好友那条红线在指令路径上等于不存在。
        """
        if not within_active_hours(now, self._active_hours):
            return GuardDecision.deny(f"不在允许动作的时间窗 {self._active_hours}")
        floor = READ_INTERVALS.get(action)
        if floor is None:
            return GuardDecision.allow()
        wait = self._store.try_touch_read(account_id, action, now, floor)
        if wait > 0:
            return GuardDecision.deny(f"这类查询要隔 {int(floor)} 秒才做一次，还要等 {wait:g} 秒")
        return GuardDecision.allow()
