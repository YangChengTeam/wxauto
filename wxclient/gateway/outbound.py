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

from .candidates import CandidateDirectory, CandidateUpdateError
from .guard import ACTION_GROUP, ACTION_MESSAGE, OutboundGuard
from .inbound import FOLLOW_UP_DEFER
from .message_events import DIRECTION_OUTBOUND, MessageEventReporter, local_message_id

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
    follow_up: str = ""
    """这批入站消息的去向：空 = 有结论了，销账；否则见 `inbound.FOLLOW_UP_*`。"""

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
        candidates: CandidateDirectory | None = None,
        message_events: MessageEventReporter | None = None,
    ) -> None:
        self._sink = sink
        self._guard = guard
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._candidates = candidates
        self._message_events = message_events

    async def deliver(
        self, account_id: str, peer_id: str, card: dict[str, Any] | None
    ) -> DeliveryResult:
        segments, actions, no_reply_reason = extract_reply(card)

        # 卡片里的拉黑提案先落地——即使这一轮不发消息，拉黑也要生效。
        self._apply_blacklist_actions(account_id, peer_id, actions)
        # 同理，状态回写也不等「这一轮发没发消息」：判定转人工的那一轮恰恰**一条都不发**
        # （v7 §10.4：微信侧不发「已转人工」），而 HR 正是要在那一刻看到状态变了。
        await self._apply_ai_status_actions(account_id, peer_id, actions)

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
            # 配额用完 / 不在时间窗 = 等一等就能发，**别把候选人的话丢了**；黑名单、
            # 本机关掉了这类动作则是明确结论，销账（审计 P0-4）。
            return DeliveryResult(
                skipped_reason=reservation.reason,
                actions=actions,
                follow_up=FOLLOW_UP_DEFER if reservation.temporary else "",
            )

        task_ids: list[str] = []
        try:
            for segment in segments:
                task_ids.append(await self._sink.send(account_id, peer_id, segment))
        finally:
            # 发到一半炸了：没发出去的那几段不该占着额度（发出去的照记）。
            reservation.settle(len(task_ids))
            # 同理，真发出去的那几段照样要上报——「多久没回」是从**发出的那一刻**起算的。
            self._note_outbound(account_id, peer_id, task_ids, segments)
        _log.info("已投递回复", account=account_id, 会话=peer_id, segments=len(task_ids))
        return DeliveryResult(sent=task_ids, actions=actions)

    def _note_outbound(
        self, account_id: str, peer_id: str, task_ids: Sequence[str], segments: Sequence[str]
    ) -> None:
        """我方消息发出去了 → 登记一条 outbound 事件（业务后端据此排「未回复跟进」）。

        **一段一条**：一条回复常常拆成好几段，每一段都是对方手机上真真切切的一次响动。
        后端按最后一条重排跟进时点，正好是我们希望的「从最后说完那句起算」。

        **候选人 ID 由本机反查**，反查不到就如实跳过（契约「对接注意事项」最后一条）——
        那是「这个人还没上报过会话名 / 后端没归到位」，不是错，更不能拿别人的 ID 顶上。

        ★ 只走流程不真动微信的机器压根不会有这个上报器（见 `factory.py`），
        所以这里不必再判一次 dry-run。
        """
        if self._message_events is None or not task_ids:
            return
        candidate_id = self._store.contact_worker_id(account_id, peer_id)
        if not candidate_id:
            _log.info("发出的回复没能归位到候选人，不上报消息事件", account=account_id)
            return
        now = self._clock()
        for task_id, segment in zip(task_ids, segments):
            self._message_events.record(
                account_id,
                candidate_id=candidate_id,
                direction=DIRECTION_OUTBOUND,
                # 微信读不到稳定的消息 ID，用队列任务 ID 派生一个本机稳定 ID（契约允许）。
                message_id=local_message_id(account_id, task_id),
                occurred_at=now,
                content=segment,
                source="auto_reply",
            )

    async def _apply_ai_status_actions(
        self, account_id: str, peer_id: str, actions: Sequence[dict[str, Any]]
    ) -> None:
        """AI 端判定的候选人 AI 状态 → 写回业务后端（api2 §9.2）。

        **候选人 ID 由本机反查**：AI 端只知道「对面这个微信会话叫什么」，手机号↔候选人的
        关联全程不经它（081 裁决 2）。反查不到就如实跳过——那是「这个人还没上报过会话名 /
        后端没归到位」，不是错，更不该拿别人的 ID 顶上。
        """
        entries = [a for a in actions if a.get("type") == "set_ai_status"]
        if not entries or self._candidates is None or not self._candidates.configured:
            return
        candidate_id = self._store.contact_worker_id(account_id, peer_id)
        if not candidate_id:
            _log.info("状态回写跳过：本机不知道这个会话对应哪个候选人", account=account_id, 会话=peer_id)
            return
        # 一轮只写一次：卡上真出现两条就是模型自相矛盾，取第一条，剩下的记一笔。
        if len(entries) > 1:
            _log.warning("一张卡上有多条状态提案，只取第一条", count=len(entries))
        entry = entries[0]
        payload = entry.get("payload")
        status = (payload or {}).get("status") if isinstance(payload, dict) else None
        try:
            await self._candidates.update(
                candidate_id,
                ai_status={"status": status, "reason": entry.get("reason") or ""},
            )
        except CandidateUpdateError as exc:
            _log.warning("状态回写请求不成立", error=str(exc))

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
