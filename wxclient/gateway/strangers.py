"""陌生人主动发起：把「会话列表里出现了、可我们不认识」的会话临时纳入监听。

契约：`docs/招聘分身_客户端接管托管同步接口_v1.md` §9.5（AI 端建档时机），
spec 090（劳小智 AI 服务仓）。

★ **这条路今天是断的。** 入站只读监听名单里的会话（``wechat_io.QueueMessageSource``），
  而名单从 `gateway_contacts` 派生（spec 085）——也就是「后端已经知道的候选人」。
  一个刚加上好友、劳小智库里还没有档案的人，他发的消息网关**根本读不到**，
  于是「候选人主动发起 → AI 主动收集 → 建档」整条流程从第一步就走不下去。

★ **默认关**（``stranger_intake`` 开关）。名单是 fail-closed 白名单，
  它的存在理由就是「装上不会自动回复专员的家人朋友」（069 / 需求文档 §13.1）。
  放开这道口子是专员的一次知情选择，不是缺省行为。

★ 打开之后仍有两道闸兜着，缺一不可：
  1. **本机这层**：只准入「会话列表里有、我们不认识、不在 never 名单里」的名字，
     有数量上限、有保鲜期——不能攒成一份谁也没审过的长名单；
  2. **AI 端那层**：临时准入的会话，第一轮会带 ``first_contact``。AI 端判不出对方在求职
     就**一条都不发**（spec 090）。所以最坏情况是白读一次会话，不是给妈妈发招聘话术。

★ 只读**会话列表**，不打开任何聊天窗口——`read_chat_sessions` 读的是主窗口那个列表控件。
  节拍仍按 `pacing` 抖动 + 工作时间窗，理由见那份模块说明。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

import structlog

from wxclient.queue import TaskType

from .guard import within_active_hours
from .pacing import jittered
from .sessions import GatewayStore

_log = structlog.get_logger(__name__)

#: 临时准入的名额。这不是第二份名单——满了就不再收新的，等老的过期。
MAX_STRANGERS = 50

#: 保鲜期：这么久没在会话列表里露过面就清掉。
#: 队列是短期的（需求文档 §2.4），一个两周前来问过一句的人不该永远挂在准入名单上。
STRANGER_TTL_HOURS = 168.0

#: 查一趟会话列表的最小间隔。读列表比读聊天轻，但它仍然是在动微信主窗口，
#: 按「查好友」那一档走（300s），别按读消息那一档。
MIN_SCAN_INTERVAL_SECONDS = 300.0


class StrangerWatcher:
    """按自己的间隔扫一遍会话列表，把不认识的名字临时纳入监听。

    ``interval_seconds <= 0`` 或 ``enabled=False`` = 完全不扫（缺省）。
    """

    def __init__(
        self,
        *,
        accounts: Sequence[str],
        runtime,  # WeChatRuntime，或任何提供 submit(TaskType, payload) 的对象
        store: GatewayStore,
        interval_seconds: float,
        enabled: bool = False,
        active_hours: str = "",
        allow_real_actions: bool = False,
        confirmation: str = "",
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._accounts = list(accounts)
        self._runtime = runtime
        self._store = store
        self._interval = max(float(interval_seconds), MIN_SCAN_INTERVAL_SECONDS) if interval_seconds > 0 else 0.0
        self._enabled = bool(enabled)
        self._active_hours = active_hours
        self._allow_real = allow_real_actions
        # 真机上 dry_run=False 的读也要过 `WeChatRuntime._authorize_payload`，没带确认短语会被拒
        # （PermissionError → 下面只记一条 warning，扫描等于一直没跑）。
        self._confirmation = confirmation
        self._on_change = on_change
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng
        self._stopped = asyncio.Event()
        self._warned_disabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled and self._interval > 0 and bool(self._accounts)

    def _within_active_hours(self) -> bool:
        if not self._active_hours:
            return True
        return within_active_hours(self._clock(), self._active_hours)

    async def tick(self) -> list[str]:
        """扫一趟，返回**这一趟新准入**的会话名（所有账号合起来）。"""
        if not self.enabled or not self._within_active_hours():
            return []

        admitted: list[str] = []
        changed = False
        cutoff = self._clock() - timedelta(hours=STRANGER_TTL_HOURS)
        for account_id in self._accounts:
            expired = self._store.purge_strangers_before(account_id, cutoff)
            if expired:
                changed = True
                _log.info("临时准入过期，已移出监听", account=account_id, 数量=len(expired))
            fresh = await self._scan_account(account_id)
            admitted.extend(fresh)
            changed = changed or bool(fresh)
        # 加进来和过期出去**都要**重算名单：只认「加进来」的话，过期那些会一直留在
        # 监听名单里，保鲜期形同虚设。没变化就不重算，别白跑。
        if changed and self._on_change is not None:
            self._on_change()
        return admitted

    async def _scan_account(self, account_id: str) -> list[str]:
        try:
            # ⚠️ dry_run 必须显式给：底座默认 True，dry-run 下 `read_chat_sessions`
            # 返回的是空壳（`wechat_api/uia.py`），扫了等于没扫。
            result = await self._runtime.submit(
                TaskType.READ_CHAT_SESSIONS,
                {"dry_run": not self._allow_real},
                confirmation=self._confirmation if self._allow_real else "",
            )
        except Exception as exc:  # noqa: BLE001 —— 扫失败不该拖垮循环
            _log.warning("读会话列表失败", account=account_id, error=str(exc))
            return []

        names = session_names(result)
        if not names:
            return []
        known = self._store.known_session_names(account_id)
        fresh = [name for name in names if name not in known]
        if not fresh:
            return []
        added = self._store.note_strangers(account_id, fresh, now=self._clock(), limit=MAX_STRANGERS)
        if added:
            # 说出来：现场看到「有人加我了却没人理」时，这一行是唯一能解释发生了什么的线索。
            _log.info(
                "会话列表里有不认识的人，已临时纳入监听（第一轮由 AI 端判是不是求职咨询）",
                account=account_id,
                数量=len(added),
            )
        return added

    async def run(self) -> None:
        if not self.enabled:
            if not self._warned_disabled:
                self._warned_disabled = True
                _log.info("未开启「陌生人主动发起」接待——只处理名单里的会话")
            return
        self._stopped.clear()
        while not self._stopped.is_set():
            # 先睡后扫：启动那一刻已经有读消息在动微信了，别两件事挤在一起。
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


def session_names(result: dict | None) -> list[str]:
    """从 `read_chat_sessions` 的结果里取会话名。

    只认**这一档**：名字非空、且微信自己标的时间是今天。理由是准入的代价是「这个人会被
    自动回复」——把整个会话列表（几百个老会话）一次性倒进来，等于把 fail-closed 名单
    变成了 fail-open。今天有动静的那几个，才是「有人正在找我们」。

    读不到就是空列表：dry-run 下底座回的是空壳，这里不去替它编。
    """
    if not isinstance(result, dict):
        return []
    payload = result.get("result") if isinstance(result.get("result"), dict) else result
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in sessions:
        if not isinstance(item, dict):
            continue
        if not item.get("timestamp_is_today"):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out
