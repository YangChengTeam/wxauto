"""开场发送协调（AI 端 spec 138，按业务端《开场去重与岗位承接》对接文档）。

**要治的病**（2026-10-08 线上）：对方刚通过好友，两条路同时给他发了自我介绍——
业务端按计划下发的主动开场，和网关读到「我通过了你的朋友验证请求」当成候选人开口、
AI 端照常回的那一句。AI 端现在用一本账让两条路**共用一个开场名额**；本模块管网关这一侧：

1. **逐条报来源**（``delivery_context.messages``）：对方说的（user）、专员手打的（operator）、
   分身自己读回来的回声（assistant）、微信系统行（system）。来源按**本机已知的事实**认——
   控件类型、出站台账、好友关系台账——不按正文关键字猜。
2. **好友建立事件**：「这个人刚加上好友」是本机自己确定的事（我们发的申请被通过 / 我们通过了
   他的申请，见 `GatewayStore.open_relationship`）。第一次把这个会话交给 AI 端时带一条
   ``relationship_established`` 系统事件，事件的消息 ID 就是这次好友关系的 id——删了又加才换。
3. 对方通过申请时微信会替他发一句固定的「我通过了你的朋友验证请求……」。它长得和普通气泡一样，
   **只有**在「本机刚记下这次加好友、还没交给 AI 端」的那一批里、并且一字不差，才按系统行报；
   以后他自己手打同一句，照常是他说的话。

身份只报一个：本机微信的稳定账号标识（`accountRef`，和状态上报给业务端的是同一个值）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from .inbound import InboundMessage

#: 网关 ↔ AI 端能力握手里这项能力的键。
DELIVERY_COORDINATION = "delivery_coordination_v1"
#: 好友建立事件类型（AI 端协议的固定值）。
RELATIONSHIP_ESTABLISHED = "relationship_established"

#: 对方通过我们的申请时，微信替他发的那一句（跟系统语言走）。只在刚加上好友的那一批里认。
ACCEPT_NOTICE_TEXTS = frozenset({
    "我通过了你的朋友验证请求，现在我们可以开始聊天了",
    "我通過了你的朋友驗證請求，現在我們可以開始聊天了",
    "I've accepted your friend request. Now let's chat!",
})

#: AI 端协议上限：一批最多 64 条、单条正文 8192 字、整批正文 65536 字。
MAX_MESSAGES = 64
MAX_TEXT = 8192
MAX_BATCH_TEXT = 65_536


def wire_message_id(account_id: str, peer_id: str, msg_id: str) -> str:
    """本机消息号（``会话名#序号``，带中文）→ AI 端认得的不透明 ID。同一条永远同一个值。"""
    return "m-" + sha256(f"{account_id}\x00{peer_id}\x00{msg_id}".encode()).hexdigest()[:32]


def wire_time(value: datetime) -> str:
    """AI 端只收带 ``Z`` 的 UTC 时间。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def source_kind(message: InboundMessage) -> str:
    if message.system_tip:
        return "system"
    if message.from_gateway:
        return "assistant"
    if message.from_self:
        return "operator"
    return "user"


def build_delivery_context(
    messages: Sequence[InboundMessage],
    *,
    relationship: tuple[str, datetime] | None = None,
) -> dict[str, Any]:
    """这一批消息的逐条来源。``relationship`` = 本机记下、还没交给 AI 端的那次加好友 ``(id, 时刻)``。"""
    rows: list[dict[str, Any]] = []
    for message in messages:
        kind = source_kind(message)
        if relationship is not None and kind == "user" and message.text.strip() in ACCEPT_NOTICE_TEXTS:
            kind = "system"
        rows.append({
            "message_id": wire_message_id(message.account_id, message.peer_id, message.msg_id),
            "source_kind": kind,
            "text": message.text[:MAX_TEXT],
            "occurred_at": wire_time(message.sent_at or message.at),
        })
    if relationship is not None:
        relationship_id, established_at = relationship
        rows.insert(0, {
            "message_id": relationship_id,
            "source_kind": "system",
            "event_kind": RELATIONSHIP_ESTABLISHED,
            "text": "",
            "occurred_at": wire_time(established_at),
        })
    if len(rows) > MAX_MESSAGES:
        # 留住事件（排在最前）和最新的那些；中间被挤掉的是同一批里更早的话。
        head = rows[:1] if relationship is not None else []
        rows = head + rows[len(rows) - (MAX_MESSAGES - len(head)):]
    first = 1 if relationship is not None else 0
    while len(rows) > first + 1 and sum(len(row["text"]) for row in rows) > MAX_BATCH_TEXT:
        del rows[first]
    return {"messages": rows}
