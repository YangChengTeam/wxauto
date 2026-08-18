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
from datetime import datetime, timezone
from typing import Any

import structlog

from wxclient.queue import TaskType

from .capabilities import capability_of
from .download import DownloadError, FileRef
from .guard import (
    ACTION_FRIEND,
    ACTION_GROUP,
    ACTION_MESSAGE,
    ACTION_MOMENTS,
    OutboundGuard,
)

_log = structlog.get_logger(__name__)

#: AI 端推指令用的事件名（server → 网关）。
COMMAND_EVENT = "wechat.command"

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


def _file_refs(args: dict[str, Any]) -> list[FileRef]:
    """067：`files` 里是**文件描述符**（`{file_id, url}`），不是路径。

    路径是 AI 端给不了的东西（服务端派生、客户端不可传），所以这里也不接受字符串——
    真收到一串路径，那要么是老版本、要么是有人在试探，两种情况都该当场拒掉而不是照着发。
    本机路径由 `FileFetcher` 下载后产生，见 `_run`。
    """
    raw = args.get("files")
    refs = [ref for ref in (FileRef.parse(item) for item in raw) if ref] if isinstance(raw, list) else []
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
COMMANDS: dict[str, CommandSpec] = {
    # ── 写类 ──
    "send_message": CommandSpec(
        TaskType.SEND_MESSAGE, True,
        lambda a: {"friend": _req(a, "to", alias="friend"), "text": _req(a, "text")},
        lambda a: _req(a, "to", alias="friend"),
    ),
    # files 不在这里填：那是下载完才有的本机路径（067，见 CommandExecutor._run）。
    "send_files": CommandSpec(
        TaskType.SEND_FILES, True,
        lambda a: {"friend": _req(a, "to", alias="friend"), "text": str(a.get("text") or "")},
        lambda a: _req(a, "to", alias="friend"),
    ),
    "add_friend": CommandSpec(
        TaskType.ADD_FRIEND, True,
        lambda a: {"number": _req(a, "number"), "greetings": str(a.get("greetings") or "你好"),
                   "remark": a.get("remark")},
        lambda a: _req(a, "number"),
        action=ACTION_FRIEND,
    ),
    "accept_new_friends": CommandSpec(
        TaskType.ACCEPT_NEW_FRIENDS, True,
        lambda a: {"limit": _limit(a, 1, 5)},
        action=ACTION_FRIEND,
    ),
    "post_moments": CommandSpec(
        TaskType.POST_MOMENTS, True,
        lambda a: {"text": _req(a, "text"), "medias": list(a.get("medias") or [])},
        action=ACTION_MOMENTS,
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
            _log.info("指令已执行过，直接回上次结果", op=op, status=cached.get("status"))
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
        spec = COMMANDS.get(op)
        if spec is None:
            _log.warning("收到不支持的微信指令", op=op)
            return CommandResult(command_id, "rejected", f"不支持的能力：{op or '(空)'}")

        args = data.get("args")
        args = args if isinstance(args, dict) else {}
        try:
            payload = spec.build(args)
            peer = spec.target(args)
            # 发文件先把描述符解出来：形状不对要在动微信**之前**拒，别下载完才发现发不了。
            refs = _file_refs(args) if op == "send_files" else []
        except CommandError as exc:
            return CommandResult(command_id, "rejected", str(exc))
        if refs and self._fetcher is None:
            _log.warning("收到发文件指令但没配下载通道", op=op)
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
            _log.info("这台电脑现在做不了这个动作", op=op, reason=blocked)
            return CommandResult(command_id, "rejected", blocked)

        now = self._clock()
        action = self._action_for(spec, to_group)
        denial = self._gate(spec, peer, now, action)
        if denial:
            _log.info("指令被守门拦下", op=op, reason=denial)
            return CommandResult(command_id, "rejected", denial)

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
                    _log.warning("取 AI 端产出文件失败", op=op, error=str(exc))
                    return CommandResult(command_id, "failed", str(exc))
                except Exception as exc:  # noqa: BLE001 —— 网络类异常也要回给上游
                    _log.warning("取 AI 端产出文件失败", op=op, error=str(exc))
                    return CommandResult(command_id, "failed", f"取文件失败：{exc}")
                payload["files"] = [str(p) for p in paths]
            try:
                raw = await self._runtime.submit(
                    spec.task_type, payload,
                    confirmation=self._confirmation if self._allow_real else "",
                )
            except Exception as exc:  # noqa: BLE001 —— 失败要能回给上游，不能只写日志
                _log.warning("指令执行失败", op=op, error=str(exc))
                return CommandResult(command_id, "failed", f"{type(exc).__name__}: {exc}")

            if spec.writes:
                # 主动发出去的也要计入频次——不然「被动回复限 20 条/时」会被主动触达绕开。
                # ★ 没有具体对象的写动作（发朋友圈）同样要记账（072）：它照样是一次
                #   平台看得见的动作，不记就等于朋友圈那一档配额永远用不满、形同虚设。
                self._store.record_send(self._account, peer, now, action=action)
            _log.info("指令执行完成", op=op, dry_run=(spec.writes and not self._allow_real))
            result = (raw or {}).get("result") or {}
            state = self._friend_state(op, result)
            if state:
                result = {**result, "friend_state": state}
            return CommandResult(command_id, "done", result=result)

    def _friend_state(self, op: str, result: dict[str, Any]) -> str:
        """加好友这条链走到哪一步了（docs/18 §3.2）。AI 端据此写候选人的 `wechatAdded`。

        `add_friend` 只能报 `requested`：**申请发出去 ≠ 对方通过**，中间隔着一个真人，
        要多久没人知道。两个 op 的回执原来长得一模一样，「申请发出」会被当成「已经是好友」，
        后面所有发消息的动作都建在一个假前提上。

        真正的 `accepted` 只有一处：我们自己通过了对方的申请，而且**确实通过了至少一个**
        （一个申请都没有时 `items` 是空的，那这一趟什么也没发生，别报状态）。
        对方通过了我们的申请是**后来**才知道的事，只能靠入站消息，不在这条指令的射程内。
        """
        if not self._allow_real:
            return ""      # 只走流程没真动微信，报了 AI 端就会去写 wechatAdded
        if op == "add_friend":
            return "requested"
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

    def _gate(self, spec: CommandSpec, peer: str, now: datetime, action: str) -> str:
        """返回拒绝理由；空串 = 放行。

        读类只受时间窗约束（半夜翻联系人一样是「机器在跑」）；写类走完整守门。
        """
        decision = self._guard.check(self._account, peer, now=now, action=action)
        if decision.allowed:
            return ""
        if not spec.writes and "时间窗" not in decision.reason:
            # 读操作不受黑名单/发送频次限制——它不打扰任何人。
            return ""
        return decision.reason


def parse_commands(data: dict[str, Any]) -> Sequence[dict[str, Any]]:
    """一帧里可能带一条或多条指令，统一成列表。"""
    if not isinstance(data, dict):
        return []
    batch = data.get("commands")
    if isinstance(batch, list):
        return [c for c in batch if isinstance(c, dict)]
    return [data] if data.get("op") else []
