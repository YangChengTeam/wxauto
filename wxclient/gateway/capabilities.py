"""逐能力探测：**这台机器现在到底能做哪几件事**（074，需求文档 §12.2）。

原来只有一个总开关「微信 UIA 可用 / 不可用」，于是所有「做不了」都长一个样，而且要
**执行到一半**才发现——比如没配取文件通道时，发海报会一路走到取字节那一步才拒，
用户看到的是一条不知所云的失败。需求文档 §12.2 要的是逐能力上报，并且在**创建任务前**
和**执行前**各校验一次。

这一层只回答「能不能」，不回答「该不该」：该不该由守门（频次配额/时间窗/黑名单）管。
两者分开是因为原因不同——「做不了」要去修环境，「不让做」是规则挡的，给用户的话完全两样。

判据都是**当下真能查到的事实**，不猜：微信登没登、UIA 暴没暴露、是不是只走流程不真发、
取文件通道配没配。猜出来的能力表比没有更糟——它会让 AI 端理直气壮地承诺做不到的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 能力键。按需求文档 §12.2 的分类切，粒度对着「用户会问的那件事」。
CAP_PRIVATE_MESSAGE = "private_message"
CAP_GROUP_MESSAGE = "group_message"
CAP_SEND_FILES = "send_files"
CAP_ADD_FRIEND = "add_friend"
CAP_ACCEPT_FRIEND = "accept_friend"
CAP_MOMENTS = "moments"
CAP_MOMENTS_LIKE = "moments_like"
CAP_MOMENTS_COMMENT = "moments_comment"
CAP_READ = "read"
#: 发消息的回执里能带回发送凭据（`message_id` + `delivery_status`，0.1.77 起）。AI 端投面试天气提醒前
#: 看这一项：老版本助手发了也报不出凭据，业务端只能记「结果未知」交 HR——不如在投递前就明确拒。
CAP_DELIVERY_RECEIPT = "delivery_receipt"
#: AI 端 123 节前祝福：认得回复卡上的 `holiday_greeting`（过截止时间剥掉那句、带祝福那段的出站事件挂业务端指令号）。
#: AI 端没见到这一项就不向业务端占位、不拼祝福——老助手不认这个槽位，发了也对不上账（0.1.89 起）。
CAP_HOLIDAY_GREETING = "holiday_greeting_v1"
#: AI 端 138 开场发送协调：逐条报来源 + 好友建立事件，开场指令带名额凭据、回执报发了/没发/不知道。
#: 前提是认得出本机登录的是哪个微信（`accountRef`）——AI 端拿它把同一个号的两条开场路并成一个名额。
CAP_DELIVERY_COORDINATION = "delivery_coordination_v1"

#: 网关 → AI 端上报能力用的 op。
CAPABILITIES_OP = "wechat.capabilities"

CAPABILITY_LABELS: dict[str, str] = {
    CAP_PRIVATE_MESSAGE: "私聊发消息",
    CAP_GROUP_MESSAGE: "往群里发消息",
    CAP_SEND_FILES: "发文件/图片",
    CAP_ADD_FRIEND: "加好友",
    CAP_ACCEPT_FRIEND: "通过好友申请",
    CAP_MOMENTS: "发朋友圈",
    CAP_MOMENTS_LIKE: "给朋友圈点赞",
    CAP_MOMENTS_COMMENT: "给朋友圈评论",
    CAP_READ: "查看联系人/聊天记录/朋友圈",
    CAP_DELIVERY_RECEIPT: "回报消息发送凭据",
    CAP_HOLIDAY_GREETING: "节前祝福对账",
    CAP_DELIVERY_COORDINATION: "开场发送协调",
}

#: op → 能力键。没登记的 op 一律按「读」处理（读类不改变外部世界）。
CAPABILITY_OF_OP: dict[str, str] = {
    "send_message": CAP_PRIVATE_MESSAGE,
    "send_files": CAP_SEND_FILES,
    "add_friend": CAP_ADD_FRIEND,
    "accept_new_friends": CAP_ACCEPT_FRIEND,
    "post_moments": CAP_MOMENTS,
    "like_moments": CAP_MOMENTS_LIKE,
    "comment_moments": CAP_MOMENTS_COMMENT,
}


#: 契约 §8 的 ``capabilityStatus`` 三档。
CAPABILITY_PASSED = "passed"
CAPABILITY_LIMITED = "limited"
CAPABILITY_FAILED = "failed"


def capability_status(caps: dict[str, Capability]) -> str:
    """把能力表压成企业端要的那一档（契约 §8）。

    ``passed`` 才会让聚合状态进入「运行中」，所以这里**不许往好了说**：
    dry-run（只走流程不真发）下写类能力全是不可用，如实报 ``limited``——报 passed
    的话，企业端显示「运行中」，而这台机器一条消息都不会真的发出去。
    """
    if not any(cap.available for cap in caps.values()):
        return CAPABILITY_FAILED
    if all(cap.available for cap in caps.values()):
        return CAPABILITY_PASSED
    return CAPABILITY_LIMITED


def capability_of(op: str, *, to_group: bool = False) -> str:
    """这个 op 用到哪项能力。发给群的私聊消息算「往群里发」——两者的开关是分开的。"""
    if op == "send_message" and to_group:
        return CAP_GROUP_MESSAGE
    return CAPABILITY_OF_OP.get(op, CAP_READ)


@dataclass(frozen=True)
class Capability:
    key: str
    available: bool
    reason: str = ""
    """不可用时说人话——这句会一路传到用户眼前，所以不许写术语。"""

    @property
    def label(self) -> str:
        return CAPABILITY_LABELS.get(self.key, self.key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "label": self.label,
            **({"reason": self.reason} if self.reason else {}),
        }


def probe_capabilities(
    *,
    uia: dict[str, Any] | None,
    allow_real_actions: bool,
    has_file_channel: bool,
    supported_ops: set[str] | None = None,
    account_ref: str = "",
) -> dict[str, Capability]:
    """按当下的真实条件算出能力表。

    - ``uia``：`uia_probe.probe_wechat_uia()` 的结果。没登录 / UIA 没暴露 → 全都做不了。
    - ``allow_real_actions``：只走流程不真发时，写类能力**照实说做不了**。
      这一条尤其重要：dry-run 下每条写指令都会回 done，AI 端会照着回执告诉用户
      「已经发过去了」，而其实一条都没发。
    - ``has_file_channel``：没配取文件通道就发不了 AI 端产出的图。
    """
    caps: dict[str, Capability] = {}
    supported = supported_ops if supported_ops is not None else set(CAPABILITY_OF_OP)

    blocked = _wechat_blocker(uia)
    for key in CAPABILITY_LABELS:
        if blocked:
            caps[key] = Capability(key, False, blocked)
            continue
        caps[key] = Capability(key, True)

    if blocked:
        return caps

    # 写类能力的额外前提。
    write_keys = (
        CAP_PRIVATE_MESSAGE, CAP_GROUP_MESSAGE, CAP_SEND_FILES,
        CAP_ADD_FRIEND, CAP_ACCEPT_FRIEND, CAP_MOMENTS, CAP_MOMENTS_LIKE, CAP_MOMENTS_COMMENT,
        # 凭据只在真发出去时才有：只走流程不真发 = 没有凭据可报。
        CAP_DELIVERY_RECEIPT,
        # 指令号只挂在真发出去的那一段上：只走流程不真发 = 业务端永远确认不了，不如别让 AI 端占位。
        CAP_HOLIDAY_GREETING,
        # 协调只管「发出去的开场」：只走流程不真发 = 没有开场可协调、也报不出凭据。
        CAP_DELIVERY_COORDINATION,
    )
    if not allow_real_actions:
        for key in write_keys:
            caps[key] = Capability(
                key, False, "这台电脑现在只走流程、不会真的发出去（联调模式）"
            )
    if not has_file_channel and caps[CAP_SEND_FILES].available:
        caps[CAP_SEND_FILES] = Capability(
            CAP_SEND_FILES, False, "这台电脑没有配取文件的通道，发不了系统生成的图片和文件"
        )

    if not account_ref:
        # 认不出这台电脑登录的是哪个微信号：不声明这项，AI 端照旧流程走，消息照样发——
        # 报成「不可用」会把企业端状态拉成「受限」，可这台机器什么都没少。
        caps.pop(CAP_DELIVERY_COORDINATION)

    # 白名单里没有的 op = 这个版本的网关压根不支持。
    for op, key in CAPABILITY_OF_OP.items():
        if op not in supported and caps[key].available:
            caps[key] = Capability(key, False, "这台电脑上的助手版本还不支持这个动作")

    return caps


def _wechat_blocker(uia: dict[str, Any] | None) -> str:
    """微信本身有没有挡住一切。返回人话理由；空串 = 没挡。"""
    if not uia:
        return "还没探测到微信，请确认微信已经打开并登录"
    if not uia.get("logged_in"):
        if uia.get("login_window"):
            return "微信停在登录界面，请先扫码登录"
        return "没找到已登录的微信窗口，请先打开并登录微信"
    if not uia.get("uia_exposed"):
        return "微信窗口在，但读不到里面的内容（可能被最小化到托盘，或需要重启微信）"
    return ""


def capabilities_frame(caps: dict[str, Capability], account_ref: str = "") -> dict[str, Any]:
    """上报给 AI 端的形状。开场协调可用时一并报**是哪个微信号**（138）：AI 端只认这一条连接
    替这个号结算开场；报了能力却不报号，AI 端会整条拒掉，所以两者必须一起出现。"""
    frame: dict[str, Any] = {"capabilities": {key: cap.to_dict() for key, cap in caps.items()}}
    coordination = caps.get(CAP_DELIVERY_COORDINATION)
    if coordination is not None and coordination.available and account_ref:
        frame["delivery_identity"] = {"account_id": account_ref}
    return frame
