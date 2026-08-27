"""监听名单里还不是好友的人，自动补加好友（092）。

**补的是哪个洞**：业务后端已经有一整套「先加好友再触达」，但它只在**计划触达到点**时才判。
一个人在监听名单里却从没进过计划、或者好友被删了、或者加了一直没通过——没人发现。
只有客户端的轮询能真看到微信里的实情，现场表现就是「名单加了他，可他一直不回」。

**本模块只做一件事：判「这个人到底是不是好友」，然后走既有那条加好友的路。**
不新造管子——执行器、频控闸、上报全是现成的（用户 2026-08-27 裁决）。

★★ 最容易做错的两条，都写在下面的判定链里：

① **「读不到」≠「不是好友」。** `wechat_io` 那条分支任何读失败都会走：名字差一个字、
   微信卡了、窗口打不开。拿它直接去加好友就是误加。所以这里要再确定性地判一次
   （`search_contacts`），而且**搜索本身失败不算「没这个人」**。

② **去重必须持久、且不能被同步名单覆盖。** 既有的 `note_friend_request` 写的是
   `gateway_contacts.status`，而那张表同步名单会覆盖（服务端才是权威）——拿它当去重键，
   同步一次就会把同一个人再加一遍。加好友重复执行就是风控事故。
   所以另起一张只由本机写的 `gateway_auto_friend`。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog

from wxclient.queue import TaskType

from .pacing import MIN_FRIEND_INTERVAL_SECONDS

_log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutoFriendWatcher:
    """连续读不到的那个会话，判一判是不是压根还不是好友；是就补加一次。"""

    def __init__(
        self,
        *,
        store,
        runtime,          # 提供 submit(TaskType, payload)
        commands,         # CommandExecutor：加好友走它，闸和记账全在里面
        on_added=None,    # async (command, result) -> None：加完照旧上报业务端
        clock=None,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._commands = commands
        #: 加完之后照旧上报业务端。runner 拼好之后再装（它才有 `_report_contact`）。
        self.on_added = on_added
        self._clock = clock or _utcnow
        self._enabled = enabled

    async def on_unreachable(self, account_id: str, session_name: str, reason: str = "") -> None:
        """`QueueMessageSource` 连续几轮读不到这个会话时调进来。

        判定链**任何一条不满足就什么都不做**——不动微信、不记账、不报。
        宁可漏一次补加（人还在名单里，下一轮还会再来），也不能误加一次。
        """
        if not self._enabled:
            return
        session_name = (session_name or "").strip()
        if not session_name:
            return

        # ① 本机查得到这个会话名对应的号吗。没有就动不了——`add_friend` 的 `number` 必填，
        #    而陌生人、还没归位的会话本来就没有号。这不是错误，是「这条路走不通」。
        number = self._store.contact_number_for(account_id, session_name)
        if not number:
            _log.info("读不到但本机没有他的号，补加不了", 会话=session_name)
            return

        # ② 这个号本机自作主张加过吗。加过就永不再自动加（见 `auto_friend_tried`）。
        tried_at = self._store.auto_friend_tried(account_id, number)
        if tried_at:
            return

        # ③ 节拍：两次自动加好友之间至少 MIN_FRIEND_INTERVAL_SECONDS。
        #    这是**除了** guard 的 5 次/小时之外又一道——加好友是风控最敏感的动作，
        #    名单一次刷进来十几个读不到的，不能连着十几个申请。
        if not self._pacing_ok(account_id):
            _log.info("自动补加好友节拍没到，这轮先不加", 会话=session_name)
            return

        # ④ ★ 确定性判「确实不在通讯录」。搜不到 ≠ 搜失败：
        #    `search_contacts` 搜不到人时正常返回 `exact_count == 0`；
        #    只有搜索框都找不到之类才会失败。**失败就当没判过**，下轮再说。
        verdict = await self._not_a_contact(session_name)
        if verdict is not True:
            if verdict is None:
                _log.info("没能确认是不是好友，这轮不动手", 会话=session_name)
            return

        # ⑤ ★ **先记账再动手。** 反过来的话，动手成功而记账失败（磁盘满、进程被杀）
        #    下一轮就会再加一次。宁可漏一次，不可重一次。
        now = self._clock()
        if not self._store.note_auto_friend(account_id, number, session_name, now.isoformat()):
            return

        await self._add_friend(account_id, number, session_name, reason)

    # ────────────────────────────── 内部 ──────────────────────────────

    def _pacing_ok(self, account_id: str) -> bool:
        last = self._store.last_auto_friend_at(account_id)
        if not last:
            return True
        try:
            at = datetime.fromisoformat(last)
        except ValueError:
            return True
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return self._clock() - at >= timedelta(seconds=MIN_FRIEND_INTERVAL_SECONDS)

    async def _not_a_contact(self, session_name: str) -> bool | None:
        """在微信通讯录里搜这个名字。

        `True` = 确实搜不到（可以补加）· `False` = 搜到了（他是好友，读不到是别的原因）
        · `None` = **没判出来**（搜索本身失败），什么都别做。
        """
        try:
            found = await self._runtime.submit(
                TaskType.SEARCH_CONTACTS, {"query": session_name, "limit": 5},
            )
        except Exception as exc:  # noqa: BLE001 —— 搜失败不能变成「没这个人」
            _log.warning("查通讯录失败，不当作「不是好友」", 会话=session_name, 原因=str(exc)[:120])
            return None
        if str((found or {}).get("status") or "") not in {"", "succeeded"}:
            return None
        result = (found or {}).get("result") or {}
        if "exact_count" not in result and "match_count" not in result:
            # 形状不对：宁可不判，也别拿一个读不出来的结构当「没这个人」。
            return None
        exact = int(result.get("exact_count") or 0)
        matches = int(result.get("match_count") or 0)
        return exact == 0 and matches == 0

    async def _add_friend(self, account_id: str, number: str, session_name: str, reason: str) -> None:
        """走**既有的**加好友执行器。能力闸、频控闸、时间窗、幂等全在它里面。

        ★ `greetings` 传空 —— 用户 2026-08-27 裁决「不填，用微信默认」。
          红线照旧：内部 `reason`（「名单里读不到」这种话）**绝不**兜底进验证语，
          那就是「内部指令变成一条候选人真能读到的微信」那个事故形状。这里连传都不传。
        """
        command = {
            "command_id": f"autofriend:{uuid.uuid4()}",
            "op": "add_friend",
            "args": {"number": number, "greetings": "", "remark": session_name},
        }
        _log.info("名单里这个人还不是好友，补发一条好友申请",
                  会话=session_name, 起因=reason[:80] or None)
        result = await self._commands.execute(command)
        if result.status not in {"done", "succeeded"}:
            _log.warning("自动补加好友没成", 会话=session_name, 状态=result.status,
                         原因=(result.reason or "")[:120])
        if self.on_added is not None:
            await self.on_added(command, result)


__all__ = ["AutoFriendWatcher"]
