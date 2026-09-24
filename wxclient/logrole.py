"""每一行日志说的是**跟谁打交道**：微信 / AI 端 / 业务后端 / 本机。

排障时第一个要分的就是这三方：微信客户端（UIA 操作）、AI 端（那条 WS）、业务后端
（Client Token 走的那几条 HTTP）。原来这三方混在一条时间线里，谁也看不出「这一步
是在等 AI 端，还是在等业务后端」——同样是「超时」，前者要去看 AI 服务，后者要去看
api.laoxiaozhi.cn，方向完全相反（用户 2026-08-30：实时日志里要能看出角色）。

**判据只有两条，按优先级来**：

1. **写日志的人自己说的**——``_log.info("…", 对端=logrole.AI)``。混着跟两三方打交道的
   模块（``runner`` / ``inbound`` / ``outbound`` / ``commands``）走这条，通常的写法是在
   模块顶上 ``bind`` 出几个带角色的 logger，调用处只换个名字。
2. **模块默认**——一个模块只跟一方打交道的（``wsclient`` 只跟 AI 端、``ticket`` 只跟
   业务后端、``wechat_io`` 只碰微信），在下面登记一次就够，不必逐条标。

**为什么不做「事件名 → 角色」的对照表**：那是把同一件事在两处各写一遍，改了日志措辞
而忘了改表，界面上就会挂着一个错的角色——错的角色比没有角色更误导人。角色写在写日志
的那一行上，只有一处会漂。

没登记也没显式标的，一律算 ``本机``：这台 Windows 上自己的事（起停服务、排期、
队列、界面），不涉及任何一方对端。宁可少认，不可乱认。
"""

from __future__ import annotations

#: 结构化日志里显式指定角色用的键。渲染时会被吃掉，不会出现在消息尾巴的 kv 串里。
ROLE_KEY = "对端"

#: 微信客户端本身：UIA 操作、读会话、发消息、加好友那一层。
WECHAT = "微信"

#: 劳小智 AI 服务，也就是那条 WS 门（派轮 / 卡片 / 指令 / 回执 / 名单 / 能力上报）。
AI = "AI端"

#: 业务后端 api.laoxiaozhi.cn：换票、会话名、候选人回写、消息事件、客户端状态。
BACKEND = "业务后端"

#: 这台电脑自己的事，不涉及任何对端。默认档。
LOCAL = "本机"

#: 界面上认得的全部角色（顺序 = 界面里的排列顺序）。
ROLES: tuple[str, ...] = (WECHAT, AI, BACKEND, LOCAL)

#: 模块 → 默认角色。键是 logger 名的**前缀**（按点分段匹配，取最长的那条）。
#: 只登记「整个模块都只跟这一方打交道」的；混着来的模块不登记，靠调用处显式标。
_MODULE_ROLES: dict[str, str] = {
    # ── 微信：这几处从头到尾都在操作微信客户端 ──────────────────
    "wxclient.wechat_api": WECHAT,
    "wxclient.queue.worker": WECHAT,
    "wxclient.gateway.wechat_io": WECHAT,
    "wxclient.gateway.friends": WECHAT,
    "wxclient.gateway.friendcheck": WECHAT,
    "wxclient.gateway.autofriend": WECHAT,
    # 入站读消息 / 出站发消息 / 执行 AI 端下发的微信指令：这三条主干**默认**都在操作
    # 微信客户端；其中夹着的少数几行（派轮给 AI 端、上报业务后端、纯本机判定）在调用处
    # 用 `_ai` / `_backend` / `_local` 显式标了角色，会盖掉这个默认值。
    "wxclient.gateway.inbound": WECHAT,
    "wxclient.gateway.outbound": WECHAT,
    "wxclient.gateway.commands": WECHAT,
    # ── AI 端：那条 WS 上的往返 ──────────────────────────────
    "wxclient.gateway.wsclient": AI,
    "wxclient.gateway.watchlist": AI,
    # 产出物的字节是随指令从 AI 端推下来的（067），不是自己去哪儿下载的
    "wxclient.gateway.download": AI,
    # ── 业务后端：Client Token 走的那几条 HTTP ─────────────────
    "wxclient.gateway.ticket": BACKEND,
    "wxclient.gateway.backend_client": BACKEND,
    "wxclient.gateway.contacts": BACKEND,
    "wxclient.gateway.candidates": BACKEND,
    "wxclient.gateway.candidate_mode": BACKEND,
    "wxclient.gateway.message_events": BACKEND,
    "wxclient.ui.client_api": BACKEND,
    "wxclient.ui.status_reporter": BACKEND,
}


def role_of(logger_name: str | None, explicit: object = None) -> str:
    """这一行日志的对端是谁。显式标注优先，其次模块默认，最后算本机。"""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return module_role(logger_name)


def module_role(logger_name: str | None) -> str:
    """按 logger 名找模块默认角色。取**最长**的那条前缀，子模块能盖住父包。"""
    if not logger_name:
        return LOCAL
    parts = logger_name.split(".")
    for end in range(len(parts), 0, -1):
        role = _MODULE_ROLES.get(".".join(parts[:end]))
        if role is not None:
            return role
    return LOCAL
