"""出站：把 AI 端产出的卡片翻译成微信消息，投递给队列串行发出。

网关**不生成任何话术**——`reply_segments` 里是什么就发什么，一段一条，不二次切分、
不改写、不补充。网关只决定「发不发」，不决定「发什么」。

真正操作微信的仍是既有的 ``queue -> wechat_api``，这里只 ``submit``。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import structlog

from .guard import ACTION_GROUP, ACTION_MESSAGE, OutboundGuard

_log = structlog.get_logger(__name__)


class MessageSink(Protocol):
    """出站投递口。生产实现把任务投进 SQLite 队列；测试用假实现。"""

    async def send(self, account_id: str, peer_id: str, text: str) -> str:
        """投递一条待发消息，返回任务 id。"""
        ...


@dataclass
class DeliveryResult:
    """一次出站的结果，供留痕与断言。"""

    sent: list[str] = field(default_factory=list)
    """已投递的任务 id。"""
    skipped_reason: str = ""
    """一条都没发时的原因；发了就是空串。"""
    actions: list[dict[str, Any]] = field(default_factory=list)
    """卡片里给业务后端的执行提案，原样带出，由上层转交。"""

    @property
    def delivered(self) -> bool:
        return bool(self.sent)


def extract_reply(card: dict[str, Any] | None) -> tuple[list[str], list[dict[str, Any]], str]:
    """从卡片里取出 (回复段, 执行提案, 不回复的理由)。

    只认契约里的形状；不合规的卡当成「不回复」处理，绝不猜。
    """
    if not isinstance(card, dict):
        return [], [], "没有卡片"
    body = card.get("body")
    if not isinstance(body, dict):
        return [], [], "卡片缺少 body"

    kind = body.get("kind")
    if kind == "notice":
        note = body.get("note")
        return [], [], f"AI 端返回提示卡：{note}" if isinstance(note, str) else "AI 端返回提示卡"
    if kind != "wechat_reply":
        return [], [], f"卡片不是 wechat_reply（kind={kind!r}）"

    raw_segments = body.get("reply_segments")
    segments = [s for s in raw_segments if isinstance(s, str) and s.strip()] if isinstance(raw_segments, list) else []
    actions = [a for a in body.get("actions", []) if isinstance(a, dict)] if isinstance(body.get("actions"), list) else []

    if not segments:
        reason = body.get("no_reply_reason")
        return [], actions, reason if isinstance(reason, str) and reason else "AI 端判定本轮不回复"
    return segments, actions, ""


class OutboundSender:
    """出站发送器：守门 → 逐段投递 → 记账。"""

    def __init__(
        self,
        *,
        sink: MessageSink,
        guard: OutboundGuard,
        store,  # GatewayStore（鸭子类型）
        clock=None,
    ) -> None:
        self._sink = sink
        self._guard = guard
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def deliver(
        self, account_id: str, peer_id: str, card: dict[str, Any] | None
    ) -> DeliveryResult:
        segments, actions, no_reply_reason = extract_reply(card)

        # 卡片里的拉黑提案先落地——即使这一轮不发消息，拉黑也要生效。
        self._apply_blacklist_actions(account_id, peer_id, actions)

        if not segments:
            _log.info("本轮不回复", account=account_id, 会话=peer_id, reason=no_reply_reason)
            return DeliveryResult(skipped_reason=no_reply_reason, actions=actions)

        now = self._clock()
        # 回到群里和回给一个人是两档配额（072）：群的误伤面大得多。
        action = ACTION_GROUP if self._store.is_known_group(account_id, peer_id) else ACTION_MESSAGE
        # ★ 一条回复常常拆成好几段，**每一段都是一次平台看得见的动作**。
        #   原来判一次就把整张卡发完，等于按 1 条记账、按 N 条打扰；而不同联系人的轮次
        #   还并发跑，两条一叠加就是「配额写着 20、实际发出去 40」。整批一次占住。
        reservation = self._guard.reserve(
            account_id, peer_id, now=now, action=action, count=len(segments),
        )
        if not reservation.allowed:
            _log.info("出站被守门拦下", account=account_id, 会话=peer_id, reason=reservation.reason)
            return DeliveryResult(skipped_reason=reservation.reason, actions=actions)

        task_ids: list[str] = []
        try:
            for segment in segments:
                task_ids.append(await self._sink.send(account_id, peer_id, segment))
        finally:
            # 发到一半炸了：没发出去的那几段不该占着额度（发出去的照记）。
            reservation.settle(len(task_ids))
        _log.info("已投递回复", account=account_id, 会话=peer_id, segments=len(task_ids))
        return DeliveryResult(sent=task_ids, actions=actions)

    def _apply_blacklist_actions(
        self, account_id: str, peer_id: str, actions: Sequence[dict[str, Any]]
    ) -> None:
        """AI 端判定拒绝 → 本地立即拉黑（第二道闸；第一道是入站关键词）。"""
        for action in actions:
            if action.get("type") == "blacklist":
                reason = action.get("reason")
                self._store.blacklist(
                    account_id,
                    peer_id,
                    reason=reason if isinstance(reason, str) else "AI 端判定对方拒绝联系",
                )
                _log.info("按 AI 端提案拉黑", account=account_id)
                return
