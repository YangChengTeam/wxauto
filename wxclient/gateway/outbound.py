"""出站：把 AI 端产出的卡片翻译成微信消息，投递给队列串行发出。

网关**不生成任何话术**——`reply_segments` 里是什么就发什么，一段一条，不二次切分、
不改写、不补充。网关只决定「发不发」，不决定「发什么」。

真正操作微信的仍是既有的 ``queue -> wechat_api``，这里只 ``submit``。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import structlog

from .. import logrole
from .candidates import CandidateDirectory, CandidateUpdateError
from .guard import ACTION_GROUP, ACTION_MESSAGE, OutboundGuard
from .inbound import FOLLOW_UP_DEFER
from .message_events import DIRECTION_OUTBOUND, MessageEventReporter, local_message_id
from .sessions import HUMAN_TAKEOVER

_log = structlog.get_logger(__name__)

#: 跟业务后端打交道的那几行（Client Token 走的 HTTP）。
_backend = _log.bind(**{logrole.ROLE_KEY: logrole.BACKEND})
#: 这台电脑自己的事，不涉及任何对端。
_local = _log.bind(**{logrole.ROLE_KEY: logrole.LOCAL})


#: AI 端提案接管却没写原因时用的那句（业务端 v1.5 §4.1：接管必须带原因）。
AUTO_TAKEOVER_REASON = "候选人表示不再找工作或不希望被联系，已自动转人工接管"

#: 自动接管的落地口：``(会话名, 原因) -> …``。生产里是 `GatewayRunner.set_candidate_mode`
#: （先落本机、再同步业务端，和招聘老师按按钮同一条路）；没给就只落本机。
TakeoverFn = Callable[[str, str], Awaitable[Any]]


def takeover_reason(actions: Sequence[dict[str, Any]]) -> str | None:
    """卡上有没有「把这个人转给人工」的提案。有 → 原因（缺了用默认那句）；没有 → None。

    认两种形状：

    - ``set_ai_status`` 且 ``payload.status == human_takeover``——AI 端 2026-10-09 起对
      「明确拒绝联系 / 不再找工作」出的就是这个（用户：「不应该有拉黑的情况」）。
    - ``blacklist``——老 AI 端的拉黑提案。本机拉黑已经拆了，**按接管处置**，绝不再拉黑。
    """
    for action in actions:
        kind = action.get("type")
        payload = action.get("payload")
        status = payload.get("status") if isinstance(payload, dict) else None
        if kind == "blacklist" or (kind == "set_ai_status" and status == HUMAN_TAKEOVER):
            reason = action.get("reason")
            return reason.strip() if isinstance(reason, str) and reason.strip() else AUTO_TAKEOVER_REASON
    return None


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


@dataclass(frozen=True)
class HolidayGreeting:
    """卡上的节前祝福（AI 端 123）：业务端写的那句祝福接在第 ``index`` 段末尾，原文 = ``suffix``。

    AI 端在卡下发前已向业务端占位成功，``command_id`` 是业务端给的指令号——带祝福的那一段发出去后，
    出站消息事件**必须**带上它，业务端才会确认「祝过了」。``valid_until`` 过了就不能再祝（剥掉那句照发正文）。

    ★ 命令与内容不可变绑定：剥掉祝福的那一段**不带**这个指令号——指令号只跟着带祝福的原文走。
    """

    index: int
    """在 ``extract_reply`` 过滤后的段列表里的下标。"""
    suffix: str
    command_id: str
    valid_until: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.valid_until


def holiday_greeting(card: dict[str, Any] | None) -> tuple[HolidayGreeting | None, str]:
    """读卡上的 ``holiday_greeting``。返回 (可用的祝福, 不可用时追加原文)。

    - 没有槽位 → ``(None, "")``：普通回复。
    - 槽位齐全、原文确实在那一段末尾 → ``(HolidayGreeting, "")``。
    - 槽位缺指令号 / 截止时间读不懂 → ``(None, suffix)``：调用方把 suffix 剥掉再发（没占上的祝福不许发）。
    - 原文对不上那一段 → ``(None, "")``：文字里没有它，也就没什么可剥、不挂指令号。
    """
    if not isinstance(card, dict) or not isinstance(card.get("body"), dict):
        return None, ""
    body = card["body"]
    slot = body.get("holiday_greeting")
    if not isinstance(slot, dict):
        return None, ""
    raw = body.get("reply_segments")
    index = slot.get("segment_index")
    suffix = slot.get("suffix")
    if not isinstance(raw, list) or not isinstance(index, int) or isinstance(index, bool) or not isinstance(suffix, str) or not suffix:
        return None, ""
    if not 0 <= index < len(raw) or not isinstance(raw[index], str) or not raw[index].strip():
        return None, ""
    if not raw[index].endswith(suffix):
        return None, ""
    command_id = slot.get("command_id")
    valid_until = _parse_time(slot.get("valid_until"))
    if not isinstance(command_id, str) or not command_id.strip() or valid_until is None:
        return None, suffix
    # 过滤掉空段之后的下标（extract_reply 只保留非空字符串）。
    filtered = sum(1 for s in raw[:index] if isinstance(s, str) and s.strip())
    return HolidayGreeting(filtered, suffix, command_id.strip(), valid_until), ""


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _strip_suffix(segments: list[str], index: int, suffix: str) -> list[str]:
    """把第 ``index`` 段末尾的 ``suffix`` 剥掉；剥空了整段拿掉（别发一条空消息）。"""
    out = list(segments)
    if 0 <= index < len(out) and out[index].endswith(suffix):
        rest = out[index][: -len(suffix)]
        if rest.strip():
            out[index] = rest
        else:
            del out[index]
    return out


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
        takeover: TakeoverFn | None = None,
    ) -> None:
        self._sink = sink
        self._takeover = takeover
        self._guard = guard
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._candidates = candidates
        self._message_events = message_events

    async def deliver(
        self, account_id: str, peer_id: str, card: dict[str, Any] | None
    ) -> DeliveryResult:
        segments, actions, no_reply_reason = extract_reply(card)
        # AI 端 123 节前祝福：没占上 / 槽位不全的祝福先剥掉；占上的，发到那一段前再看一眼有没有过期。
        holiday, unclaimed_suffix = holiday_greeting(card)
        if unclaimed_suffix:
            for i, seg in enumerate(segments):
                if seg.endswith(unclaimed_suffix):
                    segments = _strip_suffix(segments, i, unclaimed_suffix)
                    break
            _local.info("节前祝福没有指令号，剥掉照发正文", account=account_id, 会话=peer_id)

        # 状态回写不等「这一轮发没发消息」：判定转人工的那一轮恰恰**一条都不发**
        # （v7 §10.4：微信侧不发「已转人工」），而 HR 正是要在那一刻看到状态变了。
        # （`human_takeover` 不走这条，见下面的 `handover`。）
        await self._apply_ai_status_actions(account_id, peer_id, actions)
        # ★ 对方拒绝联系 / 不再找工作 → 转人工接管（2026-10-09，替代原来的本机拉黑）。
        #   **排在这张卡的回复发完之后**：接管一落地，守门就一条都不放了——
        #   那句「好的，不打扰了」得先出门，不然它会被当成「接管中暂缓」攒着，
        #   等招聘老师交回分身的那一刻再冒出去，牛头不对马嘴。
        handover = takeover_reason(actions)

        if not segments:
            _local.info("本轮不回复", account=account_id, 会话=peer_id, reason=no_reply_reason)
            await self._hand_over(account_id, peer_id, handover)
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
            _local.info("出站被守门拦下", account=account_id, 会话=peer_id, reason=reservation.reason)
            await self._hand_over(account_id, peer_id, handover)
            # 配额用完 / 不在时间窗 = 等一等就能发，**别把候选人的话丢了**；
            # 本机关掉了这类动作则是明确结论，销账（审计 P0-4）。
            # ★ 这一轮要转人工的不留：留着的那句收尾话会在交回分身时重投出去。
            return DeliveryResult(
                skipped_reason=reservation.reason,
                actions=actions,
                follow_up=FOLLOW_UP_DEFER if reservation.temporary and handover is None else "",
            )

        task_ids: list[str] = []
        command_ids: list[str] = []
        sent_texts: list[str] = []
        try:
            for i, segment in enumerate(segments):
                command_id = ""
                if holiday is not None and i == holiday.index:
                    if holiday.expired(self._clock()):
                        # 过了业务端给的截止时间（守门排队、网关慢）：祝福不能再发，正文照发、不挂指令号。
                        segment = segment.removesuffix(holiday.suffix)
                        _local.info("节前祝福已过截止时间，剥掉照发正文", account=account_id, 会话=peer_id)
                        if not segment.strip():
                            continue
                    else:
                        command_id = holiday.command_id
                task_ids.append(await self._sink.send(account_id, peer_id, segment))
                command_ids.append(command_id)
                sent_texts.append(segment)
                # ★ 发成功一段就记一段原文：下一轮从微信读回来时靠它认出「这是我说的」，
                #   不再全押在 UI 的左右气泡判定上（判错 = 自问自答刷屏）。
                self._store.remember_sent_text(account_id, peer_id, segment, at=now)
        finally:
            # 发到一半炸了：没发出去的那几段不该占着额度（发出去的照记）。
            reservation.settle(len(task_ids))
            # 同理，真发出去的那几段照样要上报——「多久没回」是从**发出的那一刻**起算的。
            self._note_outbound(account_id, peer_id, task_ids, sent_texts, command_ids)
            # 发到一半炸了也要接管：他说了不找 / 别联系，这件事不因一条没发出去就不算数。
            await self._hand_over(account_id, peer_id, handover)
        _log.info("已投递回复", account=account_id, 会话=peer_id, segments=len(task_ids))
        return DeliveryResult(sent=task_ids, actions=actions)

    def _note_outbound(
        self,
        account_id: str,
        peer_id: str,
        task_ids: Sequence[str],
        segments: Sequence[str],
        command_ids: Sequence[str] = (),
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
            _backend.info("发出的回复没能归位到候选人，不上报消息事件", account=account_id)
            return
        now = self._clock()
        ids = list(command_ids) + [""] * (len(task_ids) - len(command_ids))
        for task_id, segment, command_id in zip(task_ids, segments, ids):
            self._message_events.record(
                account_id,
                candidate_id=candidate_id,
                direction=DIRECTION_OUTBOUND,
                # 微信读不到稳定的消息 ID，用队列任务 ID 派生一个本机稳定 ID（契约允许）。
                message_id=local_message_id(account_id, task_id),
                occurred_at=now,
                content=segment,
                source="auto_reply",
                # AI 端 123：带节前祝福的那一段挂业务端占位时给的指令号（业务端据此确认「祝过了」）；其余段不带。
                command_id=command_id,
            )

    async def _apply_ai_status_actions(
        self, account_id: str, peer_id: str, actions: Sequence[dict[str, Any]]
    ) -> None:
        """AI 端判定的候选人 AI 状态 → 写回业务后端（api2 §9.2）。

        **候选人 ID 由本机反查**：AI 端只知道「对面这个微信会话叫什么」，手机号↔候选人的
        关联全程不经它（081 裁决 2）。反查不到就如实跳过——那是「这个人还没上报过会话名 /
        后端没归到位」，不是错，更不该拿别人的 ID 顶上。
        """
        # `human_takeover` 走候选人级接管（`_hand_over`），不在这里 PATCH 计划状态：
        # 那条要求他在有效计划里，直聊来的人多半不在，409 之后什么都没停下。
        entries = [
            a for a in actions
            if a.get("type") == "set_ai_status" and takeover_reason([a]) is None
        ]
        if not entries or self._candidates is None or not self._candidates.configured:
            return
        candidate_id = self._store.contact_worker_id(account_id, peer_id)
        if not candidate_id:
            _backend.info("状态回写跳过：本机不知道这个会话对应哪个候选人", account=account_id, 会话=peer_id)
            return
        # 一轮只写一次：卡上真出现两条就是模型自相矛盾，取第一条，剩下的记一笔。
        if len(entries) > 1:
            _backend.warning("一张卡上有多条状态提案，只取第一条", count=len(entries))
        entry = entries[0]
        payload = entry.get("payload")
        status = (payload or {}).get("status") if isinstance(payload, dict) else None
        try:
            await self._candidates.update(
                candidate_id,
                ai_status={"status": status, "reason": entry.get("reason") or ""},
            )
        except CandidateUpdateError as exc:
            _backend.warning("状态回写请求不成立", error=str(exc))

    async def _hand_over(self, account_id: str, peer_id: str, reason: str | None) -> None:
        """把这个人转给人工（候选人级接管，091 那条路）。``reason`` 为 None = 这张卡没要求。

        ★ 已经是接管中就**不动**：那是招聘老师按的，原因也是老师写的，别拿一句自动的盖掉。
        ★ **永不往上抛**：同步失败本机照样生效（`set_candidate_mode` 的铁律），
          连本机都落不下也只记一笔——这一轮的消息已经发完了，不能因为这一步把整轮判失败。
        """
        if reason is None:
            return
        try:
            if self._store.candidate_mode(account_id, peer_id).mode == HUMAN_TAKEOVER:
                return
            if self._takeover is not None:
                await self._takeover(peer_id, reason)
            else:
                # 没接上 runner（单测 / 最小装配）：只落本机，闸照样成立。
                self._store.set_candidate_mode(
                    account_id, peer_id, HUMAN_TAKEOVER,
                    candidate_id=self._store.contact_worker_id(account_id, peer_id) or "", reason=reason,
                )
            _local.info("按 AI 端提案转人工接管", account=account_id, 会话=peer_id)
        except Exception as exc:  # noqa: BLE001 —— 理由见 docstring
            _local.warning("自动转人工接管没落成", account=account_id, 会话=peer_id, error=str(exc))
