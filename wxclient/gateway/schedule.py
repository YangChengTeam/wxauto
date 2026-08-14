"""定时指令：到点才执行的那一类（077，066 task 4）。

``schedule_at`` 早就在契约里透传到网关了，但一直没人落表执行——于是需求文档 §6.19 的
定时 / 周期 / 条件触发**全都不可用**：面试前提醒、未回复跟进、内容日历都无从谈起，
只有「来了新消息就回」这一种触发方式能跑。

设计上刻意做成**独立的一条循环**，跟入站轮询、保活各走各的节拍（``runner.run`` 里并列起）：
串成一个循环就等于让最慢的那件事决定所有事的频率。

到点之后**仍旧走 ``CommandExecutor``**，不另开一套执行路径——幂等（同一 command_id 只做
一次）和守门（频次配额 / 工作时间窗 / 黑名单）就都白捡了。

后续要加周期（每周一次）和条件触发（对方 N 天没回），在这一层加就行：
表里多一列重复规则、执行完重新排下一次，执行侧一行都不用动。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import structlog

_log = structlog.get_logger(__name__)

#: 到点检查的间隔。定时任务的精度要求是「分钟级」——面试提醒早一分钟晚一分钟都没关系，
#: 而查一次库几乎不要钱，所以这里不跟风控节拍挂钩（它压根不动微信，只是翻自己的表）。
CHECK_INTERVAL_SECONDS = 30.0

#: 迟到多久就不做了。定时提醒的价值全在时点上——「面试前一小时提醒」迟到六小时送达，
#: 比不送更让人困惑（面试都结束了）。机器关了一夜再开机，昨天的排期不该突然全放出来。
MAX_LATE_HOURS = 6


def parse_schedule_at(raw: object) -> datetime | None:
    """解析 ``schedule_at``（ISO-8601，带时区）。解析不了返回 ``None``。

    不带时区的按**本机时间**理解：用户说「明天上午九点」，指的显然是他那台电脑上的九点。
    当成 UTC 的话在东八区会差八小时——正是 `guard.within_active_hours` 踩过的那个坑。
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


class ScheduleLoop:
    """定时指令的常驻循环。

    时间与「现在能不能动微信」都从外面注入，所以整层在 Linux 上可单步测试——
    ``tick()`` 是可以直接调的一趟，测试永远不用真的等到某个时刻。
    """

    def __init__(
        self,
        *,
        accounts: list[str],
        store,                                   # GatewayStore（鸭子类型，避免循环导入）
        run_command: Callable[[str, dict], Awaitable[None]],
        is_active: Callable[[datetime], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        interval_seconds: float = CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._accounts = list(accounts)
        self._store = store
        self._run_command = run_command
        self._is_active = is_active or (lambda _now: True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._interval = interval_seconds
        self._stopped = asyncio.Event()

    async def tick(self) -> int:
        """跑一趟：捞到点的、执行、销账。返回本趟放出去几条。

        ★ 工作时间窗外**一条都不放**：定时任务同样是微信动作，半夜三点准时发一条
        「提醒您明天面试」正是风控要找的那种账号（2026-08-12 自动退登事故）。
        没到时间窗就在表里等着，第二天进窗口第一趟放出去。
        """
        now = self._clock()
        if not self._is_active(now):
            return 0

        fired = 0
        cutoff = now - timedelta(hours=MAX_LATE_HOURS)
        for account_id in self._accounts:
            for row in self._store.due_scheduled_commands(account_id, now):
                self._store.drop_scheduled_command(row["command_id"])
                if row["due_at"] < cutoff.isoformat():
                    # 迟太久就别做了——但必须说出来，静悄悄吞掉一条排期跟丢消息一样坏。
                    _log.warning(
                        "定时指令迟到太久，已放弃",
                        account=account_id, 原定=row["due_at"],
                        op=(row["payload"] or {}).get("op"),
                    )
                    continue
                _log.info(
                    "定时指令到点，开始执行",
                    account=account_id, 原定=row["due_at"],
                    op=(row["payload"] or {}).get("op"),
                )
                await self._run_command(account_id, row["payload"])
                fired += 1
        return fired

    async def run(self) -> None:
        self._stopped.clear()
        while not self._stopped.is_set():
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 —— 一趟出错不该把整条循环带停
                _log.warning("定时指令巡检出错", error=str(exc))
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stopped.set()
