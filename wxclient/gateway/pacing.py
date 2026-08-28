"""节拍：多久去动一次微信。

★ 这一层是被真机事故逼出来的（2026-08-12：跑了一天之后微信自己退了登录，判定触发风控）。
UI 自动化被风控盯上的两个最强特征是：

  ① **节拍精确得不像人**——每 5 秒整点动一次，人不会这样；
  ② **半夜也在动**——没人凌晨三点还在一秒不差地翻聊天列表。

所以这里只做两件事：给每个间隔加随机抖动、给间隔设下限。工作时段的限制复用
``guard.within_active_hours``（同一个「工作时间窗」配置，不再另开旋钮）。

**这不是「反检测」**，是把自动化的节奏收敛到正常使用水平——少动、慢动、该睡就睡。
"""

from __future__ import annotations

import random

import structlog

_log = structlog.get_logger(__name__)

#: 读消息的最小间隔。比这更勤没有意义（对方也不会一秒回一条），却成倍放大风控风险。
MIN_READ_INTERVAL_SECONDS = 30.0

#: 查新好友申请的最小间隔。加好友相关的动作是风控最敏感的一类，节拍必须比读消息慢得多。
#:
#: ★ 这个下限**同时管住「加好友」**（2026-08-27 真机弹出安全验证）：原来它只挂在读侧
#: （`guard.allow_read`），而真正去搜陌生号、发申请的 `add_friend` 走的是写侧，写侧只有
#: 「5 次/小时」的配额、**没有最小间隔**——5 次全挤进一分钟也照样放行。那天就是 15:22:36
#: 和 15:23:57 两次，隔 81 秒，而且两次都是「没搜到这个号」。**连续搜不到的陌生手机号**
#: 正是枚举的特征，比成功加一个好友敏感得多。加和查现在共用同一本账（`READ_FRIENDS_ACTION`）：
#: 谁先动了微信，另一边就得等——这才是「整台机器每 300 秒才碰一次好友」。
MIN_FRIEND_INTERVAL_SECONDS = 300.0

#: 加好友**连续失败**后的退避倍数上限。搜不到 → 600s → 1200s → 2400s，成功即归零。
#:
#: 为什么失败要比成功更慢：一次「没搜到这个号」在微信那边和一次正常加好友不是一回事，
#: 它更像在试号。第二次紧接着再来一个搜不到的号，就是最典型的枚举形状——
#: 昨天弹安全验证的正是这个序列。退避只往慢里走，成功一次就恢复正常节拍。
MAX_FRIEND_BACKOFF_STEPS = 3


def backoff_seconds(base: float, streak: int, *, max_steps: int = MAX_FRIEND_BACKOFF_STEPS) -> float:
    """连续失败 ``streak`` 次之后，这一档的间隔该是多少秒。``streak<=0`` → 原样。"""
    steps = max(0, min(int(streak), int(max_steps)))
    return float(base) * (2 ** steps)

#: 抖动幅度：实际间隔在 [1, 1+r] 之间随机取——**只往慢里抖，不往快里抖**。
#: 0.3 已经足够打散「整点」特征。
#:
#: ★ 原来是 [1-r, 1+r] 双向的（2026-08-18 审计实测：读消息 min=21s、52% 的轮次低于 30s；
#: 查好友 min=210s、50% 低于 300s）——那两个数字正是本模块声称「下限把住了」的下限。
#: 下限是防风控红线，抖动不该有权把它抖穿；往慢里抖同样不规律，代价只是平均慢 15%。
JITTER_RATIO = 0.3

#: AI 端下发节拍用的推送事件。业务比网关更清楚「现在该盯紧还是放松」——正等对方回话
#: 就调快，聊完/转人工就放慢。**下限仍由网关这边把住**，下发方只能在允许范围内表达意愿。
PACING_EVENT = "wechat.pacing"


def clamp_interval(seconds: float, minimum: float, *, what: str) -> float:
    """低于下限就抬到下限并留一条警告。

    不直接拒绝启动：配置早于这条下限存在（线上那台填的是 15 秒），为一条节拍把整个网关
    挡在门外太重。但也不能默默照跑——抬上来并说清楚，人看日志就知道自己填的没生效。
    """
    value = float(seconds)
    if value < minimum:
        _log.warning(
            "间隔过密，已抬到下限（防风控）", what=what, 配置=value, 实际=minimum
        )
        return minimum
    return value


def jittered(seconds: float, *, ratio: float = JITTER_RATIO, rng: random.Random | None = None) -> float:
    """给间隔加抖动，返回这一次实际要睡多久。**只会比基数慢，绝不会更快。**

    传进来的基数已经过 ``clamp_interval``（不低于红线），所以单向抖动 = 红线永远成立。
    """
    if seconds <= 0:
        return 0.0
    source = rng or random
    return seconds * (1.0 + source.uniform(0.0, ratio))
