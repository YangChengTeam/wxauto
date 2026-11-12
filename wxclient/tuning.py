"""极速模式：原来写死在各模块里的时间项，集中到一张表（用户 2026-09-30）。

「这些原来写死的地板值搞一个专门菜单」「这些时间都弄到里面去，配置中心的不要」——
配置中心里已经有格子的（工作时间窗、各类上限、查好友通过期限……）不进这张表，
这里只收**代码里写死、界面上原来改不了**的那些。

★ 只在极速模式开着时生效。关着时各模块用的就是它们自己写死的那个数，界面上灰着显示、改不了。
★ 这个模块只用标准库：配置中心（`ui/settings_store.py`）要在启动最早的几秒读它，
  不能把 pydantic / structlog / pywinauto 拉进来。

生效方式：网关每次启动（`gateway.factory.build_gateway`）调一次 `apply`，把值写到对应模块的
全局量上——那些模块都是在**用的时候**才读自己的全局量，所以启动时改一次就管住整个进程。
关着时 `apply` 把它们恢复成模块自己的原值（上一次开过、这次关掉重启，也不会残留）。
`uia.py` 的几项（停顿倍率、等名片、等输入框）直接读本模块的全局量，见文件末尾。
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from dataclasses import dataclass

#: 环境变量前缀。键名＝前缀＋`Knob.name`，和其余配置同一个 gateway.env。
PREFIX = "WXCLIENT_GATEWAY_DEMO_"
TOGGLE_KEY = "WXCLIENT_GATEWAY_DEMO_MODE"

_TRUE = {"1", "true", "yes", "on", "是"}


@dataclass(frozen=True)
class Knob:
    name: str            # 键名后缀，如 DEBOUNCE_SECONDS
    label: str
    help: str
    unit: str
    section: str         # 界面分段：demo_wait / demo_conn / demo_keep
    locked: str          # 平时（极速关着）生效的值，界面上灰着显示的就是它
    default: str         # 极速开着、没填过时用的值
    target: str          # "module.path:ATTR" 写到模块全局量；"settings:attr" 写到 GatewaySettings
    kind: str = "float"  # float | int
    minimum: float = 1.0

    @property
    def key(self) -> str:
        return PREFIX + self.name


KNOBS: tuple[Knob, ...] = (
    # ── 等待与排队：演示时最直接让人干等的几项 ──
    Knob("DEBOUNCE_SECONDS", "回复前静默", "对方停下多久才开始回", "秒", "demo_wait",
         "3", "1", "settings:debounce_seconds", minimum=0),
    # 「正聊着的时长 / 每轮读几个会话 / 正聊着每轮几个」0.1.128 删了：入站改本地库监听，
    # 不再按名单开窗轮询，这三格只管 UIA 开窗那条路（`settings_store._RETIRED_KEYS` 清老键）。
    Knob("AUTO_ACCEPT_LIMIT", "一趟通过几个申请", "自动通过好友申请", "个", "demo_wait",
         "5", "20", "wxclient.gateway.friends:AUTO_ACCEPT_LIMIT", kind="int"),
    Knob("PAUSE_SCALE", "操作停顿倍率", "加好友/朋友圈每步停顿；0=不停", "倍",
         "demo_wait", "1", "0", "wxclient.tuning:PAUSE_SCALE", minimum=0),
    Knob("CONTACTS_SYNC_INTERVAL_SECONDS", "名单同步间隔", "多久从后端拉一次监听名单", "秒",
         "demo_wait", "120", "15", "wxclient.gateway.runner:CONTACTS_SYNC_INTERVAL_SECONDS"),
    Knob("SCHEDULE_CHECK_INTERVAL_SECONDS", "定时指令检查", "多久看一次到点的定时指令", "秒",
         "demo_wait", "30", "5", "wxclient.gateway.schedule:CHECK_INTERVAL_SECONDS"),
    Knob("RECEIPT_FLUSH_INTERVAL_SECONDS", "回执补报间隔", "没报出去的回执多久重报", "秒",
         "demo_wait", "20", "5", "wxclient.gateway.runner:RECEIPT_FLUSH_INTERVAL_SECONDS"),
    Knob("MESSAGE_EVENT_FLUSH_INTERVAL_SECONDS", "消息事件上报", "聊天记录多久报一批", "秒",
         "demo_wait", "20", "5", "wxclient.gateway.runner:MESSAGE_EVENT_FLUSH_INTERVAL_SECONDS"),
    # ── 连接与超时 ──
    Knob("PING_INTERVAL_SECONDS", "连接心跳间隔", "和 AI 端多久 ping 一次", "秒", "demo_conn",
         "20", "20", "wxclient.gateway.wsclient:PING_INTERVAL_SECONDS"),
    Knob("PING_TIMEOUT_SECONDS", "连接心跳超时", "多久没回应算断线", "秒", "demo_conn",
         "120", "120", "wxclient.gateway.wsclient:PING_TIMEOUT_SECONDS"),
    Knob("KEEPALIVE_INTERVAL_SECONDS", "保活间隔", "空闲时多久探一次连接", "秒", "demo_conn",
         "30", "30", "wxclient.gateway.runner:KEEPALIVE_INTERVAL_SECONDS"),
    Knob("TURN_HEARTBEAT_SECONDS", "处理中提示间隔", "没跑完时多久报一声", "秒",
         "demo_conn", "30", "30", "wxclient.gateway.runner:TURN_HEARTBEAT_SECONDS"),
    Knob("TURN_TIMEOUT_SECONDS", "一轮对话上限", "等 AI 端回复最长多久", "秒", "demo_conn",
         "180", "180", "settings:turn_timeout_seconds"),
    Knob("TICKET_TIMEOUT_SECONDS", "换票超时", "向业务端换票最长等多久", "秒", "demo_conn",
         "3", "3", "settings:ticket_timeout_seconds"),
    Knob("PROFILE_WAIT_SECONDS", "等名片出现", "加好友搜号后最多等几秒", "秒", "demo_conn",
         "12", "12", "wxclient.tuning:PROFILE_WAIT_SECONDS"),
    Knob("EDIT_ENABLE_WAIT_SECONDS", "等输入框可用", "发消息前最多等几秒", "秒", "demo_conn",
         "3", "3", "wxclient.tuning:EDIT_ENABLE_WAIT_SECONDS"),
    # ── 保留期限 ──
    Knob("ATTACHMENT_RETENTION_HOURS", "附件本机保留", "对方发来的文件留多久", "小时", "demo_keep",
         "24", "24", "wxclient.gateway.attachments:RETENTION_HOURS"),
    Knob("RECEIPT_MAX_AGE_HOURS", "回执重报期限", "报不出去的回执重报多久", "小时", "demo_keep",
         "24", "24", "wxclient.gateway.runner:RECEIPT_MAX_AGE_HOURS"),
    Knob("MESSAGE_EVENT_MAX_AGE_HOURS", "消息事件重报期限", "报不出去的记录重报多久", "小时",
         "demo_keep", "24", "24", "wxclient.gateway.message_events:MAX_AGE_HOURS"),
    Knob("PENDING_MAX_AGE_HOURS", "待回复保留", "没回出去的消息留多久", "小时", "demo_keep",
         "24", "24", "wxclient.gateway.inbound:PENDING_MAX_AGE_HOURS"),
    Knob("SENT_ECHO_MAX_AGE_HOURS", "发出记录保留", "认自己发的消息用", "小时", "demo_keep",
         "48", "48", "wxclient.gateway.inbound:SENT_ECHO_MAX_AGE_HOURS"),
    Knob("HISTORY_MAX_AGE_DAYS", "聊天记录保留", "本机聊天记录留几天", "天", "demo_keep",
         "14", "14", "wxclient.gateway.inbound:HISTORY_MAX_AGE_DAYS"),
    Knob("SCHEDULE_MAX_LATE_HOURS", "定时指令补晚上限", "错过超过多久就不补", "小时",
         "demo_keep", "6", "6", "wxclient.gateway.schedule:MAX_LATE_HOURS"),
)

# ── uia.py 读的三项：放在这里而不是 uia 里，免得为了改一个数去 import pywinauto ──
PAUSE_SCALE = 1.0
PROFILE_WAIT_SECONDS = 12.0
EDIT_ENABLE_WAIT_SECONDS = 3.0

#: 各模块全局量第一次被改之前的原值，关掉极速模式时照它恢复。
_originals: dict[str, object] = {}


def is_on(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return str(source.get(TOGGLE_KEY, "")).strip().lower() in _TRUE


def value_of(knob: Knob, env: Mapping[str, str] | None = None) -> float | int:
    """极速开着时这一项的值：填了用填的，没填或填坏了用 `default`；不低于 `minimum`。"""
    source = os.environ if env is None else env
    raw = str(source.get(knob.key, "")).strip() or knob.default
    try:
        number = float(raw)
    except ValueError:
        number = float(knob.default)
    number = max(number, knob.minimum)
    return int(number) if knob.kind == "int" else number


def settings_overrides(env: Mapping[str, str] | None = None) -> dict[str, float | int]:
    """写到 GatewaySettings 上的那几项（`settings:` 目标）。极速关着 → 空。"""
    if not is_on(env):
        return {}
    return {
        knob.target.split(":", 1)[1]: value_of(knob, env)
        for knob in KNOBS if knob.target.startswith("settings:")
    }


def apply(env: Mapping[str, str] | None = None) -> dict[str, float | int]:
    """把模块全局量设成极速值（开着）或原值（关着）。返回这次改了哪些（给日志用）。"""
    on = is_on(env)
    changed: dict[str, float | int] = {}
    for knob in KNOBS:
        if knob.target.startswith("settings:"):
            continue
        module_name, attr = knob.target.split(":", 1)
        module = importlib.import_module(module_name)
        if knob.target not in _originals:
            _originals[knob.target] = getattr(module, attr)
        if on:
            value = value_of(knob, env)
            setattr(module, attr, value)
            changed[knob.name] = value
        else:
            setattr(module, attr, _originals[knob.target])
    return changed
