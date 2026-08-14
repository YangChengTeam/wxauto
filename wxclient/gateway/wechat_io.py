"""微信侧的进出口：把队列任务的结果映射成 ``InboundMessage``，把回复投成发送任务。

**所有微信 UI 动作仍走 ``queue -> wechat_api``**（``CLAUDE.md`` §3）——这里只 submit，
一行都不碰窗口。

⚠️ 消息 id 的现实：微信 UI 读不到稳定的消息标识，`read_chat_history` 返回的每条只有
``{class_name, text, sender}``。所以「哪些是新消息」靠**游标签名**判定（见 ``sessions.py``），
``msg_id`` 只是给去重表用的单调序号，不承担判新职责。

⚠️ 本模块的字段映射来自对 `uia.py` 返回结构的静态阅读，**尚未在真机验证**。
真机联调（Phase C）时必须专门验这三条：
  ① 同一个人连发两条一模一样的消息，两条都要被处理；
  ② 第一次读一个会话，只接尾部今天、对方发的那几条，更早的历史不回溯；
  ③ 专员本人手打的消息 `sender == "我"`，要被标成 from_self。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import structlog

from wxclient.queue import TaskType

from .inbound import InboundMessage
from .sessions import GatewayStore
from .watchlist import Watchlist

_log = structlog.get_logger(__name__)

SELF_SENDER = "我"

#: 一轮最多读几个会话。见 ``QueueMessageSource._due_peers``——每读一个就要开一次聊天窗口。
MAX_PEERS_PER_POLL = 8


def is_self_message(raw: dict[str, Any]) -> bool:
    """这条是不是专员本人发的。

    真机上权威字段是 ``is_self``（`uia.py` 按气泡左右位置判定，逐条都有）；``sender``
    只有部分行会被标注（多数行根本没这个键），所以只能当兜底。
    **判错的代价极大**：网关自己发出去的回复下一轮会被原样读回来，认不出是自己说的就会
    当成新入站再回一轮 —— 自问自答刷屏。
    """
    flag = raw.get("is_self")
    if isinstance(flag, bool):
        return flag
    return str(raw.get("sender") or "") == SELF_SENDER


def is_timestamp_row(raw: dict[str, Any]) -> bool:
    """聊天记录里夹的时间分隔行（「18:28」「昨天」），不是消息。"""
    return bool(raw.get("is_timestamp"))


def message_signature(raw: dict[str, Any]) -> str:
    """一条消息的签名（游标用）。没有稳定 id，只能拿文本凑。

    ★ **绝不能把 `sender` / `is_self` 算进来**（2026-08-13 真机血的教训）：
    `uia.py::read_current_chat` 只给**最后一条**消息补这两个字段（靠会话预览文案 +
    右键菜单探测），其余行压根没有。而游标存的正是最后一条的签名——下一轮它不再是
    最后一条、不再被补字段，同一条消息就算出**另一个**哈希：

        21:25 存游标 aeea6b8f…（那时它是最后一条，sender 被补成「我」）
        21:31 同一条消息再读 → 没人补字段 → 算出别的哈希 → 「游标已滚出读取窗口」

    于是游标**永远**对不回来，`fresh` 永远是空，**自动回复一次都触发不了**
    （真机实证：`gateway_cursors.seq` 一直是 0、`gateway_sessions` 一条没有）。
    文本是唯一两次读取都稳定的字段，就只拿它。同文重复的代价是定位到最后一次出现，
    比"永远定位不到"轻得多。
    """
    return sha256(str(raw.get("text") or "").encode()).hexdigest()[:32]


def legacy_message_signature(raw: dict[str, Any]) -> str:
    """旧签名（含 sender/is_self）。**只用于匹配**库里已有的老游标，不再写入。"""
    sender = "self" if is_self_message(raw) else str(raw.get("sender") or "peer")
    text = str(raw.get("text") or "")
    return sha256(f"{sender}\x00{text}".encode()).hexdigest()[:32]


def cursor_candidates(raw: dict[str, Any]) -> set[str]:
    """这一行**可能**被记成游标的所有签名形态。

    老游标是在「它当过最后一条、被补了 `sender='我'` / `is_self=True`」的那一刻算的，
    而现在再读到它时那些字段没了——所以光按当前字段算旧签名也认不回来，
    得把「当作自己发的」那一版也一并认掉。否则换算法当天每个会话都要先白丢一条消息。
    """
    text = str(raw.get("text") or "")
    return {
        message_signature(raw),
        legacy_message_signature(raw),
        sha256(f"self\x00{text}".encode()).hexdigest()[:32],
    }


def split_new_messages(
    raw_messages: Sequence[dict[str, Any]], last_signature: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """按游标切出新消息，返回 (新消息, 新游标签名)。

    - 游标为空（第一次读这个会话）→ 只取**尾部今天的、对方发的**那几条，见 ``first_seen_fresh``。
    - 游标找不到（历史滚出了读取窗口）→ 保守处理：不回溯，只对齐游标。
    """
    if not raw_messages:
        return [], last_signature

    latest_signature = message_signature(raw_messages[-1])
    if last_signature is None:
        return first_seen_fresh(raw_messages), latest_signature

    # 从后往前找游标最后一次出现的位置（老游标按旧算法也认一次，见 legacy_message_signature）。
    cursor_index = -1
    for index in range(len(raw_messages) - 1, -1, -1):
        if last_signature in cursor_candidates(raw_messages[index]):
            cursor_index = index
            break

    if cursor_index < 0:
        # 这条**不是**日常噪音：正常情况下游标就在刚读到的这批里。连着出现 =
        # 签名不稳定或读取窗口太小，后果是自动回复永远不触发（2026-08-13 真机踩过），
        # 所以报警告级别，别让它混在 info 里没人看见。
        _log.warning(
            "游标在刚读到的记录里找不到，只对齐游标不回溯；连着出现说明游标机制失效了",
            已读到=len(raw_messages),
        )
        return [], latest_signature

    return list(raw_messages[cursor_index + 1:]), latest_signature


#: 第一次读一个会话时，最多把尾部几条当成待处理。够装下「连发三条」那种，
#: 又不至于把一屏历史整个倒给模型。
FIRST_SEEN_TAIL = 5


def first_seen_fresh(raw_messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """第一次读这个会话时，哪些算「要处理的新消息」。

    ★ 原来是**一条都不算**，只对齐游标——理由是「网关刚上线不能对着几个月历史逐条回复」。
    可那是「先扫未读、被动等消息」时代的设定。现在是名单驱动、直接读会话：把一个人
    加进名单的那一刻，他很可能刚问完话正等着，而「首次不回」意味着他永远等不到——
    除非他再发一条（用户 2026-08-14：「没有首次见到这一说」）。

    两边都要：不倒着回历史，也不能把刚问的话咽掉。判据就是**时间 + 谁说的**——
    只认尾部连续的、今天的、对方发的那几条：

    - 尾部是专员自己发的 → 停（他已经在人工接着了，轮不到分身插话）；
    - 遇到不是今天的 → 停（昨天以前的都是历史，不回溯）；
    - 最多 ``FIRST_SEEN_TAIL`` 条。

    没有时间分隔行时 ``timestamp_is_today`` 是 None —— 当今天处理（宁可回一条稍旧的，
    也好过把刚到的消息咽掉；真正的老历史前面必然夹着日期分隔行）。
    """
    fresh: list[dict[str, Any]] = []
    for raw in reversed(raw_messages):
        if is_self_message(raw):
            break
        if raw.get("timestamp_is_today") is False:
            break
        fresh.append(raw)
        if len(fresh) >= FIRST_SEEN_TAIL:
            break
    fresh.reverse()
    return fresh


def to_inbound(
    account_id: str,
    peer_id: str,
    raw: dict[str, Any],
    *,
    seq: int,
    at: datetime | None = None,
) -> InboundMessage:
    return InboundMessage(
        account_id=account_id,
        peer_id=peer_id,
        msg_id=f"{peer_id}#{seq}",
        text=str(raw.get("text") or ""),
        at=at or datetime.now(timezone.utc),
        from_self=is_self_message(raw),
        peer_name=peer_id,
        at_text=str(raw.get("timestamp_text") or "").strip(),
    )


class QueueMessageSource:
    """入站来源：**名单里的会话** → 逐个读聊天记录 → 按游标切出新消息。

    ``peer_id`` 用的是**微信会话名**——微信 UI 自动化能拿到的就是这个，
    ``wechat_api`` 的所有动作也都按会话名寻址。
    """

    def __init__(
        self,
        *,
        runtime,  # WeChatRuntime 或任何提供 submit(TaskType, payload) 的对象
        store: GatewayStore,
        watchlist: Watchlist | None = None,
        history_limit: int = 20,
        clock=None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._watchlist = watchlist or Watchlist()
        self._history_limit = history_limit
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rotation = 0
        self._unreachable: set[str] = set()

    def skip_reason(self, account_id: str, peer_id: str) -> str | None:
        """这个会话该不该碰；返回不碰的理由，None 表示可以处理。

        判定放在**打开会话之前**：打开一个聊天窗口本身就是打扰（窗口会弹到前台，
        还会把未读清掉），不该为了判断该不该回而先去动它。名单本身由 AI 端推下来
        （``watchlist.py``），这里只负责照办。

        ★ 判据**只有名单**，不再看「是不是群」（用户裁决 2026-08-14）：要不要管一个群，
        是 AI 端那份名单说了算的业务判断。这也顺带解掉了老问题——判断是不是群本来就得
        先把会话打开才知道，等于为了「决定要不要打扰」先打扰一次。
        """
        return self._watchlist.reason(peer_id)

    async def fetch(self, account_id: str) -> list[InboundMessage]:
        """读名单里的会话，按游标切出新消息。

        ★ **不看微信的未读**（用户裁决 2026-08-14）。未读这个信号根本不可靠：
        群多半设了消息免打扰、不进未读；专员在手机上瞄过一眼的也不再是未读——
        于是「该回的消息网关压根看不见」。名单本来就是「要盯的会话」清单，
        直接读它、按游标判新，跟「微信觉得你读没读过」彻底脱钩。
        """
        # 名单空着 = 一个会话都不碰。这时候必须喊一嗓子，否则现场表现是
        # 「微信有新消息、网关一声不吭」，看的人只会以为功能坏了（069）。
        self._watchlist.warn_if_idle()
        watched = self._watchlist.watched()
        if not watched:
            return []

        collected: list[InboundMessage] = []
        for peer_id in self._due_peers(watched):
            collected.extend(await self._fetch_peer(account_id, peer_id))
        return collected

    def _due_peers(self, watched: Sequence[str]) -> list[str]:
        """这一轮读哪几个。名单长了就轮着来，一轮最多 ``MAX_PEERS_PER_POLL`` 个。

        读一个会话 = 打开一次聊天窗口。名单有五十个人时，每轮把五十个窗口挨个点一遍
        既慢又正是风控最不喜欢的形状（2026-08-12 自动退登事故）。轮转的代价只是
        「名单很长时，某个人的消息最多晚几轮被看到」——比被封号强得多。
        """
        if len(watched) <= MAX_PEERS_PER_POLL:
            return list(watched)
        start = self._rotation % len(watched)
        self._rotation = start + MAX_PEERS_PER_POLL
        doubled = list(watched) + list(watched)
        due = doubled[start:start + MAX_PEERS_PER_POLL]
        _log.info("名单较长，本轮只读其中一段", 名单=len(watched), 本轮=len(due))
        return due

    async def _fetch_peer(self, account_id: str, peer_id: str) -> list[InboundMessage]:
        history = await self._runtime.submit(
            TaskType.READ_CHAT_HISTORY, {"friend": peer_id, "limit": self._history_limit}
        )
        # 读不到要说清楚是**哪个名字**读不到。名单里的名字必须和微信里显示的**一字不差**
        # （网关按会话名寻址），差一个空格就是「查无此人」——而这条错误如果只落在队列日志里，
        # 现场表现就成了「名单加了、可他就是不回」，谁也想不到是名字对不上（2026-08-14 实测）。
        if str((history or {}).get("status") or "") not in {"", "succeeded"}:
            reason = str((history or {}).get("last_error") or "").strip()
            # 只在「刚开始读不到」时喊一次：轮询是分钟级的，每轮一条会把日志刷满，
            # 真正有用的信息（第一次是什么时候、为什么）反而被埋掉。
            if peer_id not in self._unreachable:
                self._unreachable.add(peer_id)
                _log.warning("名单里的这个会话读不到，先跳过", 会话=peer_id, 原因=reason[:120])
            return []
        if peer_id in self._unreachable:
            self._unreachable.discard(peer_id)
            _log.info("名单里的这个会话又能读了", 会话=peer_id)
        raw_messages = ((history or {}).get("result") or {}).get("messages") or []
        # 时间分隔行必须在切游标**之前**滤掉：它会被当成一条消息发给 AI，还会把游标钉在
        # 一个随时间变化的字符串上（真机读回来过一行「18:28」）。
        raw_messages = [raw for raw in raw_messages if not is_timestamp_row(raw)]

        # 是不是群只有打开会话才判得出来（未读列表里没有这个信息）。记下来只为诊断和日志——
        # **不再拿它决定处理与否**（069）：群和单聊一样都只看监听名单。
        if any(raw.get("is_group") for raw in raw_messages):
            self._store.mark_peer_kind(account_id, peer_id, is_group=True)

        last_signature, seq = self._store.get_cursor(account_id, peer_id)
        fresh, new_signature = split_new_messages(raw_messages, last_signature)
        if last_signature is None and raw_messages:
            # 第一次读这个会话：只接尾部今天、对方发的那几条（见 first_seen_fresh），
            # 更早的算历史不回溯。**必须说出来**——不然日志上就是「读完了 N 条」然后
            # 再无下文，看的人只会以为回复功能坏了（2026-08-13 真机，用户就是这么问的）。
            _log.info(
                "第一次读这个会话，只接尾部今天、对方发的那几条，更早的算历史不回溯",
                account=account_id,
                peer=peer_id,
                已读到=len(raw_messages),
                要处理=len(fresh),
            )
        if new_signature is not None:
            self._store.set_cursor(account_id, peer_id, new_signature, seq + len(fresh))

        now = self._clock()
        return [
            to_inbound(account_id, peer_id, raw, seq=seq + offset + 1, at=now)
            for offset, raw in enumerate(fresh)
            if str(raw.get("text") or "").strip()
        ]


class QueueMessageSink:
    """出站投递：把一段回复投成 ``SEND_MESSAGE`` 任务，由单消费者 worker 串行发出。

    ``allow_real_actions=False``（默认）时投的是 ``dry_run`` 任务——走完整条链路
    但不真的发出去。联调期就用这个模式。
    """

    def __init__(self, *, runtime, allow_real_actions: bool = False, confirmation: str = "") -> None:
        self._runtime = runtime
        self._allow_real = allow_real_actions
        self._confirmation = confirmation

    async def send(self, account_id: str, peer_id: str, text: str) -> str:
        payload = {"friend": peer_id, "text": text, "dry_run": not self._allow_real}
        result = await self._runtime.submit(
            TaskType.SEND_MESSAGE,
            payload,
            confirmation=self._confirmation if self._allow_real else "",
        )
        task_id = str((result or {}).get("task_id") or "")
        _log.info(
            "已投递发送任务",
            account=account_id,
            dry_run=not self._allow_real,
            task_id=task_id,
        )
        return task_id
