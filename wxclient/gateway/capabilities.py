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


def capabilities_frame(caps: dict[str, Capability]) -> dict[str, Any]:
    """上报给 AI 端的形状。"""
    return {"capabilities": {key: cap.to_dict() for key, cap in caps.items()}}
