"""监听范围：哪些会话才轮到网关管。

**权威在 AI 端**——该盯哪些人是业务侧才知道的事（谁在招聘漏斗里、谁归这个专员），
不该让专员在桌面端手抄一份名单。名单由 AI 端通过 WS 推下来（``ev: "wechat.watchlist"``），
推一次覆盖一次，网关只负责照办。

★ 两条硬规矩（用户裁决 2026-08-14）：

1. **群和单聊一视同仁。** 原来写死「群聊一概不管」，于是「把某个招聘群纳入自动化」
   这件事在网关侧根本做不到，而单聊反过来一概都管。现在两者都只看名单——
   要不要管一个群，是 AI 端那份名单说了算的业务判断，不是网关的类型判断。

2. **名单是白名单，没在名单上就不管（fail-closed）。** 原来的兜底是「所有单聊都管」，
   意味着装上就会自动回复任何人——包括专员的家人朋友，这违反需求文档 §13.1
   「对象必须在 HR 数据权限范围内」。现在 AI 端没推名单之前**一个会话都不碰**，
   并把这件事喊出来（否则又变成「装上去什么都不回」的哑谜）。

名单落盘（``GatewayStore``）：进程重启后如果要等 AI 端重推才恢复，中间那段就是全哑的。

判定必须发生在**打开会话之前**：打开一个聊天窗口本身就是打扰（窗口会弹到前台、未读被清掉）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
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
    """只管这些会话（微信里显示的名字；**群名和人名放在同一份名单里**）。"""

    never: frozenset[str] = frozenset()
    """永远不管这些会话。优先级最高——应急掐断用。"""

    received: bool = False
    """AI 端是否推过名单。没推过时连「名单是空的」都算不得数，只能说「还不知道」。"""

    origin: str = "本地配置"
    """这份名单是哪来的，只用于日志和状态展示。"""

    def reason(self, peer_id: str) -> str | None:
        """不该管这个会话的理由；``None`` = 可以管。

        ★ 白名单语义：**只有在名单上的才管**。空名单 = 一个都不管，不是「不限制」——
        「不限制」这个默认值正是「装上就自动回复所有人」的来源。
        """
        if peer_id in self.never:
            return "在不处理名单里"
        if peer_id in self.only:
            return None
        if not self.received and not self.only:
            return "还没收到 AI 端的监听名单"
        return "不在监听名单里"


class Watchlist:
    """当前生效的监听范围。可被 AI 端推来的名单整体替换。"""

    def __init__(
        self,
        scope: WatchScope | None = None,
        *,
        persist: Callable[[WatchScope], None] | None = None,
    ) -> None:
        self._scope = scope or WatchScope()
        self._persist = persist
        self._warned_empty = False

    @classmethod
    def from_local(cls, *, only: Sequence[str] = (), never: Sequence[str] = (), **kwargs) -> Watchlist:
        """环境变量兜底/应急开关。这里给的 ``only`` **不算** AI 端推过。"""
        return cls(WatchScope(_clean(only), _clean(never), False, "本地配置"), **kwargs)

    @property
    def scope(self) -> WatchScope:
        return self._scope

    @property
    def ready(self) -> bool:
        """有没有一份能用的名单。``False`` 时网关一个会话都不会碰。"""
        return bool(self._scope.only) or self._scope.received

    def reason(self, peer_id: str) -> str | None:
        return self._scope.reason(peer_id)

    def watched(self) -> list[str]:
        """要盯的会话名（名单减掉应急拉黑的），排序后返回。

        ★ 这份名单**就是入站要读的会话清单**（用户裁决 2026-08-14）。原来是先扫微信的
        未读列表、再读会话——可未读这个信号根本不可靠：群多半设了消息免打扰不进未读，
        专员在手机上看过一眼的也不再是未读，于是该回的消息网关压根看不见。
        改成直接读名单里的会话、按游标判新，跟「微信觉得你读没读过」彻底脱钩。
        """
        return sorted(name for name in self._scope.only if name not in self._scope.never)

    def warn_if_idle(self) -> None:
        """名单为空时喊一嗓子——只喊一次，别把日志刷满。

        不喊的话，现场表现就是「微信有新消息、网关一声不吭」，看的人只会以为功能坏了
        （2026-08-13 用户就是这么问的：「但是没有回复消息呢」）。
        """
        if self.ready:
            self._warned_empty = False
            return
        if self._warned_empty:
            return
        self._warned_empty = True
        _log.warning(
            "监听名单是空的，暂不处理任何会话——等 AI 端把「可以自动回复的人/群」推下来",
        )

    def apply_scope(self, scope: WatchScope) -> None:
        """直接装一份名单（从库里读回来时用），不落盘、不打日志。"""
        self._scope = scope

    def apply_push(self, data: dict) -> bool:
        """吃下 AI 端推来的一份名单，整体替换。返回是否真的换了。

        缺字段就沿用现有值——AI 端只想改 ``never`` 时不必把 ``only`` 一起重发。
        坏数据宁可不换：名单被一个坏帧清空 = 网关开始回它本不该回的人，
        或者反过来突然全哑。
        """
        if not isinstance(data, dict):
            _log.warning("监听列表推送格式不对，忽略")
            return False

        updated = self._scope
        if isinstance(data.get("only"), list):
            updated = replace(updated, only=_clean(data["only"]))
        if isinstance(data.get("never"), list):
            updated = replace(updated, never=_clean(data["never"]))
        updated = replace(updated, received=True)
        # 比的是名单本身，不含 origin：AI 端推来一份和现状一模一样的名单不算「变了」。
        if updated == replace(self._scope, received=True):
            self._scope = updated
            return False
        self._scope = replace(updated, origin="AI 端推送")
        self._warned_empty = False
        if self._persist is not None:
            self._persist(self._scope)
        _log.info(
            "监听列表已更新（AI 端推送）",
            只管=len(self._scope.only),
            不管=len(self._scope.never),
        )
        return True
