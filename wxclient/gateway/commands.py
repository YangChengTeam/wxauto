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

from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from wxclient.queue import TaskType

from .download import DownloadError, FileRef
from .guard import OutboundGuard

_log = structlog.get_logger(__name__)

#: AI 端推指令用的事件名（server → 网关）。
COMMAND_EVENT = "wechat.command"

#: 网关回执用的 op（网关 → server，走客户端帧格式）。
RESULT_OP = "wechat.result"


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
        raise CommandError("缺少必填参数 files（要 {file_id, url} 描述符，不是文件路径）")
    if len(refs) > 9:
        raise CommandError("一条指令最多带 9 个文件")
    return refs


#: op 白名单。**只有这张表里的 op 会被执行**，其余一律 rejected。
#:
#: 刻意不收 watch_chats / scan_messages：它们是有界时长的常驻扫描，属于入站循环，
#: 被一条指令占住同一个单消费者 worker 会把正常收消息饿死。
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
    ),
    "accept_new_friends": CommandSpec(
        TaskType.ACCEPT_NEW_FRIENDS, True,
        lambda a: {"limit": _limit(a, 1, 5)},
    ),
    "post_moments": CommandSpec(
        TaskType.POST_MOMENTS, True,
        lambda a: {"text": _req(a, "text"), "medias": list(a.get("medias") or [])},
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
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._guard = guard
        self._account = account_id
        self._allow_real = allow_real_actions
        self._confirmation = confirmation
        self._fetcher = fetcher
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(self, data: dict[str, Any]) -> CommandResult:
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

        result = await self._run(command_id, op, data)
        self._store.put_command_result(
            self._account, command_id,
            {"status": result.status, "reason": result.reason, "result": result.result},
        )
        return result

    async def _run(self, command_id: str, op: str, data: dict[str, Any]) -> CommandResult:
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

        now = self._clock()
        denial = self._gate(spec, peer, now)
        if denial:
            _log.info("指令被守门拦下", op=op, reason=denial)
            return CommandResult(command_id, "rejected", denial)

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

            if spec.writes and peer:
                # 主动发出去的也要计入频次——不然「被动回复限 20 条/时」会被主动触达绕开。
                self._store.record_send(self._account, peer, now)
            _log.info("指令执行完成", op=op, dry_run=(spec.writes and not self._allow_real))
            return CommandResult(command_id, "done", result=(raw or {}).get("result") or {})

    def _gate(self, spec: CommandSpec, peer: str, now: datetime) -> str:
        """返回拒绝理由；空串 = 放行。

        读类只受时间窗约束（半夜翻联系人一样是「机器在跑」）；写类走完整守门。
        """
        decision = self._guard.check(self._account, peer, now=now)
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
