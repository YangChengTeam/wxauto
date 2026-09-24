"""新好友申请：一条**独立节拍**的循环。

为什么不跟读消息共用一个间隔（用户 2026-08-13 要求）：这两件事在风控眼里的分量完全不同。
读聊天记录是「看」，加好友是「攀关系」——后者是被盯得最紧的一类动作，节拍必须慢一个量级
（默认 15 分钟 vs 读消息 1 分钟），下限也更严（见 ``pacing.MIN_FRIEND_INTERVAL_SECONDS``）。

**通不通过由 ``auto_accept`` 定**（``GatewaySettings.auto_accept_friend``，默认开——用户 2026-09-12 裁决
「要改成自动通过」）：

- 关着：跑 ``check_new_friends(verify=False)``，只把待处理的申请数报出来，不点「通过」（2026-09-12 之前的行为）。
- 开着：**走指令那条路**——``CommandExecutor.execute({op: accept_new_friends})``，和 AI 端下发的同一条指令
  完全一样：能力闸、加好友配额（每小时 5 次）、幂等一道不绕，一趟最多通过 5 个。这一趟真点了「通过」的名字
  （驱动层 ``accepted``，不是「新的朋友」整个列表）交给 ``on_accepted``，由 runner 把认得出号的
  报给业务端（不自己加进监听，监听只认同步名单）。通过是写动作，仍要 ``--allow-real-actions``——dry-run 机器上只数不点。

为什么以前不通过：底座那一下是「把待处理的全部通过」，挑不了人。现在的判断是：扫名片加进来的人本来
就没有任何名单可对，能判的只有「他找上门了」；卡着不通过的结果是候选人加了好友一直没人理（2026-09-11 现场）。
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone

import structlog

from wxclient.queue import TaskType

from .guard import within_active_hours
from .pacing import jittered

_log = structlog.get_logger(__name__)

#: 自动通过一趟最多点几个「通过」。与 `commands.py` 里 `accept_new_friends` 的上限同值：
#: 申请攒多了也一趟 5 个，剩下的下一趟（15 分钟后）——人点「通过」也不会一口气点二十个。
AUTO_ACCEPT_LIMIT = 5


class FriendWatcher:
    """按自己的间隔查一次新好友申请。``interval_seconds <= 0`` = 完全不查。

    ``auto_accept=True`` 时通过 ``commands``（``CommandExecutor``）下 ``accept_new_friends``，
    真通过了谁就调 ``on_accepted(command, result, names)``。
    """

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        runtime,  # WeChatRuntime，或任何提供 submit(TaskType, payload) 的对象
        interval_seconds: float,
        active_hours: str = "",
        allow_real_actions: bool = False,
        confirmation: str = "",
        auto_accept: bool = False,
        commands=None,  # CommandExecutor；auto_accept 时必给，否则退回只看不通过
        on_accepted: Callable[..., Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self._runtime = runtime
        self._interval = float(interval_seconds)
        self._active_hours = active_hours
        self._allow_real = allow_real_actions
        self._confirmation = confirmation
        self._auto_accept = bool(auto_accept)
        self._commands = commands
        self.on_accepted = on_accepted
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

    @property
    def auto_accept(self) -> bool:
        """这一台会不会真点「通过」：开关开着 + 有指令执行器 + 允许真动微信。"""
        return self._auto_accept and self._commands is not None and self._allow_real

    async def tick(self) -> int:
        """查一趟，返回所有账号加起来的待处理申请数（自动通过时 = 「新的朋友」列表条数）。"""
        if not self.enabled or not self._within_active_hours():
            return 0

        total = 0
        for account_id in self._accounts:
            if self.auto_accept:
                total += await self._accept_account(account_id)
                continue
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

    async def _accept_account(self, account_id: str) -> int:
        """自动通过：下一条和 AI 端同形的 `accept_new_friends` 指令，真通过了谁就交给 runner。"""
        command = {
            "command_id": f"auto-accept-{uuid.uuid4().hex[:12]}",
            "op": "accept_new_friends",
            "args": {"limit": AUTO_ACCEPT_LIMIT},
        }
        try:
            result = await self._commands.execute(command)
        except Exception as exc:  # noqa: BLE001 —— 通过失败不该拖垮循环
            _log.warning("自动通过好友申请失败", account=account_id, error=str(exc))
            return 0
        if result is None or result.status != "done":
            # 配额满 / 时间窗外 / 能力关着：指令层已经说了原因，这里只记一条，下一趟再来。
            _log.info("这一趟没通过好友申请", account=account_id,
                      status=getattr(result, "status", ""), reason=getattr(result, "reason", ""))
            return 0
        payload = result.result or {}
        items = payload.get("items") or []
        accepted = [str(n or "").strip() for n in (payload.get("accepted") or []) if str(n or "").strip()]
        count = len(items) if isinstance(items, list) else 0
        if not accepted:
            # 查到了条目却一个都没点「通过」：要么全是早就通过的老条目，要么这版微信的「前往验证」按钮
            # 认不出来（vendor 判据跟微信版本走）。原来这一档一个字不记，看日志只见「轮询」不见「为什么没通过」。
            # 只在数目变化时说话：每 15 分钟固定刷一条是纯噪音。
            if count != self._pending.get(account_id):
                self._pending[account_id] = count
                if count:
                    _log.info("「新的朋友」里有条目，但这一趟一个都没点「通过」（都是老条目，或认不出「前往验证」按钮）",
                              account=account_id, count=count)
            return count
        self._pending[account_id] = count
        _log.info("已自动通过好友申请", account=account_id, 通过=len(accepted))
        if self.on_accepted is not None:
            try:
                await self.on_accepted(command, result, accepted)
            except Exception as exc:  # noqa: BLE001 —— 后续处理失败不该拖垮循环
                _log.warning("通过之后的处理失败", account=account_id, error=str(exc))
        return count

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
