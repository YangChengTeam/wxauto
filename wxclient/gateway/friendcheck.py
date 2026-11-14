"""已申请、还没通过的好友，隔一阵去微信里复查一次（093）。

**补的是哪个洞**：`add_friend` 成功只代表**申请发出去了**，对方哪天点「通过」没人知道
（`wechat_api/uia.py` 自己写着「加好友是异步的」）。而 `requested` 这一档人**不在监听名单里**
（`watched_session_names` 只要 `accepted`，理由是好友没通过时微信里根本没有那个会话窗口，
放进名单只会每轮白开一次、白吃一个读名额），于是他通过之后发来的第一条消息网关也读不到——
`runner._note_friend_accepted`（「收到他的消息 = 他通过了」）等的那个调用永远不会来。

结果就是：**我们主动加的人一旦通过，系统里没有任何一处会发现。**
现场表现是这个人在计划里永远停在「等对方通过」，而对方其实早就通过了。

★ 2026-09-01 用户改口径，本模块跟着变了三处（别照旧注释改回去）：
  · **间隔和期限进配置中心**（`friend_recheck_interval_seconds` /
    `friend_recheck_deadline_hours`），期限从**申请那一刻**算起，
    取代原来按次数放弃的 `MAX_CHECKS = 96`——次数换算不出来是几天，期限看得懂。
  · **搜谁的权威改成本机台账** `gateway_friend_checks`（`next_friend_check`）。
    原来取 `gateway_contacts.status='requested'`，而那张表被同步名单无条件覆盖：
    后端没回 `contact_status` 就写成空串，人从此不在待查集里，一声不吭。
  · **认出通过之后当场报**，不再压半小时（`gateway_pending_accepted` 队列已删）。
还有一条**不在本模块**、但和本模块一正一反：业务端下发的发消息指令发成功了，
也说明他早就通过了（`runner._note_sent_to_pending_friend`），那条不额外动微信。

**本模块只做一件事：判「他通过了没有」，通过了就交给现成的那条上报路**
（`runner._note_friend_accepted` → `POST /client/contacts` → 进监听名单）。
不新造管子：判据、上报、名单派生全是现成的。

判据和 092 是同一条（`search_contacts` 精确命中 = 他已经在通讯录里了），只是反过来问：
092 问「是不是还不是好友」，这里问「是不是已经是好友了」。

★ 三条容易做错的：

① **搜不到 ≠ 搜失败。** `search_contacts` 搜不到人时正常返回 `exact_count == 0`；
   只有搜索框都找不到之类才会抛。**搜失败什么都不算**——既不报通过，也不计进放弃次数。

② **节拍走共享的 `read_friends` 闸**（`GatewayStore.try_touch_read`），和 AI 端下发的
   `check_new_friends` 共用同一档。这是「查好友」这类动作在**整台机器上**的总节拍，
   不是本循环自己的间隔——否则几条循环各自守着自己的间隔，合起来就是每几十秒动一次微信。
   另外本循环还要**给主动加好友让路**：加好友那本账（`friend_write`）只看不占，
   刚加过就跳过这轮。让路的是能重来的一方，被拒的加好友指令是直接丢掉的。
   ★ 两道闸的秒数都是配置中心的「加好友最小间隔」（2026-09-17 用户裁决，原来写死 300）；
   被挡下的那一趟只睡闸还差的秒数，不整睡一个复查周期。

③ **同名会误判。** 搜的是我们自己设的备注名（`张三-仓库工` 这种），微信通讯录里另有一个
   一模一样的名字就会命中。备注名带岗位后缀时概率很低，但这是这条判据的已知边界。
   误判的代价可控：那个人被报成 `accepted` 进了监听名单，每轮读不到，092 随后会确认
   「他确实不在通讯录」并补加一次好友——链路自己能绕回来。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

import structlog

from wxclient.queue import TaskType

from .guard import FRIEND_WRITE_ACTION, READ_FRIENDS_ACTION, within_active_hours
from .pacing import MIN_FRIEND_INTERVAL_SECONDS, jittered
from .sessions import GatewayStore

_log = structlog.get_logger(__name__)

#: 兜底期限（小时）。配置中心没给「查多久」时用它。
#:
#: 取代原来按次数的 MAX_CHECKS（96 次，2026-09-01 用户改口径）：次数是个换算不出来的数——
#: 每轮最多查一个人、还要过 ≥300s 共享闸和工作时间窗，96 次实际横跨好几天，
#: 谁也说不准是几天。期限是**看得懂、改得动**的一个数。
#: 期限本身不是精度问题，是**别让一个永远不会通过的号一直占住每轮那一个名额**——
#: 名额被占住，后面新申请的人就一直排不上。
DEFAULT_DEADLINE_HOURS = 72.0


class PendingFriendWatcher:
    """按自己的间隔复查一个「已申请、还没通过」的人。``interval_seconds <= 0`` = 完全不查。"""

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        runtime,  # WeChatRuntime，或任何提供 submit(TaskType, payload) 的对象
        store: GatewayStore,
        interval_seconds: float,
        deadline_hours: float = DEFAULT_DEADLINE_HOURS,
        friend_interval_seconds: float = MIN_FRIEND_INTERVAL_SECONDS,
        active_hours: str = "",
        on_accepted: Callable[[str, str], object] | None = None,
        db_connected: Callable[[], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self._runtime = runtime
        self._store = store
        self._interval = float(interval_seconds)
        #: 两次「搜人」之间至少隔多久＝配置中心的「加好友最小间隔」（`friend_interval_seconds`）。
        #: ★ 2026-09-17 用户裁决：这个数以前只管「加人→再加人」，复查这边让路和占闸都写死 300，
        #:   界面上填 180 对「加人→查他通过没」一点不生效，秒通过的候选人要等七分钟才收到第一句。
        #:   现在两道闸都读它：让路（刚加过好友）和本循环自己占的搜人闸。
        self._friend_interval = float(friend_interval_seconds)
        #: 上一趟被闸挡下时，闸还差几秒（0 = 没被挡）。`run()` 据此只睡剩余的秒数，
        #: 不再整睡一个复查周期——否则 180 秒周期撞上 300 秒闸，实际每 6 分钟才真搜一次。
        self.pending_wait_seconds: float = 0.0
        #: 申请发出后搜多久就不再搜（配置中心「查多久」）。``<= 0`` = 不设期限，一直搜。
        self._deadline_hours = float(deadline_hours)
        self._active_hours = active_hours
        #: async (account_id, session_name) -> None：确认通过之后怎么落地。
        #: runner 拼好之后再装（上报和名单派生都在它手上）。
        self.on_accepted = on_accepted
        #: () -> bool：本地库此刻连着没有（`DbMessageSource.db_connected`）。连着就整趟不搜——
        #: 「他通过了没有」由本地库每分钟对 contact 表判（`DbMessageSource._check_pending_friends`），
        #: 不动微信。★ 用户 2026-11-13：名单清空了还在一个个搜人，就是这条和本地库那条两头一起跑。
        #: 撞名的人本地库判不出，搜索也一样判不准（见模块注释 ③），所以不留给搜索兜底。
        self._db_connected = db_connected
        self._deferring_to_db = False
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng
        self._stopped = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._interval > 0 and bool(self._accounts)

    def _within_active_hours(self) -> bool:
        """非工作时段一次也不查——半夜自动翻通讯录是最典型的「机器在跑」。"""
        if not self._active_hours:
            return True
        return within_active_hours(self._clock(), self._active_hours)

    async def tick(self) -> list[str]:
        """复查一趟，返回**这一趟确认已经通过**的会话名。"""
        self.pending_wait_seconds = 0.0
        if not self.enabled or not self._within_active_hours():
            return []
        if self._db_connected is not None and self._db_connected():
            if not self._deferring_to_db:
                self._deferring_to_db = True
                _log.info("本地库已连上，好友通过改由本地库判，搜索复查暂停")
            return []
        if self._deferring_to_db:
            self._deferring_to_db = False
            _log.info("本地库断开了，好友通过退回搜索复查")
        accepted: list[str] = []
        waits: list[float] = []
        for account_id in self._accounts:
            name, wait = await self._check_one(account_id)
            if name:
                accepted.append(name)
            if wait > 0:
                waits.append(wait)
        self.pending_wait_seconds = min(waits) if waits else 0.0
        return accepted

    def next_sleep_seconds(self) -> float:
        """下一趟睡多久：正常是带抖动的复查周期；上一趟被闸挡下就只睡闸还差的秒数。

        被挡下时不加抖动——抖动是为了「整点动作」不像机器，而这里睡完醒来还要再过一次闸，
        真正动微信的时刻由闸决定，不由这一觉决定。下限 1 秒，免得 0.1 秒级的余量空转。
        """
        base = jittered(self._interval, rng=self._rng)
        if self.pending_wait_seconds > 0:
            return min(base, max(self.pending_wait_seconds, 1.0))
        return base

    async def _check_one(self, account_id: str) -> tuple[str, float]:
        """这个账号下复查**一个**人。一趟只查一个：查一个人 = 动一次微信搜索。

        返回 ``(确认通过的会话名, 闸还差几秒)``：被闸挡下时前者为空、后者 > 0。
        """
        now = self._clock()
        # 期限从**申请发出那一刻**算起：早于这个时刻的不再查。0 或负数 = 不设期限。
        not_before = (
            (now - timedelta(hours=self._deadline_hours)).isoformat()
            if self._deadline_hours > 0 else ""
        )
        number, session_name = self._store.next_friend_check(account_id, not_before=not_before)
        if not number or not session_name:
            return "", 0.0

        # ★ 先给「主动加好友」让路（2026-08-27 安全验证）。刚搜过一个陌生号，紧接着再搜
        #   一个人，在微信那边就是连着两次搜索——正是弹安全验证的形状。
        #   让路的必须是本循环：复查是死循环，这轮不查下轮再查；AI 端下发的加好友被拒
        #   就直接丢了。所以这里只**看**加好友那本账（`seconds_since_read`，不占住它）。
        #   让路多久＝配置中心的「加好友最小间隔」，和加好友自己守的是同一个数。
        since_add = self._store.seconds_since_read(account_id, FRIEND_WRITE_ACTION, now)
        if since_add is not None and since_add < self._friend_interval:
            remaining = round(self._friend_interval - since_add, 1)
            _log.info(
                "刚加过好友，这轮先不复查", 距上次加好友=round(since_add, 1), 还要等=remaining
            )
            return "", remaining
        # ★ 共享节拍闸，不是本循环自己的间隔。092 的自动补加、AI 端下发的查好友申请
        #   都走这一档，谁先动了微信，后面的就得等。本循环占这本账时按配置的
        #   「加好友最小间隔」算（AI 端下发的 `check_new_friends` 仍按 guard 的下限）。
        wait = self._store.try_touch_read(
            account_id, READ_FRIENDS_ACTION, now, self._friend_interval
        )
        if wait > 0:
            _log.info("查好友那档节拍没到，这轮先不复查", 还要等=wait)
            return "", wait

        verdict = await self._is_contact(session_name)
        # 真去微信里查过了就记时间（判没判出来都记），否则下一轮还挑中同一个人，
        # 后面排队的谁也轮不上。放弃与否现在只看期限，不再计次。
        self._store.note_friend_check(account_id, number, now.isoformat())
        if verdict is not True:
            if verdict is None:
                _log.info("没能确认对方通过了没有，这轮不动结论")
            return "", 0.0

        # 名字不进日志（会话名是候选人的备注名，属于可识别信息）。
        _log.info("复查确认：对方已经通过好友申请", account=account_id)
        if self.on_accepted is not None:
            result = self.on_accepted(account_id, session_name)
            if asyncio.iscoroutine(result):
                await result
        return session_name, 0.0

    async def _is_contact(self, session_name: str) -> bool | None:
        """在微信通讯录里搜这个备注名。

        ``True`` = 搜到了（他通过了）· ``False`` = 确实搜不到（还没通过）
        · ``None`` = **没判出来**（搜索本身失败），什么都别动。
        """
        try:
            found = await self._runtime.submit(
                TaskType.SEARCH_CONTACTS, {"query": session_name, "limit": 5},
            )
        except Exception as exc:  # noqa: BLE001 —— 搜失败不能变成「他通过了」
            _log.warning("查通讯录失败，不当作「已通过」", 原因=str(exc)[:120])
            return None
        if str((found or {}).get("status") or "") not in {"", "succeeded"}:
            return None
        result = (found or {}).get("result") or {}
        if "exact_count" not in result and "match_count" not in result:
            # dry-run 下底座回的是空壳，没有这两个字段。**没判过**，别当成「还没通过」——
            # 当成还没通过就会一路计次，把人计到放弃阈值上，真通过了也再没人去看一眼。
            return None
        exact = int(result.get("exact_count") or 0)
        return exact > 0

    async def run(self) -> None:
        if not self.enabled:
            return
        self._stopped.clear()
        while not self._stopped.is_set():
            # 先睡后查：启动那一刻已经有读消息在动微信了，别两件事挤在一起。
            # 上一趟被闸挡下就只睡闸还差的秒数（`next_sleep_seconds`）。
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.next_sleep_seconds())
            except TimeoutError:
                pass
            if self._stopped.is_set():
                break
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 —— 复查失败不该拖垮循环
                _log.warning("复查好友申请出错，下轮再来", error=str(exc)[:120])

    def stop(self) -> None:
        self._stopped.set()
