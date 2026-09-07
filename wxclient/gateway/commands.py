"""AI 端推来的微信能力指令：白名单 → 守门 → 队列 → 回执（066）。

**卡片是提案，网关是执行器**。AI 端只会推来「想做什么」，做不做、能不能做，由这里判：
op 不在白名单直接拒、写类动作过频次/时间窗/黑名单、真实动作仍要 `--allow-real-actions`
+ 确认短语（`wxclient/CLAUDE.md` §5.1 §5.2）。**地基不信任上游**——AI 端推来的 op 和参数
一律当外部输入校验。

节拍上也不能松：主动动作比被动回复更招风控（memory `wechat-rate-control`，2026-08-12
自动退登事故）。所有指令共用 `queue` 的单消费者 worker，天然串行；批量只能是「多条指令」，
不存在「一条指令批量加 50 个好友」这种形状。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from wxclient.queue import TaskType

from .. import logrole
from .candidates import CandidateDirectory, CandidateUpdateError
from .capabilities import capability_of
from .download import DownloadError, FileRef
from .guard import (
    ACTION_FRIEND,
    ACTION_GROUP,
    ACTION_MESSAGE,
    ACTION_MOMENTS,
    ACTION_MOMENTS_COMMENT,
    ACTION_MOMENTS_LIKE,
    READ_ACTION,
    READ_FRIENDS_ACTION,
    OutboundGuard,
    Reservation,
)
from .message_events import DIRECTION_OUTBOUND, MessageEventReporter, local_message_id

_log = structlog.get_logger(__name__)

#: 跟 AI 端打交道的那几行（那条 WS：派轮 / 卡片 / 指令 / 回执 / 名单 / 能力）。
_ai = _log.bind(**{logrole.ROLE_KEY: logrole.AI})
#: 跟业务后端打交道的那几行（Client Token 走的 HTTP）。
_backend = _log.bind(**{logrole.ROLE_KEY: logrole.BACKEND})
#: 这台电脑自己的事，不涉及任何对端。
_local = _log.bind(**{logrole.ROLE_KEY: logrole.LOCAL})

#: AI 端推指令用的事件名（server → 网关）。
COMMAND_EVENT = "wechat.command"
#: AI 端 → 网关：这一轮发生了什么。**只打日志、不执行任何动作**。
#: 在此之前，只有「有指令要执行」时这台电脑上才看得见动静；后端按计划触发的一轮
#: 如果什么都没做，本机日志里一个字都没有 —— 分不出是没收到、还是收到了觉得不用做。
NOTICE_EVENT = "wechat.notice"

#: 网关回执用的 op（网关 → server，走客户端帧格式）。
RESULT_OP = "wechat.result"


#: 阶段性回执（073，需求文档 §12.4）。前两个是**中间态**——AI 端收到后只刷新卡，
#: 不销账；只有终态才结束这条指令的生命周期。
#:
#: 为什么要有中间态：原来只有 done/failed/rejected，卡上写完「已下发」就再无下文，
#: 十几秒里分不清「在跑」还是「丢了」——排查时先怀疑的永远是丢了。
STAGE_RECEIVED = "received"   # 网关收到了，排队中
STAGE_STARTED = "started"     # 过了守门，开始真动微信
TERMINAL_STATUSES = frozenset({"done", "failed", "rejected"})


class CommandError(ValueError):
    """指令本身不合法（缺参数、类型不对）。回 rejected，不重试。"""


@dataclass(frozen=True)
class CommandSpec:
    """一个 op 怎么翻成底座任务。"""

    task_type: TaskType
    writes: bool
    """是否会改变外部世界（发消息/加好友/发朋友圈）。决定守门强度与真实动作确认。"""
    build: Callable[[dict[str, Any]], dict[str, Any]]
    """args → 队列 payload。缺必填参数就抛 CommandError，**绝不用默认值糊弄**。"""
    target: Callable[[dict[str, Any]], str] = lambda _args: ""
    """这条指令打扰的是谁（黑名单与发送计数按它记）。没有具体对象就返回空串。"""

    action: str = ACTION_MESSAGE
    """走哪一档频次配额（072，需求文档 §13.3）。读类不计数，这个字段用不上。

    私聊 / 群消息是同一个 op 的两种形态（发到群里误伤面更大），所以群那一档
    不写在这里，由 `_action_for` 按对方是不是群现算。"""

    read_action: str = READ_ACTION
    """读类走哪一档**节拍**（最小间隔）。查好友申请比读消息敏感得多，单列一档。"""

    reserve_count: Callable[[dict[str, Any]], int] | None = None
    """这条指令**最多**做几次这档动作（`None` = 就一次）。

    给了它就意味着「最多 N 个」——额度不够时**减量放行**（做 3 个总比一个不做强）。
    没给的一律全有或全无：一段回复拆成三条，发出两条比一条不发更糟。"""

    counts: Callable[[dict[str, Any], dict[str, Any]], int] = lambda _args, _result: 1
    """这条指令**实际**做了几次这档动作。`(args, result) -> n`。

    默认 1。`accept_new_friends` 一条能通过好几个人——按 1 记的话，加好友那档红线
    实际被放宽到 5 倍（2026-08-18 审计实测）。"""

    files_key: str | None = None
    """这条指令要带文件时，本机路径落进 payload 的哪个键（`None` = 不带文件）。

    067 的字节通道对 op 是通用的（AI 端随指令送字节、本机落进一次性临时目录），
    原来却在两处写死了 `op == "send_files"`——于是发朋友圈只能走 `medias`，
    而那是一串**上游给的本机绝对路径**，公开发到朋友圈上（2026-08-18 审计 P0-5）。
    把「带不带文件、落进哪个键」写回这张 op 声明表，两处写死随之消失。"""

    files_required: bool = False
    """没带文件就是这条指令不成立（发文件）。为假 = 可有可无（朋友圈配图）。"""


def _greetings(args: dict[str, Any]) -> str:
    """加好友的验证语。**「没给」和「明确给了空」是两回事**（092）。

    - 没给（键不在、给的是 null）→ 兜底一句「你好」，保持原行为不变；
    - 明确给了空串 → **就是要空**，让微信用它自己预填的「我是XXX」
      （`uia.py` 里是 `if greetings:` 才动那个输入框）。用户 2026-08-27 裁决。

    ★ 红线不变：内部 `reason` 绝不兜底进验证语——那是「内部指令变成一条候选人
      真能读到的微信」那个事故形状。这里连碰都不碰 `reason`。
    """
    value = args.get("greetings")
    return value if isinstance(value, str) else "你好"


def _req(args: dict[str, Any], key: str, *, alias: str = "") -> str:
    value = args.get(key)
    if not value and alias:
        value = args.get(alias)
    text = str(value or "").strip()
    if not text:
        raise CommandError(f"缺少必填参数 {key}")
    return text


def _limit(args: dict[str, Any], default: int, cap: int) -> int:
    try:
        value = int(args.get("limit", default))
    except (TypeError, ValueError) as exc:
        raise CommandError("limit 必须是整数") from exc
    return max(1, min(value, cap))


#: 一条评论指令最多评几条。跟默认日上限同数；要多评就多下几条指令，网关按剩余额度减量放行。
MOMENTS_COMMENT_MAX = 5
#: 评论最长几个字。朋友圈评论本来就是一两句话；再长就是在别人动态下面发文章。
MOMENTS_COMMENT_TEXT_MAX = 200


def _squash(text: str) -> str:
    return "".join(str(text or "").split())


def _comment_requests(args: dict[str, Any]) -> list[dict[str, str]]:
    """`comments`：``[{content: 动态正文, text: 评论}]``。**形状不对当场拒**，不猜、不补默认值。

    正文是 `read_recent_moments` 读回来的那段，网关按它在时间线上认动态。太短（去空白不足
    2 字）认不准，拒；评论空的没意义，拒；同一段正文出现两次只留第一条；超过
    `MOMENTS_COMMENT_MAX` 条拒——AI 端那边本来就按这个数出，多了说明有一边坏了。
    """
    raw = args.get("comments")
    if not isinstance(raw, list) or not raw:
        raise CommandError("缺少必填参数 comments（要评哪几条、各说什么）")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise CommandError("comments 里每一项都要是 {content, text}")
        content = str(entry.get("content") or "").strip()
        text = str(entry.get("text") or "").strip()
        if len(_squash(content)) < 2:
            raise CommandError("comments[].content 要是那条动态的正文（至少 2 个字），网关按它认动态")
        if not text:
            raise CommandError("comments[].text 不能为空")
        if len(text) > MOMENTS_COMMENT_TEXT_MAX:
            raise CommandError(f"评论太长了：最多 {MOMENTS_COMMENT_TEXT_MAX} 字")
        key = _squash(content)
        if key in seen:
            continue
        seen.add(key)
        out.append({"content": content, "text": text})
    if len(out) > MOMENTS_COMMENT_MAX:
        raise CommandError(f"一条指令最多评 {MOMENTS_COMMENT_MAX} 条动态")
    return out


def _file_refs(args: dict[str, Any], *, required: bool = True) -> list[FileRef]:
    """067：`files` 里是**文件描述符**（`{file_id, file_name, content_b64}`），不是路径。

    路径是 AI 端给不了的东西（服务端派生、客户端不可传），所以这里也不接受字符串——
    真收到一串路径，那要么是老版本、要么是有人在试探，两种情况都该当场拒掉而不是照着发。
    本机路径由 `FileFetcher` 下载后产生，见 `_run`。

    ``required=False``：这条指令**可以**不带文件（朋友圈允许纯文字）。
    """
    raw = args.get("files")
    refs = [ref for ref in (FileRef.parse(item) for item in raw) if ref] if isinstance(raw, list) else []
    if not refs and not required:
        return []
    if not refs:
        raise CommandError("这条发文件指令没带上任何文件（AI 端应随指令送来文件字节）")
    if len(refs) > 9:
        raise CommandError("一条指令最多带 9 个文件")
    return refs


#: op 白名单。**只有这张表里的 op 会被执行**，其余一律 rejected。
#:
#: 刻意不收 watch_chats / scan_messages：它们是有界时长的常驻扫描，属于入站循环，
#: 被一条指令占住同一个单消费者 worker 会把正常收消息饿死。
#: 发消息两个 op 另收一个可选参数 `to_group`（bool）：AI 端知道这次发的是群时给 true。
#: 本机只有收到过群消息才认得出群，主动发第一条时一无所知，会误算进私聊那档配额
#: （见 `CommandExecutor._to_group`）。**它只能收紧**：给 false 不会把已知的群降回私聊。
#: 非微信动作：拿 Client Token 把候选人状态回写到业务后端（api2 §9）。
#: 不在 `COMMANDS` 里——那张表每一条都要走微信队列，而这条一个微信窗口都不碰，
#: 也不该占任何微信配额（配额管的是「会不会打扰真人」）。
UPDATE_CANDIDATE_OP = "update_candidate"

#: 这两个 op 是**真发给候选人一条微信**，做成了要上报给业务后端（未回复跟进的计时起点）。
#: 值是契约的 `messageType`。其余 op 一律不报：加好友不是消息，朋友圈没有收件人，
#: 读类什么都没发生。
MESSAGE_EVENT_OPS: dict[str, str] = {"send_message": "text", "send_files": "file"}

COMMANDS: dict[str, CommandSpec] = {
    # ── 写类 ──
    "send_message": CommandSpec(
        TaskType.SEND_MESSAGE, True,
        lambda a: {"friend": _req(a, "to", alias="friend"), "text": _req(a, "text")},
        lambda a: _req(a, "to", alias="friend"),
    ),
    # 文件不在这里填：那是下载完才有的本机路径（067，见 CommandExecutor._run 与 files_key）。
    "send_files": CommandSpec(
        TaskType.SEND_FILES, True,
        lambda a: {"friend": _req(a, "to", alias="friend"), "text": str(a.get("text") or "")},
        lambda a: _req(a, "to", alias="friend"),
        files_key="files", files_required=True,
    ),
    "add_friend": CommandSpec(
        TaskType.ADD_FRIEND, True,
        lambda a: {"number": _req(a, "number"), "greetings": _greetings(a),
                   "remark": a.get("remark")},
        lambda a: _req(a, "number"),
        action=ACTION_FRIEND,
    ),
    "accept_new_friends": CommandSpec(
        TaskType.ACCEPT_NEW_FRIENDS, True,
        lambda a: {"limit": _limit(a, 1, 5)},
        action=ACTION_FRIEND,
        # 一条指令 = 最多 limit 次「加好友」：按 limit 预留，按真实通过数结算。
        reserve_count=lambda a: _limit(a, 1, 5),
        counts=lambda _args, result: len(result.get("items") or []),
    ),
    # ★ 配图同样只收描述符，`medias` 这个键**不再被 build 看见**（2026-08-18 审计 P0-5）：
    #   原来它是上游给的一串本机绝对路径，`_normalize_moments_media_paths` 只要文件存在
    #   就发上朋友圈——而朋友圈是**公开**的，这台电脑上任何一个可读文件都在射程内。
    "post_moments": CommandSpec(
        TaskType.POST_MOMENTS, True,
        lambda a: {"text": _req(a, "text")},
        action=ACTION_MOMENTS,
        files_key="medias",
    ),
    # 刷自己的朋友圈时间线，给今天的动态点赞。**不逐个好友翻**（那条路和搜号同形，
    # 是风控最盯的动作）。一条指令 = 最多 limit 个赞：按 limit 预留、按真点了几个结算，
    # 额度不够就减量放行——点 5 个总比一个不点强。没有具体对象，target 是空串。
    "like_moments": CommandSpec(
        TaskType.LIKE_MOMENTS, True,
        lambda a: {"limit": _limit(a, 20, 20)},
        action=ACTION_MOMENTS_LIKE,
        reserve_count=lambda a: _limit(a, 20, 20),
        counts=lambda _args, result: int(result.get("liked") or 0),
    ),
    # 给时间线里今天的、指定的那几条动态各写一条评论（用户 2026-09-07：由业务端计划触发，
    # 评论文字由 AI 端按计划素材现写）。按要评几条预留、按真评了几条结算、减量放行。
    # 评论是公开文字，自己一档配额（默认一天 5，可配）和自己一本节拍账。
    "comment_moments": CommandSpec(
        TaskType.COMMENT_MOMENTS, True,
        lambda a: {"comments": _comment_requests(a)},
        action=ACTION_MOMENTS_COMMENT,
        reserve_count=lambda a: len(_comment_requests(a)),
        counts=lambda _args, result: int(result.get("commented") or 0),
    ),
    # ── 读类 ──
    "search_contacts": CommandSpec(
        TaskType.SEARCH_CONTACTS, False,
        lambda a: {"query": _req(a, "query"), "limit": _limit(a, 10, 50)},
    ),
    "get_contact_profile": CommandSpec(
        TaskType.GET_CONTACT_PROFILE, False,
        lambda a: {"friend": _req(a, "friend", alias="to")},
    ),
    "list_group_members": CommandSpec(
        TaskType.LIST_GROUP_MEMBERS, False,
        lambda a: {"group": _req(a, "group")},
    ),
    "read_chat_history": CommandSpec(
        TaskType.READ_CHAT_HISTORY, False,
        lambda a: {"friend": _req(a, "friend", alias="to"), "limit": _limit(a, 20, 100)},
    ),
    "get_unread_messages": CommandSpec(TaskType.GET_UNREAD_MESSAGES, False, lambda _a: {}),
    "check_new_friends": CommandSpec(
        TaskType.CHECK_NEW_FRIENDS, False,
        lambda a: {"limit": _limit(a, 8, 20)},
        read_action=READ_FRIENDS_ACTION,
    ),
    "read_recent_moments": CommandSpec(
        TaskType.READ_RECENT_MOMENTS, False,
        lambda a: {"limit": _limit(a, 5, 20)},
    ),
}


@dataclass(frozen=True)
class CommandResult:
    """一条指令的结局。原样进回执帧。"""

    command_id: str
    status: str          # done | rejected | failed
    reason: str = ""
    result: dict[str, Any] | None = None

    def to_frame_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {"command_id": self.command_id, "status": self.status}
        if self.reason:
            data["reason"] = self.reason
        if self.result is not None:
            data["result"] = self.result
        return data


class CommandExecutor:
    """执行一条 AI 端推来的指令。**同一 command_id 只执行一次**。"""

    def __init__(
        self,
        *,
        runtime,           # WeChatRuntime 或任何提供 submit(TaskType, payload, ...) 的对象
        store,             # GatewayStore
        guard: OutboundGuard,
        account_id: str,
        allow_real_actions: bool = False,
        confirmation: str = "",
        fetcher=None,      # FileFetcher | None：发文件时去 AI 端取产出物（067）。没配就发不了文件。
        clock: Callable[[], datetime] | None = None,
        capabilities: Callable[[], dict] | None = None,
        candidates: CandidateDirectory | None = None,
        message_events: MessageEventReporter | None = None,
    ) -> None:
        """``capabilities`` 是个**取当下能力表**的函数（074），不是一份快照——
        微信随时可能被关掉、掉线，执行前这一刻算的才作数。"""
        self._runtime = runtime
        self._store = store
        self._guard = guard
        self._account = account_id
        self._allow_real = allow_real_actions
        self._confirmation = confirmation
        self._fetcher = fetcher
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._capabilities = capabilities
        self._candidates = candidates
        self._message_events = message_events

    async def execute(
        self,
        data: dict[str, Any],
        on_stage: Callable[[str], Awaitable[None]] | None = None,
    ) -> CommandResult:
        """``on_stage`` 收中间态（073）。**重推的老指令不报中间态**——它没有在跑。"""
        command_id = str((data or {}).get("command_id") or "").strip()
        op = str((data or {}).get("op") or "").strip()
        if not command_id:
            return CommandResult("", "rejected", "指令缺少 command_id")

        # 幂等：重推同一条只回上次的结果，绝不重发一遍消息。
        cached = self._store.get_command_result(self._account, command_id)
        if cached is not None:
            _ai.info("指令已执行过，直接回上次结果", op=op, status=cached.get("status"))
            return CommandResult(command_id, str(cached.get("status") or "done"),
                                 str(cached.get("reason") or ""), cached.get("result"))

        result = await self._run(command_id, op, data, on_stage)
        self._store.put_command_result(
            self._account, command_id,
            {"status": result.status, "reason": result.reason, "result": result.result},
        )
        return result

    async def _run(
        self,
        command_id: str,
        op: str,
        data: dict[str, Any],
        on_stage: Callable[[str], Awaitable[None]] | None = None,
    ) -> CommandResult:
        if op == UPDATE_CANDIDATE_OP:
            return await self._update_candidate(command_id, data)
        spec = COMMANDS.get(op)
        if spec is None:
            _ai.warning("收到不支持的微信指令", op=op)
            return CommandResult(command_id, "rejected", f"不支持的能力：{op or '(空)'}")

        args = data.get("args")
        args = args if isinstance(args, dict) else {}
        try:
            payload = spec.build(args)
            peer = spec.target(args)
            # 带文件的先把描述符解出来：形状不对要在动微信**之前**拒，别下载完才发现发不了。
            refs = _file_refs(args, required=spec.files_required) if spec.files_key else []
        except CommandError as exc:
            return CommandResult(command_id, "rejected", str(exc))
        if op == "add_friend":
            # ★ 换机/重装之后不给同一个人起第二个备注名（api2 §4.2）。后端记着这个号在用哪个
            # 会话名，本机启动时已经拉回来了；AI 端产的 remark 只在**本机也不知道**时才作数。
            # 名字一变，按会话名存的关系记忆（078）就断在这里，而且现场看不出来——
            # 微信里会多出一个"新人"，聊过的东西全不认了。
            known = self._store.contact_session_name(self._account, str(payload.get("number") or ""))
            if known and known != payload.get("remark"):
                _log.info("按后端已知的会话名设置备注", op=op)   # 名字本身不进日志
                payload["remark"] = known
        if refs and self._fetcher is None:
            _ai.warning("收到发文件指令但没配下载通道", op=op)
            return CommandResult(command_id, "rejected", "本机没有配取文件通道，发不了 AI 端产出的文件")

        # ★ 074：先问「能不能做」，再问「让不让做」。两者原因不同，给用户的话完全两样：
        #   做不了 = 去修环境（微信没登、只走流程不真发、没配取文件通道）；
        #   不让做 = 规则挡的（频次配额、工作时间窗、黑名单）。
        #   原来所有「做不了」都要执行到一半才发现，用户看到的是一条不知所云的失败。
        # 「是不是发到群里」算一次，能力检查与频次分档共用同一个判断——
        # 两处各算各的，迟早会出现「按私聊放行、按群记账」这种自相矛盾。
        to_group = self._to_group(peer, args)
        blocked = self._capability_blocker(op, to_group)
        if blocked:
            _local.info("这台电脑现在做不了这个动作", op=op, reason=blocked)
            return CommandResult(command_id, "rejected", blocked)

        now = self._clock()
        action = self._action_for(spec, to_group)
        if op == "comment_moments":
            # ★ 同一条动态只评一次。计划重投 / AI 端读回同一批动态再写一遍，微信上分不出
            #   「我们评过没有」，只能靠本机这本台账挡。全都评过了 = 这条指令**做完了**
            #   （不是拒：一个字都不用再发），回执如实写 already_commented。
            fresh, already = self._drop_commented(payload["comments"], now)
            if not fresh:
                _local.info("这几条动态都评论过了，这次不再评", op=op, already=already)
                return CommandResult(command_id, "done", result={
                    "commented": 0, "scanned": 0, "matched": 0, "missed": [],
                    "already_commented": already, "stopped": "already",
                })
            payload["comments"] = fresh
            payload["already_commented"] = already
        # ★ 写类：**先占住额度再动微信**（判一次记一次的老写法在并发下等于没有守门）。
        #    读类：不占额度，但要过节拍闸（最小间隔）。两条路都在动微信之前。
        reservation = Reservation()
        if spec.writes:
            reservation = self._guard.reserve(
                self._account, peer, now=now, action=action,
                count=spec.reserve_count(args) if spec.reserve_count else 1,
                clamp=spec.reserve_count is not None,
            )
            if not reservation.allowed:
                _local.info("指令被守门拦下", op=op, reason=reservation.reason)
                return CommandResult(command_id, "rejected", reservation.reason)
            if spec.reserve_count is not None:
                # 减量放行了就把上限跟着调下来，别让微信那边照原数去做。
                payload["limit"] = reservation.granted
                if op == "comment_moments":
                    # 评论没有 limit 这回事——要评的是一张清单，减量就是砍清单尾巴。
                    payload["comments"] = payload["comments"][:reservation.granted]
            # ★ 额度之外还有**节奏**：加好友自己有一本 300s 的账（2026-08-27 安全验证）。
            #   排在 reserve 之后，是因为被节拍挡下的这一趟微信一下都没动，不该白占一次配额。
            paced = self._guard.pace_write(self._account, op, now=now)
            if not paced.allowed:
                reservation.release()
                _local.info("写类指令被节拍闸拦下", op=op, reason=paced.reason)
                return CommandResult(command_id, "rejected", paced.reason)
        else:
            decision = self._guard.allow_read(self._account, spec.read_action, now=now)
            if not decision.allowed:
                _local.info("读类指令被节拍闸拦下", op=op, reason=decision.reason)
                return CommandResult(command_id, "rejected", decision.reason)

        # 到这里参数齐了、守门也过了，接下来就要真动微信（发文件还要先取字节）。
        # 报一声「开始了」——这段可能跑十几秒，中间没有任何信号的话没法跟「丢了」区分。
        if on_stage is not None:
            await on_stage(STAGE_STARTED)

        # 读类也必须显式关掉 dry_run：底座的 dry_run 默认是 True，`check_new_friends`
        # / `read_recent_moments` 在 dry-run 下直接返回空壳（`wechat_api/uia.py`），
        # 不碰微信也就永远查不到东西——AI 端会收到一条「已完成、什么都没有」的假回执。
        # 「要不要真动微信」这个开关仍然只有一个：`--allow-real-actions`。
        payload["dry_run"] = not self._allow_real
        # 取回来的文件只在这段 with 里活着：发完（或失败）立刻连临时目录一起删，
        # 产出物不在这台电脑上留底（067）。
        async with AsyncExitStack() as stack:
            if refs:
                try:
                    paths = await stack.enter_async_context(
                        self._fetcher.materialize(self._account, refs)
                    )
                except DownloadError as exc:
                    reservation.release()   # 字节都没取到，微信一下没动
                    _ai.warning("取 AI 端产出文件失败", op=op, error=str(exc))
                    return CommandResult(command_id, "failed", str(exc))
                except Exception as exc:  # noqa: BLE001 —— 网络类异常也要回给上游
                    reservation.release()
                    _ai.warning("取 AI 端产出文件失败", op=op, error=str(exc))
                    return CommandResult(command_id, "failed", f"取文件失败：{exc}")
                payload[spec.files_key] = [str(p) for p in paths]
            try:
                raw = await self._runtime.submit(
                    spec.task_type, payload,
                    confirmation=self._confirmation if self._allow_real else "",
                )
            except Exception as exc:  # noqa: BLE001 —— 失败要能回给上游，不能只写日志
                reservation.release()   # 一件没做成，额度原样还回去
                _log.warning("指令执行失败", op=op, error=str(exc))
                return CommandResult(command_id, "failed", f"{type(exc).__name__}: {exc}")

            result = (raw or {}).get("result") or {}

            # ★ 「submit 没抛异常」不等于「这件事做成了」（2026-08-29 真机事故）：
            #   队列的终态里还有 failed / blocked / cancelled，超时更是压根没有结论，
            #   这些都是**正常返回**一个带 status 的 dict。原来这里不看 status 直接
            #   `settle` + 报 done —— 发朋友圈死在「profile pane did not appear」，
            #   AI 端却收到 done，以为已经发出去了。同 `_note_message_sent` 的口径。
            status = str((raw or {}).get("status") or "")
            if status != "succeeded":
                why = str((raw or {}).get("last_error") or "").strip()
                if (raw or {}).get("timed_out"):
                    # 没结论：微信那边**可能已经动过**了，额度绝不能还回去（还回去 =
                    # 下一条指令拿它再做一次同样的动作）。只把「不知道」如实报上去。
                    reservation.settle(reservation.granted)
                    message = why or "等超时了，没等到微信任务的结论"
                else:
                    reservation.release()   # 有明确终态且不是成功 —— 一件没做成，全额还回去
                    message = why or f"微信任务{status or 'unknown'}"
                _log.warning("微信任务没有成功", op=op, status=status or "unknown", error=why)
                return CommandResult(command_id, "failed", message)

            if spec.writes:
                # 额度在动微信**之前**就占住了（见上），这里只按真实用量结算：
                # 预留 5 实际通过 2 → 还回 3。做多少记多少，两个方向都不许含糊。
                reservation.settle(spec.counts(args, result))
            if op == "comment_moments":
                # 真评上的那几条记进台账；被这趟砍掉/没认出来的不记，下次还能评。
                done_keys = [_squash(c) for c in (result.get("commented_contents") or []) if isinstance(c, str)]
                self._store.record_moment_comments(self._account, done_keys, now)
                if payload.get("already_commented"):
                    result = {**result, "already_commented": payload["already_commented"]}
            _log.info("指令执行完成", op=op, dry_run=(spec.writes and not self._allow_real))
            self._note_message_sent(command_id, op, peer, args, raw)
            state = self._friend_state(op, result, raw)
            if state:
                result = {**result, "friend_state": state}
            return CommandResult(command_id, "done", result=result)

    def _drop_commented(
        self, comments: list[dict[str, str]], now: datetime,
    ) -> tuple[list[dict[str, str]], int]:
        """把台账里 30 天内评过的那几条从清单里去掉。返回 (还要评的, 去掉了几条)。"""
        keys = [_squash(c["content"]) for c in comments]
        done = self._store.commented_moment_keys(self._account, keys, now - timedelta(days=30))
        fresh = [c for c, k in zip(comments, keys, strict=True) if k not in done]
        return fresh, len(comments) - len(fresh)

    async def _update_candidate(self, command_id: str, data: dict[str, Any]) -> CommandResult:
        """把候选人的资料/AI 状态写回业务后端（api2 §9）。

        不走微信队列、不占配额、不看工作时间窗——它一个微信窗口都不碰。
        但**只走流程不真发**的时候一律拒：那一轮微信里什么也没发生，把人标成「AI 托管中」
        就是拿假动作去改真数据，HR 看到的进度会比现实超前一格。
        """
        if not self._allow_real:
            return CommandResult(command_id, "rejected", "本机只走流程不真动微信，不改业务数据")
        if self._candidates is None or not self._candidates.configured:
            return CommandResult(command_id, "rejected", "本机没配业务接口地址，写不了候选人状态")

        args = data.get("args")
        args = args if isinstance(args, dict) else {}
        profile = args.get("profile")
        ai_status = args.get("ai_status")
        try:
            result = await self._candidates.update(
                str(args.get("candidate_id") or ""),
                profile=profile if isinstance(profile, dict) else None,
                ai_status=ai_status if isinstance(ai_status, dict) else None,
            )
        except CandidateUpdateError as exc:
            _backend.warning("候选人回写请求不成立", error=str(exc))
            return CommandResult(command_id, "rejected", str(exc))
        if not result.get("updated"):
            return CommandResult(command_id, "failed", str(result.get("reason") or "候选人状态没写成"))
        return CommandResult(command_id, "done", result=result)

    @property
    def allow_real(self) -> bool:
        """这台机器有没有开真实动作。外部（runner）判「刚才那一下是不是真动了微信」要用它，
        别去摸 `_allow_real`——私有名一改，外面就静默失效了。"""
        return self._allow_real

    def _note_message_sent(
        self,
        command_id: str,
        op: str,
        peer: str,
        args: dict[str, Any],
        raw: dict[str, Any] | None,
    ) -> None:
        """真发出去一条微信 → 登记一条 outbound 事件（计划驱动那条路的计时起点）。

        `commandId` 带上原指令 ID：这条消息来自 AI/后端的一条指令，两端对账时按它对上
        （契约请求字段表）。**入站事件不带它**，那是候选人发来的，跟指令无关。

        `dry_run` 的一轮微信侧什么都没发生，一律不报（同 `_friend_state` / `update_candidate`
        的口径）。归不了位的候选人也不报——不编 ID 顶上。

        **只在队列确认 `succeeded` 时才报**：这里刻意只管上报，不改回执语义（那是另一件事，
        改动面大得多），所以「任务没成功」这一判在本方法内自己做一次。
        """
        if self._message_events is None or not self._allow_real:
            return
        message_type = MESSAGE_EVENT_OPS.get(op)
        if message_type is None or not peer:
            return
        # ★ 「没抛异常」不等于「话发出去了」：队列的终态里还有 failed / blocked / cancelled，
        #   等超时更是压根没有结论。只认 succeeded——报一条假的「已发送」，后端就会从一条
        #   根本没到对方手机上的消息开始算「多久没回」，真正的跟进被推后一整个跟进周期。
        if str((raw or {}).get("status") or "") != "succeeded":
            _backend.info("微信任务没有确认成功，不上报消息事件", op=op,
                      status=(raw or {}).get("status") or "unknown")
            return
        candidate_id = self._store.contact_worker_id(self._account, peer)
        if not candidate_id:
            _backend.info("发出的消息没能归位到候选人，不上报消息事件", op=op)
            return
        self._message_events.record(
            self._account,
            candidate_id=candidate_id,
            direction=DIRECTION_OUTBOUND,
            # 一条指令 = 一条消息，拿 command_id 派生本机稳定 ID：重推同一条指令时
            # 幂等层先挡住（`execute` 的 cached），万一漏过去，后端也按 messageId 去重。
            message_id=local_message_id(self._account, command_id),
            message_type=message_type,
            command_id=command_id,
            content=str(args.get("text") or ""),
            source="command",
        )

    def _friend_state(
        self, op: str, result: dict[str, Any], raw: dict[str, Any] | None = None
    ) -> str:
        """加好友这条链走到哪一步了（docs/18 §3.2）。AI 端据此写候选人的 `wechatAdded`。

        `add_friend` 一般只能报 `requested`：**申请发出去 ≠ 对方通过**，中间隔着一个真人，
        要多久没人知道。两个 op 的回执原来长得一模一样，「申请发出」会被当成「已经是好友」，
        后面所有发消息的动作都建在一个假前提上。

        `accepted` 有两处，都要求**好友关系此刻确实存在**：
        ① 我们自己通过了对方的申请（`accept_new_friends`），而且**确实通过了至少一个**
           （一个申请都没有时 `items` 是空的，那这一趟什么也没发生，别报状态）；
        ② 加的这个号**早就是好友**（`already_friend`，驱动层按名片上有「发消息」正向判定）。
           这不是"申请发出去了"，是既成事实，所以直接是 `accepted`——不这么报的话，这个人
           在计划里会永远停在"等对方通过"，而根本没有人要通过（2026-08-26 用户提的场景）。
        对方通过了我们的申请是**后来**才知道的事，只能靠入站消息，不在这条指令的射程内。
        """
        if not self._allow_real:
            return ""      # 只走流程没真动微信，报了 AI 端就会去写 wechatAdded
        # ★ **「没抛异常」不等于「申请真发出去了」**（`_note_message_sent` 上那条同款教训，
        #   2026-08-27 真机发现这条路上漏了）：队列的终态里还有 failed / blocked，
        #   `submit` 一个字都不抛，只在 `raw["status"]` 里写着。不判这一下，
        #   一次「没搜到这个号」照样算出 `requested` —— 后端把人记成「申请已发出、等对方通过」，
        #   AI 端跟着写 `ai_managed`，而微信里**什么都没发生**，这个人从此卡在那儿等一个
        #   永远不会来的通过。判不出状态时（老装配没传 raw）维持原样，别把现有链路判死。
        if raw is not None and str(raw.get("status") or "") != "succeeded":
            _backend.info("加好友任务没有确认成功，不给好友状态", op=op,
                      status=raw.get("status") or "unknown")
            return ""
        if op == "add_friend":
            return "accepted" if result.get("already_friend") else "requested"
        if op == "accept_new_friends" and result.get("items"):
            return "accepted"
        return ""

    def _to_group(self, peer: str, args: dict[str, Any]) -> bool:
        """这条指令是不是发到群里。**提示只准收紧，不准放宽**（072 节拍红线）。

        本机只有**收到过**群消息才认得出群（`wechat_io` 里 `mark_peer_kind`），
        而计划驱动的主动触达（081）恰恰是往一个从没收过消息的群发第一条——
        只信本机的话，整批群发会被算进宽松的私聊档（20/时），群那档 6/时 形同虚设。
        所以也认 AI 端的 `to_group` 提示，并记下来备用。

        只往「是群」这一个方向记：上游说错了也只会让配额更紧、能力检查更严，
        反方向（说「不是群」）一律不听，否则这就成了绕开群配额的开关。
        """
        if not peer:
            return False
        if self._store.is_known_group(self._account, peer):
            return True
        if args.get("to_group") is not True:
            return False
        self._store.mark_peer_kind(self._account, peer, is_group=True)
        return True

    def _capability_blocker(self, op: str, to_group: bool) -> str:
        """这台机器现在做不做得了这个 op；返回人话理由，空串 = 做得了。"""
        if self._capabilities is None:
            return ""      # 没接能力探测（测试/老装配）→ 不拦，维持原行为
        key = capability_of(op, to_group=to_group)
        cap = self._capabilities().get(key)
        if cap is None or cap.available:
            return ""
        return cap.reason or f"这台电脑现在做不了「{cap.label}」"

    def _action_for(self, spec: CommandSpec, to_group: bool) -> str:
        """这条指令算哪一档配额。发到群里的消息单独算——群的误伤面比单聊大得多。"""
        if spec.action == ACTION_MESSAGE and to_group:
            return ACTION_GROUP
        return spec.action



def parse_commands(data: dict[str, Any]) -> Sequence[dict[str, Any]]:
    """一帧里可能带一条或多条指令，统一成列表。"""
    if not isinstance(data, dict):
        return []
    batch = data.get("commands")
    if isinstance(batch, list):
        return [c for c in batch if isinstance(c, dict)]
    return [data] if data.get("op") else []
