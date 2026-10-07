"""监听范围：哪些会话才轮到网关管。

**权威在业务后端**（spec 085）——该盯哪些人是业务侧才知道的事（谁在招聘漏斗里、谁归这个专员），
不该让专员在桌面端手抄一份名单。名单**就是会话名映射派生出来的**：`GET /client/contacts`
回来那些 `accepted` 的会话名（用户 2026-08-20：就用这条既有接口，不新增接口）。
派生在 `GatewayRunner._apply_derived_watchlist()`，读的是本机 `gateway_contacts` 表。

迁移期 AI 端那条 WS 推送（``ev: "wechat.watchlist"``）仍能落地，直到后端这份真的给出东西为止
（见 `apply_push` 的 `_backend_seen`）——移交途中不能有「谁都不管」的窗口。

★ 两条硬规矩（用户裁决 2026-08-14）：

1. **群和单聊一视同仁。** 原来写死「群聊一概不管」，于是「把某个招聘群纳入自动化」
   这件事在网关侧根本做不到，而单聊反过来一概都管。现在两者都只看名单——
   要不要管一个群，是 AI 端那份名单说了算的业务判断，不是网关的类型判断。

2. **名单是白名单，没在名单上就不管（fail-closed）。** 原来的兜底是「所有单聊都管」，
   意味着装上就会自动回复任何人——包括专员的家人朋友，这违反需求文档 §13.1
   「对象必须在 HR 数据权限范围内」。现在拿到名单之前**一个会话都不碰**，
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
            return "还没拿到监听名单"
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
        #: 业务后端给过名单没有。给过之后 AI 端的推送不再作数（spec 085 并存期）。
        self._backend_seen = False
        self._warned_ai_push_ignored = False

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
            "监听名单是空的，暂不处理任何会话——等会话名同步把「可以自动回复的人」带回来"
            "（spec 085：名单 = /client/contacts 里 accepted 的那些会话名）",
        )

    def exclude(self, peer_id: str) -> bool:
        """配置中心点「移出」：把这个会话放进本机「不管」（``never``），落盘。返回是否真移出了。

        ★ 移出发生在**本机**（用户 2026-09-24：「删除报不在其白名单内，应该把界面上删除」）。
          名单权威 0.1.83 起在业务后端，AI 端手上已经没有这份名单，原来请 AI 端删
          永远回「本来就不在」，界面上那一行怎么点都还在。业务后端又没有「移出」的接口，
          所以落进 ``never``，重启会读回来。
        ★ 2026-10-07 起移出**不是永久的**：业务端同步再带回这个人就放回来（`include_synced`）。
        """
        name = str(peer_id or "").strip()
        if not name or name not in self.watched():
            return False
        self._scope = replace(self._scope, never=self._scope.never | {name})
        if self._persist is not None:
            self._persist(self._scope)
        _log.info("已在本机移出监听名单", 只管=len(self._scope.only), 不管=len(self._scope.never))
        return True

    def include(self, peer_id: str) -> bool:
        """把这个会话从本机「不管」里拿出来，落盘。返回是否真拿出来了。

        只给「查无此人被自动移出、后来又确认通过了好友」那一种情况用（``runner._on_friend_accepted``）：
        名字在微信里重新找得到了，还一直挂在 ``never`` 里就永远不回他。
        """
        name = str(peer_id or "").strip()
        if not name or name not in self._scope.never:
            return False
        self._scope = replace(self._scope, never=self._scope.never - {name})
        if self._persist is not None:
            self._persist(self._scope)
        _log.info("已把这个会话放回监听名单", 会话=name, 只管=len(self._scope.only), 不管=len(self._scope.never))
        return True

    def include_synced(self, names: Iterable[str]) -> int:
        """业务端这次同步带回来的人，从本机「不管」里拿出来，落盘一次。返回放回了几个。

        ★ 用户 2026-10-07：「只要同步名单有就同步过来，如果后面轮询查无此人就再删除。」
          推翻 0.1.85「本机移出的人，同步也不带回来」——那样移出过的人业务端再怎么给也进不来。
          放回来的人如果真查无此人，`runner._on_contact_not_found` 会照旧连续 3 轮后再移出。
        """
        back = self._scope.never & _clean(names)
        if not back:
            return 0
        self._scope = replace(self._scope, never=self._scope.never - back)
        if self._persist is not None:
            self._persist(self._scope)
        _log.info("同步带回了本机移出过的会话", 放回=len(back), 不管=len(self._scope.never))
        return len(back)

    def apply_scope(self, scope: WatchScope) -> None:
        """直接装一份名单（从库里读回来时用），不落盘、不打日志。"""
        self._scope = scope

    def apply_push(self, data: dict) -> bool:
        """吃下 AI 端推来的一份名单，整体替换。返回是否真的换了。

        缺字段就沿用现有值——AI 端只想改 ``never`` 时不必把 ``only`` 一起重发。
        坏数据宁可不换：名单被一个坏帧清空 = 网关开始回它本不该回的人，
        或者反过来突然全哑。

        ★ 并存期（spec 085）：业务后端一旦给过名单，AI 端这条推送就**不再作数**。
        名单权威已经移交，两个来源同时写会让「屏幕上这份到底听谁的」变成一个说不清的问题。
        在后端第一次给出名单之前，这条路照旧生效——移交途中不能有「谁都不管」的窗口。
        """
        if self._backend_seen:
            if not self._warned_ai_push_ignored:
                self._warned_ai_push_ignored = True
                _log.info("监听名单以业务后端为准，这条推送不采用")
            return False
        return self._merge(data, "AI 端推送", "监听列表推送格式不对，忽略")

    def apply_backend(self, data: dict) -> bool:
        """吃下业务后端同步来的一份名单（spec 085 之后的权威来源）。

        ★ 调用方**只在真派生出东西时**调这个（见 `GatewayRunner._apply_derived_watchlist()`）：
        派生出来是空的，那是「这轮没拿到」，不是「名单空了」。名单是 fail-closed 白名单，
        把没拿到当成空名单 = 网关一个会话都不回，而且这一调就再也退不回 AI 端那条推送。
        """
        changed = self._merge(data, "业务后端会话名同步派生", "派生出来的名单格式不对，忽略")
        # 形状对就算接管了——哪怕内容和现状一样。接管与否看的是「后端说过话没有」，
        # 不是「说的话有没有让名单变化」。
        if isinstance(data, dict):
            self._backend_seen = True
        return changed

    def _merge(self, data: dict, origin: str, bad_shape_msg: str) -> bool:
        if not isinstance(data, dict):
            _log.warning(bad_shape_msg)
            return False

        updated = self._scope
        if isinstance(data.get("only"), list):
            updated = replace(updated, only=_clean(data["only"]))
        if isinstance(data.get("never"), list):
            updated = replace(updated, never=_clean(data["never"]))
        updated = replace(updated, received=True)
        # 比的是名单本身，不含 origin：推来一份和现状一模一样的名单不算「变了」。
        if updated == replace(self._scope, received=True):
            self._scope = updated
            return False
        self._scope = replace(updated, origin=origin)
        self._warned_empty = False
        if self._persist is not None:
            self._persist(self._scope)
        _log.info(
            f"监听列表已更新（{origin}）",
            只管=len(self._scope.only),
            不管=len(self._scope.never),
        )
        return True
