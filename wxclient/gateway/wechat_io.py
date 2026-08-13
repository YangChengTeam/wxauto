"""微信侧的进出口：把队列任务的结果映射成 ``InboundMessage``，把回复投成发送任务。

**所有微信 UI 动作仍走 ``queue -> wechat_api``**（``CLAUDE.md`` §3）——这里只 submit，
一行都不碰窗口。

⚠️ 消息 id 的现实：微信 UI 读不到稳定的消息标识，`read_chat_history` 返回的每条只有
``{class_name, text, sender}``。所以「哪些是新消息」靠**游标签名**判定（见 ``sessions.py``），
``msg_id`` 只是给去重表用的单调序号，不承担判新职责。

⚠️ 本模块的字段映射来自对 `uia.py` 返回结构的静态阅读，**尚未在真机验证**。
真机联调（Phase C）时必须专门验这三条：
  ① 同一个人连发两条一模一样的消息，两条都要被处理；
  ② 网关首次上线，不能对着历史记录逐条回复；
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
    """一条消息的签名。没有稳定 id，只能拿发送方 + 文本凑。"""
    sender = "self" if is_self_message(raw) else str(raw.get("sender") or "peer")
    text = str(raw.get("text") or "")
    return sha256(f"{sender}\x00{text}".encode()).hexdigest()[:32]


def split_new_messages(
    raw_messages: Sequence[dict[str, Any]], last_signature: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """按游标切出新消息，返回 (新消息, 新游标签名)。

    - 游标为空（首次见到这个联系人）→ **不返回任何新消息**，只把游标对齐到最新。
      网关刚上线就照着几个月历史逐条回复是灾难。
    - 游标找不到（历史滚出了读取窗口）→ 同样保守处理：不回溯，只对齐游标。
    """
    if not raw_messages:
        return [], last_signature

    latest_signature = message_signature(raw_messages[-1])
    if last_signature is None:
        return [], latest_signature

    # 从后往前找游标最后一次出现的位置。
    cursor_index = -1
    for index in range(len(raw_messages) - 1, -1, -1):
        if message_signature(raw_messages[index]) == last_signature:
            cursor_index = index
            break

    if cursor_index < 0:
        _log.info("游标已滚出读取窗口，跳过回溯只对齐游标")
        return [], latest_signature

    return list(raw_messages[cursor_index + 1:]), latest_signature


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
    )


class QueueMessageSource:
    """入站来源：未读会话 → 逐个读聊天记录 → 按游标切出新消息。

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

    def skip_reason(self, account_id: str, peer_id: str) -> str | None:
        """这个会话该不该碰；返回不碰的理由，None 表示可以处理。

        判定放在**打开会话之前**：打开一个聊天窗口本身就是打扰（窗口会弹到前台，
        还会把未读清掉），不该为了判断该不该回而先去动它。名单本身由 AI 端推下来
        （``watchlist.py``），这里只负责照办。
        """
        return self._watchlist.reason(
            peer_id, is_group=self._store.is_known_group(account_id, peer_id)
        )

    @property
    def allow_groups(self) -> bool:
        return self._watchlist.scope.allow_groups

    async def fetch(self, account_id: str) -> list[InboundMessage]:
        unread = await self._runtime.submit(TaskType.GET_UNREAD_MESSAGES, {})
        sessions = ((unread or {}).get("result") or {}).get("unread_sessions") or []

        collected: list[InboundMessage] = []
        for session in sessions:
            name = str((session or {}).get("name") or "").strip()
            if not name:
                continue
            reason = self.skip_reason(account_id, name)
            if reason is not None:
                _log.debug("跳过该会话", account=account_id, reason=reason)
                continue
            collected.extend(await self._fetch_peer(account_id, name))
        return collected

    async def _fetch_peer(self, account_id: str, peer_id: str) -> list[InboundMessage]:
        history = await self._runtime.submit(
            TaskType.READ_CHAT_HISTORY, {"friend": peer_id, "limit": self._history_limit}
        )
        raw_messages = ((history or {}).get("result") or {}).get("messages") or []
        # 时间分隔行必须在切游标**之前**滤掉：它会被当成一条消息发给 AI，还会把游标钉在
        # 一个随时间变化的字符串上（真机读回来过一行「18:28」）。
        raw_messages = [raw for raw in raw_messages if not is_timestamp_row(raw)]

        # 是不是群，只有打开会话读到消息才判得出来（未读列表里没有这个信息）。判出来就记下，
        # 下一轮在 skip_reason 里直接拦掉，不用再开一次窗口。
        if any(raw.get("is_group") for raw in raw_messages):
            self._store.mark_peer_kind(account_id, peer_id, is_group=True)
            if not self.allow_groups:
                # 游标仍然对齐到最新：不处理不等于要在下次把这些当成新消息。
                if raw_messages:
                    self._store.set_cursor(
                        account_id, peer_id, message_signature(raw_messages[-1]), 0
                    )
                _log.info("群聊消息不处理", account=account_id)
                return []

        last_signature, seq = self._store.get_cursor(account_id, peer_id)
        fresh, new_signature = split_new_messages(raw_messages, last_signature)
        if last_signature is None and raw_messages:
            # 首次见到这个会话：按设计只对齐游标、不回溯（否则网关一上线就照着几个月历史
            # 逐条回复）。但**必须说出来**——不然日志上就是「读完了 N 条」然后再无下文，
            # 看的人只会以为回复功能坏了（2026-08-13 真机，用户就是这么问的）。
            _log.info(
                "首次见到这个会话，只对齐游标不回溯；下一条新消息才会触发回复",
                account=account_id,
                peer=peer_id,
                已读到=len(raw_messages),
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
