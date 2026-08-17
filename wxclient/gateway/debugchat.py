"""调试会话：把 AI 端一轮的产出翻成界面上要显示的几行。

**纯函数、不碰 Tk、不碰网络**，所以在 Linux 上可测；界面只负责把它排出来。

为什么单独一层：这里的知识是「AI 端 wechat_ops 那张卡长什么样」（066 的
``wechat_command_card``），既不属于界面，也不该埋在 ``runner`` 里。卡的字段改了，
只改这一处。

⚠️ 认不出来的卡**不当成没产出**：宁可把原始 JSON 摆到界面上让人自己看，
也不要显示一句「AI 没有回复」——那是在替对面说它没说过的话。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 一条指令在界面上摘要成一行时，参数最多铺多长。指令参数里可能有整段文案。
_ARG_BUDGET = 60


@dataclass(frozen=True)
class DebugReply:
    """一轮的产出，已经是「可以直接摆出来」的形态。"""

    text: str = ""
    """给人看的那句话（卡里的 ``reply``/``ask``，没有就退回流式文本）。"""

    commands: tuple[str, ...] = field(default_factory=tuple)
    """这一轮下发了哪几条指令，每条一行摘要。"""

    skipped: tuple[str, ...] = field(default_factory=tuple)
    """没能下发的条目及原因。"""

    error: str = ""
    """AI 端明确报的错。非空时上面几项通常是空的。"""

    raw: str = ""
    """认不出形状时的原样 JSON。只在 ``text`` 也拿不到时才有内容。"""


def summarize_command(command: Any) -> str:
    """一条指令 → 一行人话摘要，如 ``post_moments（content=今天招…）``。"""
    if not isinstance(command, dict):
        return str(command)
    op = str(command.get("op") or "?")
    args = command.get("args")
    if not isinstance(args, dict) or not args:
        return op
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > _ARG_BUDGET:
            text = text[:_ARG_BUDGET] + "…"
        parts.append(f"{key}={text}")
    return f"{op}（{'，'.join(parts)}）"


def format_turn(result: Any) -> DebugReply:
    """把 ``TurnResult`` 翻成 :class:`DebugReply`。

    取值优先级：卡里的 ``reply`` → ``ask`` → 流式文本。三样都没有才落到 ``raw``。
    """
    error = getattr(result, "error", None)
    if error:
        message = str(error.get("message") or "") if isinstance(error, dict) else str(error)
        code = str(error.get("code") or "") if isinstance(error, dict) else ""
        return DebugReply(error=f"{message}（{code}）" if code and message else (message or code or "AI 端报错"))

    stream_text = str(getattr(result, "text", "") or "").strip()
    card = getattr(result, "card", None)
    body = card.get("body") if isinstance(card, dict) else None
    if not isinstance(body, dict):
        # 没有卡：把流式文本当产出。也没有就把整张卡原样摆出来（见模块说明）。
        return DebugReply(text=stream_text) if stream_text else DebugReply(raw=_dump(card))

    text = str(body.get("reply") or body.get("ask") or "").strip() or stream_text
    commands = tuple(
        summarize_command(item) for item in (body.get("wechat_commands") or []) if item
    )
    skipped = tuple(str(item) for item in (body.get("skipped") or []) if item)
    if not text and not commands and not skipped:
        return DebugReply(raw=_dump(card))
    return DebugReply(text=text, commands=commands, skipped=skipped)


def _dump(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)
