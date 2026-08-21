"""新好友申请：一条**独立节拍**的循环。

为什么不跟读消息共用一个间隔（用户 2026-08-13 要求）：这两件事在风控眼里的分量完全不同。
读聊天记录是「看」，加好友是「攀关系」——后者是被盯得最紧的一类动作，节拍必须慢一个量级
（默认 15 分钟 vs 读消息 1 分钟），下限也更严（见 ``pacing.MIN_FRIEND_INTERVAL_SECONDS``）。

⚠️ **只看不通过**。这里跑的是 ``check_new_friends(verify=False)``：把待处理的申请数报出来，
不自动点「通过」。自动通过是写动作，既要 ``CLAUDE.md`` §5.1 的三重确认，也该由 AI 端判断
「这个人是不是我们要加的候选人」——两样都还没接上之前，批量自动通过恰恰是最容易招风控的做法。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

import structlog

from wxclient.queue import TaskType

from .guard import within_active_hours
from .pacing import jittered

_log = structlog.get_logger(__name__)


class FriendWatcher:
    """按自己的间隔查一次新好友申请。``interval_seconds <= 0`` = 完全不查。"""

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        runtime,  # WeChatRuntime，或任何提供 submit(TaskType, payload) 的对象
        interval_seconds: float,
        active_hours: str = "",
        allow_real_actions: bool = False,
        confirmation: str = "",
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self._runtime = runtime
        self._interval = float(interval_seconds)
        self._active_hours = active_hours
        self._allow_real = allow_real_actions
        self._confirmation = confirmation
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng
        self._pending: dict[str, int] = {}
        self._stopped = asyncio.Event()

    def _within_active_hours(self) -> bool:
        """非工作时段一次也不查——半夜自动查好友申请是最典型的「机器在跑」。"""
        if not self._active_hours:
            return True
        return within_active_hours(self._clock(), self._active_hours)

    @property
    def enabled(self) -> bool:
        return self._interval > 0 and bool(self._accounts)

    async def tick(self) -> int:
        """查一趟，返回所有账号加起来的待处理申请数。"""
        if not self.enabled or not self._within_active_hours():
            return 0

        total = 0
        for account_id in self._accounts:
            try:
                # dry_run 必须显式给：底座默认 True，dry-run 下 `check_new_friends`
                # 返回的是空壳（`wechat_api/uia.py`），查了等于没查。真动微信仍需
                # `--allow-real-actions` + 确认短语（`CLAUDE.md` §5.1）。
                result = await self._runtime.submit(
                    TaskType.CHECK_NEW_FRIENDS,
                    {"limit": 8, "dry_run": not self._allow_real},
                    confirmation=self._confirmation if self._allow_real else "",
                )
            except Exception as exc:  # noqa: BLE001 —— 查失败不该拖垮循环
                _log.warning("查新好友申请失败", account=account_id, error=str(exc))
                continue

            items = ((result or {}).get("result") or {}).get("items") or []
            count = len(items)
            total += count
            # 只在数目变化时说话：每 15 分钟固定刷一条「0 个」是纯噪音。
            if count != self._pending.get(account_id):
                self._pending[account_id] = count
                if count:
                    _log.info("有新好友申请待处理（不会自动通过）", account=account_id, count=count)
        return total

    async def run(self) -> None:
        if not self.enabled:
            _log.info("未开启新好友申请检查")
            return
        self._stopped.clear()
        while not self._stopped.is_set():
            # 先睡后查：启动那一刻已经有读消息在动微信了，别两件事挤在一起。
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=jittered(self._interval, rng=self._rng)
                )
            except TimeoutError:
                pass
            if self._stopped.is_set():
                break
            await self.tick()

    def stop(self) -> None:
        self._stopped.set()
