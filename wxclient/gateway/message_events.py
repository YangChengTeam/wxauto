"""微信消息事件上报（``POST /client/message-events``）——「多久没回」的唯一事实来源。

契约：`docs/招聘分身_客户端微信消息事件上报_v1.md`。

业务后端要按**真实的微信事件**排「未回复跟进」：我方消息发出去了才开始计时，候选人一开口
就把待跟进取消掉。**这台电脑只上报事实，不判断该不该发第二条**——四小时该不该追、追什么，
全在后端的计划里（客户端自己再排一次 = 同一条跟进发两遍）。

三条规矩，破一条整块就白做：

- **发件箱先落盘，再慢慢发**（``GatewayStore.put_message_event``）。上报走网络，网络会抖；
  抖一下就丢的话，代价不是少一行日志，而是候选人明明回了话、四小时后还被追一条。
  记账的活也绝不能挡在微信动作前面——所以 ``record()`` 是同步的、只写本机 SQLite，
  真正发请求的是后台那条 ``flush()``。
- **eventId 由内容派生**（``event_id_for``），重试多少次都是同一个值，后端据它幂等去重。
- **归不了位就不报**（契约「对接注意事项」最后一条）：本机反查不到候选人 ID 时如实跳过，
  绝不编一个 ID 顶上——后端会把这条事件算到别人头上。

**只走流程不真动微信的机器一条都不报**：装配层（``factory.py``）在 ``allow_real_actions``
为假时压根不建这个上报器。dry-run 的一轮里微信侧什么都没发生，把它报成「已发送」
就是拿假动作去改真数据（同 ``commands.py`` 里 ``update_candidate`` 的口径）。

不上传聊天正文：只带 ``contentDigest`` 摘要，``messageId`` 也是哈希过的本机 ID。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

import structlog

from .backend_client import BackendEndpoint

_log = structlog.get_logger(__name__)

#: ⚠️ 契约正文写的是 `/api/recruitment-agent/...`，这里**刻意不带 `/api`**。
#: 2026-08-24 对线上实测过（后端当天已部署）：
#:   POST /recruitment-agent/client/message-events      → 401 CLIENT_TOKEN_REQUIRED（就是这条路）
#:   POST /api/recruitment-agent/client/message-events  → 401 UNAUTHORIZED（另一套网页鉴权，
#:                                                        客户端 Token 在那儿根本不作数）
#: 所以带上 `/api` 反而永远进不去。别动别的接口——那几条也都是这个前缀。
MESSAGE_EVENTS_PATH = "/recruitment-agent/client/message-events"

#: 契约的两个方向。`outbound` = 我方发给候选人，`inbound` = 候选人发来。
DIRECTION_OUTBOUND = "outbound"
DIRECTION_INBOUND = "inbound"
_DIRECTIONS = frozenset({DIRECTION_OUTBOUND, DIRECTION_INBOUND})

#: 契约的 `messageType` 枚举。不认识的值一律不发——后端会 400，而我们还以为报过了。
MESSAGE_TYPES = frozenset(
    {"text", "image", "voice", "video", "file", "link", "emoji", "unknown"}
)

#: 契约：`outbound` 默认 `sent`，`inbound` 默认 `received`。显式带上，不靠对端的默认值。
_STATUS_BY_DIRECTION = {DIRECTION_OUTBOUND: "sent", DIRECTION_INBOUND: "received"}

#: 契约：`messageId` 长度 1–180。
MAX_MESSAGE_ID_LENGTH = 180

#: 契约：`commandId` 长度 1–120（后端建表就是 varchar(120)，超了整条事件被 400 打回）。
#: 超长时**丢掉这个字段、保住事件**：截断得到的是一个对不上任何指令的假 ID，而事件本身
#: （「这一刻发出去了一条消息」）才是后端排跟进要的东西——为一个对账字段把它整条丢掉不划算。
MAX_COMMAND_ID_LENGTH = 120

#: eventId 的命名空间。写成从 URL 派生而不是拍一个魔法常量，是为了让它自解释：
#: 换了这个字符串，全世界的 eventId 都会变，后端的幂等去重会认不出重试。**永不改动**。
EVENT_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://laoxiaozhi.cn/recruitment-agent/client/message-events"
)

#: 一次 flush 最多发几条。发件箱平时是空的，攒起来只会是后端断了一阵子。
FLUSH_BATCH = 50

#: 同一条事件最多重试几次。轮询是分钟级的，20 次覆盖一两个小时的中断（同待处理批次）。
MAX_ATTEMPTS = 20

#: 事件的保鲜期。隔了一天才报上去，后端拿它算出来的跟进时点早就过了。
MAX_AGE_HOURS = 24

#: 这些 HTTP 状态是「等一等还能成」：认证被撤销要等重新登录，429/5xx 要等对端缓过来。
#: 其余 4xx 是**我们发错了**，重试一万次也一样——丢掉并留一条警告，别把发件箱堵死。
_RETRY_STATUSES = frozenset({401, 403, 408, 425, 429})

_OK, _RETRY, _DROP = "ok", "retry", "drop"


class MessageEventError(ValueError):
    """这条事件本身不成立（缺 ID、方向不认识、类型不认识）。调用方据此跳过。"""


def content_digest(text: str) -> str:
    """正文摘要。**绝不上传完整聊天正文**（契约「对接注意事项」）。

    截到 32 位十六进制：契约只要「摘要」，够排查时对上是不是同一条就行；短一点也少一点
    「拿摘要去撞库还原原文」的空间。
    """
    return "sha256:" + sha256((text or "").encode()).hexdigest()[:32]


def local_message_id(*parts: object) -> str:
    """本机稳定唯一 ID。微信 UI 读不到稳定的消息标识（见 ``wechat_io`` 模块说明），
    契约允许拿本机 ID 顶上，只要求它**稳定不变**。

    哈希而不是原样拼接：本机的 ``msg_id`` 形如 ``{会话名}#{序号}``，里面那个会话名是专员
    给候选人设的备注，没必要跟着每条事件出门。派生是确定性的，重试仍然给同一个值。
    """
    raw = "|".join(str(part) for part in parts)
    return "wxlocal_" + sha256(raw.encode()).hexdigest()[:32]


def event_id_for(candidate_id: str, direction: str, message_id: str) -> str:
    """事件的幂等键。**由内容派生，不用随机数**——断网重投必须还是同一个值（契约「幂等规则」）。

    落盘之外还派生一次的理由：发件箱可能在断电时丢半条，而派生出来的值不依赖任何存储。
    """
    return str(uuid.uuid5(EVENT_NAMESPACE, f"{candidate_id}|{direction}|{message_id}"))


def _iso(moment: datetime) -> str:
    """契约要求「ISO datetime with offset」。本机全程用 UTC，写成 ``…Z``（同 contacts.py）。"""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def build_event(
    *,
    candidate_id: str,
    direction: str,
    message_id: str,
    occurred_at: datetime,
    message_type: str = "text",
    command_id: str = "",
    content: str = "",
    source: str = "",
) -> dict[str, Any]:
    """按契约拼一条事件。形状不对抛 ``MessageEventError``——那是上游写错了，不该默默发出去。

    ``content`` 只用来算摘要，**原文一个字都不进 payload**。
    """
    candidate_id = (candidate_id or "").strip()
    if not candidate_id:
        raise MessageEventError("没说是哪个候选人")
    if direction not in _DIRECTIONS:
        raise MessageEventError(f"不认识的方向：{direction or '(空)'}")
    if message_type not in MESSAGE_TYPES:
        raise MessageEventError(f"不认识的消息类型：{message_type or '(空)'}")
    message_id = (message_id or "").strip()[:MAX_MESSAGE_ID_LENGTH]
    if not message_id:
        raise MessageEventError("没有消息 ID")

    event: dict[str, Any] = {
        "eventId": event_id_for(candidate_id, direction, message_id),
        "candidateId": candidate_id,
        "direction": direction,
        "messageType": message_type,
        "messageId": message_id,
        "status": _STATUS_BY_DIRECTION[direction],
        "occurredAt": _iso(occurred_at),
    }
    if command_id and direction == DIRECTION_OUTBOUND:
        # 契约：候选人入站消息不要带 commandId（后端会 400，不是忽略）。
        if len(command_id) <= MAX_COMMAND_ID_LENGTH:
            event["commandId"] = command_id
        else:
            _log.warning("指令 ID 太长，这条事件不带 commandId", length=len(command_id))
    if content:
        event["contentDigest"] = content_digest(content)
    if source:
        # 只带调度事实，方便两端对账「这条是自动回复还是计划指令」。不放任何正文或姓名。
        event["metadata"] = {"source": source}
    return event


class MessageEventReporter(BackendEndpoint):
    """消息事件的发件箱 + 上报。**永不往上抛**：这条链断了不该影响已经做完的微信动作。"""

    def __init__(
        self,
        base_url: str,
        token: Callable[[], str],
        *,
        store,  # GatewayStore（鸭子类型，避免循环导入）
        timeout_seconds: float = 5.0,
        client: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(base_url, token, timeout_seconds=timeout_seconds, client=client)
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ── 记账（同步，不碰网络）────────────────────────────────

    def record(
        self,
        account_id: str,
        *,
        candidate_id: str,
        direction: str,
        message_id: str,
        occurred_at: datetime | None = None,
        message_type: str = "text",
        command_id: str = "",
        content: str = "",
        source: str = "",
    ) -> str:
        """把一条事件排进发件箱，返回 ``eventId``；排不了返回空串（只记账，不抛）。

        **同步且只写本机 SQLite**：调用点全在微信动作的关键路径上（刚发完一条、刚读到一条），
        在那儿等一个 HTTP 往返，等于让簿记去挤真正要发的消息。
        """
        try:
            event = build_event(
                candidate_id=candidate_id,
                direction=direction,
                message_id=message_id,
                occurred_at=occurred_at or self._clock(),
                message_type=message_type,
                command_id=command_id,
                content=content,
                source=source,
            )
        except MessageEventError as exc:
            _log.warning("消息事件不成立，未登记", direction=direction, error=str(exc))
            return ""
        try:
            fresh = self._store.put_message_event(
                account_id, event["eventId"], event, at=self._clock()
            )
        except Exception as exc:  # noqa: BLE001 —— 落盘失败也不该掀翻微信侧
            _log.warning("消息事件入发件箱失败", direction=direction, error=type(exc).__name__)
            return ""
        if fresh:
            _log.info("已登记微信消息事件", direction=direction, 来源=source or None)
        return event["eventId"]

    # ── 上报（后台循环调用）──────────────────────────────────

    async def flush(self, limit: int = FLUSH_BATCH) -> int:
        """把发件箱里的事件发出去，返回成功发出的条数。**永不上抛**。

        碰到「等一等还能成」的失败就**停在那一条**：后端不通时把剩下几十条一条条撞过去
        既没用，又会把实时日志刷满。下一趟再从同一条继续，顺序也因此不会乱。
        """
        self._purge_stale()
        sent = 0
        for row in self._store.list_message_events(limit=limit):
            outcome = await self._post(row["payload"])
            if outcome == _OK:
                self._store.drop_message_event(row["event_id"])
                sent += 1
                continue
            if outcome == _DROP:
                self._store.drop_message_event(row["event_id"])
                continue
            attempts = self._store.bump_message_event_attempts(row["event_id"])
            if attempts >= MAX_ATTEMPTS:
                self._store.drop_message_event(row["event_id"])
                _log.warning(
                    "消息事件反复报不出去，已放弃",
                    direction=row["payload"].get("direction"),
                    attempts=attempts,
                )
                continue
            break
        return sent

    def pending(self) -> int:
        """发件箱里还压着几条。只读，给日志用。"""
        try:
            return self._store.count_message_events()
        except Exception:  # noqa: BLE001
            return 0

    def _purge_stale(self) -> None:
        cutoff = self._clock() - timedelta(hours=MAX_AGE_HOURS)
        try:
            stale = self._store.purge_message_events_before(cutoff)
        except Exception as exc:  # noqa: BLE001
            _log.warning("清理过期消息事件失败", error=type(exc).__name__)
            return
        if stale:
            # 丢了必须留痕：静悄悄丢事件和静悄悄丢消息是同一种病。
            _log.warning("有消息事件超过保鲜期没能报出去，已丢弃", count=len(stale))

    async def _post(self, payload: dict[str, Any]) -> str:
        response = await self._send(
            "POST", f"{self._base_url}{MESSAGE_EVENTS_PATH}", json=payload
        )
        if response is None:
            # 没配地址 / 没登录 / 网络炸 —— 都是「这台机器还没准备好」，留着下趟再报。
            return _RETRY
        status = response.status_code
        if status < 400:
            self._log_accepted(payload, response)
            return _OK
        if status in _RETRY_STATUSES or status >= 500:
            _log.warning("消息事件上报暂时失败，留着重试", status=status)
            return _RETRY
        _log.warning(
            "消息事件被后端拒收，不再重试",
            status=status,
            direction=payload.get("direction"),
        )
        return _DROP

    def _log_accepted(self, payload: dict[str, Any], response: Any) -> None:
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 —— 收下了就算成，回体解析不了不影响结论
            body = {}
        body = body if isinstance(body, dict) else {}
        _log.info(
            "微信消息事件已上报",
            direction=payload.get("direction"),
            后端动作=body.get("action") or None,
            # linked=false 表示这个候选人名下没有在跑的招聘计划任务，不是错，但值得看见。
            匹配到计划=body.get("linked"),
            重复=body.get("duplicate") or None,
        )
