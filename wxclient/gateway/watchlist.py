"""监听范围：哪些会话才轮到网关管。

**权威在 AI 端**——该盯哪些候选人是业务侧才知道的事（谁在招聘漏斗里、谁归这个专员），
不该让专员在桌面端手抄一份名单。所以这里的名单可以由 AI 端通过 WS 推下来
（``ev: "wechat.watchlist"``），推一次覆盖一次，网关只负责照办。

没推之前用本地兜底：默认「所有单聊都管、群聊一概不管」。本地的 only/never 仍留在
``GatewaySettings`` 里当应急开关（环境变量，界面上不再出现）——真出事要立刻掐掉某个
会话时，不用等 AI 端改数据。

判定必须发生在**打开会话之前**：打开一个聊天窗口本身就是打扰（窗口会弹到前台、未读被清掉）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

import structlog

_log = structlog.get_logger(__name__)

#: AI 端推监听列表用的事件名。
PUSH_EVENT = "wechat.watchlist"


def _clean(names: Iterable[str] | None) -> frozenset[str]:
    return frozenset(str(name).strip() for name in (names or ()) if str(name).strip())


@dataclass(frozen=True)
class WatchScope:
    """一份监听范围。"""

    only: frozenset[str] = frozenset()
    """只管这些会话（微信里显示的名字）。空 = 不限制。"""

    never: frozenset[str] = frozenset()
    """永远不管这些会话。优先级最高。"""

    allow_groups: bool = False
    """群聊要不要管。默认不管——群里替人自动应答的误伤面比单聊大得多。"""

    origin: str = "本地配置"
    """这份名单是哪来的，只用于日志和状态展示。"""

    def reason(self, peer_id: str, *, is_group: bool) -> str | None:
        """不该管这个会话的理由；``None`` = 可以管。"""
        if peer_id in self.never:
            return "在不处理名单里"
        if self.only and peer_id not in self.only:
            return "不在监听名单里"
        if is_group and not self.allow_groups:
            return "是群聊（默认不处理）"
        return None


class Watchlist:
    """当前生效的监听范围。可被 AI 端推来的名单整体替换。"""

    def __init__(self, scope: WatchScope | None = None) -> None:
        self._scope = scope or WatchScope()

    @classmethod
    def from_local(
        cls, *, only: Sequence[str] = (), never: Sequence[str] = (), allow_groups: bool = False
    ) -> Watchlist:
        return cls(WatchScope(_clean(only), _clean(never), allow_groups, "本地配置"))

    @property
    def scope(self) -> WatchScope:
        return self._scope

    def reason(self, peer_id: str, *, is_group: bool) -> str | None:
        return self._scope.reason(peer_id, is_group=is_group)

    def apply_push(self, data: dict) -> bool:
        """吃下 AI 端推来的一份名单，整体替换。返回是否真的换了。

        缺字段就沿用现有值——AI 端只想改「只管这几个人」时不必把 allow_groups 一起重发。
        坏数据宁可不换：名单被一个坏帧清空 = 网关开始回它本不该回的人。
        """
        if not isinstance(data, dict):
            _log.warning("监听列表推送格式不对，忽略")
            return False

        updated = self._scope
        if isinstance(data.get("only"), list):
            updated = replace(updated, only=_clean(data["only"]))
        if isinstance(data.get("never"), list):
            updated = replace(updated, never=_clean(data["never"]))
        if isinstance(data.get("allow_groups"), bool):
            updated = replace(updated, allow_groups=data["allow_groups"])
        # 比的是名单本身，不含 origin：AI 端推来一份和现状一模一样的名单不算「变了」。
        if updated == self._scope:
            return False
        self._scope = replace(updated, origin="AI 端推送")
        updated = self._scope
        _log.info(
            "监听列表已更新（AI 端推送）",
            只管=len(updated.only) or "全部单聊",
            不管=len(updated.never),
            含群聊=updated.allow_groups,
        )
        return True
