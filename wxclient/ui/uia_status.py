"""把只读 UIA 探测（`uia_probe`）的结果翻成契约 §8 的微信连接状态。

分成纯函数放这儿，是因为**这层判断没法在真机以外复现，但它决定企业端显示什么**：
「微信没开」和「微信开了但没登录」要给两句不同的话（一句是「打开微信」，一句是
「扫码登录」），报错了方向人就会去做无用功。纯函数至少能把映射表本身测死。

★ 一条克制：**拿不准就不报**。非 Windows（开发机）直接返回 None——在 Linux 上报一个
「微信没找到」，等于往企业端灌一条这台机器根本不存在的事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NOT_CONNECTED = "not_connected"
CONNECTING = "connecting"
CONNECTED = "connected"
FAILED = "failed"
DISCONNECTED = "disconnected"

#: 契约 §8 的失败码表。这里只用得上其中几条——剩下的（版本不支持、被别的分身占用）
#: 需要我们拿不到的信息（微信版本白名单、账号标识），拿不到就不假装判得出。
PROCESS_NOT_FOUND = "WECHAT_UIA_PROCESS_NOT_FOUND"
NOT_LOGGED_IN = "WECHAT_UIA_ACCOUNT_NOT_LOGGED_IN"
WINDOW_UNAVAILABLE = "WECHAT_UIA_WINDOW_UNAVAILABLE"
CONNECTION_LOST = "WECHAT_UIA_CONNECTION_LOST"

_MESSAGES = {
    PROCESS_NOT_FOUND: "未检测到正在运行的微信，请先打开并登录 PC 微信",
    NOT_LOGGED_IN: "PC 微信尚未登录，请完成微信登录后重新连接",
    WINDOW_UNAVAILABLE: "未找到可连接的微信主窗口，请重启 PC 微信后重试",
    CONNECTION_LOST: "微信 UIA 连接已断开，请检查 PC 微信运行状态",
}


@dataclass(frozen=True)
class UiaState:
    """一次探测的结论。``failure`` 只在 ``status == failed/disconnected`` 时有值。"""

    status: str
    failure_code: str = ""
    instance: dict[str, Any] | None = None
    capability_status: str = ""
    """契约 §8 的 ``capabilityStatus``：``passed`` / ``limited`` / ``failed``。

    只在 ``connected`` 时有值。契约允许先报一个不带它的 `connected`（服务端进
    `checking_capabilities`）、检测完再报一次；**我们直接在第一次就带上**——这台机器的
    能力表是从同一份探测结果里算出来的（`gateway/capabilities.py`），本来就没有
    「正在检测」那一段，硬造一个中间态只会让企业端多闪一下。
    """

    def with_capability(self, status: str) -> "UiaState":
        return UiaState(self.status, self.failure_code, self.instance, status)

    @property
    def failure(self) -> dict[str, Any] | None:
        if not self.failure_code:
            return None
        return {
            "code": self.failure_code,
            "message": _MESSAGES.get(self.failure_code, "微信连接失败"),
            # 表里这几条都是「用户自己能救」的：打开微信、登录、重启微信。
            "retryable": True,
        }

    def same_as(self, other: "UiaState | None") -> bool:
        """相同状态不重复上报（契约 §8）。失败码或能力档变了算不同状态——原因换了就得说。"""
        return other is not None and (
            self.status, self.failure_code, self.capability_status) == (
            other.status, other.failure_code, other.capability_status)


def classify(probe: dict[str, Any]) -> UiaState | None:
    """探测结果 → 状态。``None`` = 这台机器上判断不了，别上报。"""
    if str(probe.get("platform") or "") != "Windows":
        return None
    if probe.get("uia_exposed"):
        return UiaState(CONNECTED, instance=_instance(probe))
    if probe.get("login_window") and not probe.get("logged_in"):
        return UiaState(FAILED, NOT_LOGGED_IN)
    if probe.get("logged_in"):
        # 主窗口在，但控件树没暴露出来——这台微信我们驱动不了，和「没开」是两回事。
        return UiaState(FAILED, WINDOW_UNAVAILABLE)
    if int(probe.get("window_count") or 0) > 0:
        return UiaState(FAILED, WINDOW_UNAVAILABLE)
    return UiaState(FAILED, PROCESS_NOT_FOUND)


def probe_failed(error: BaseException) -> UiaState:
    """探测本身炸了（import 失败、COM 出错）。

    ★ 不硬套一个具体失败码：探不动的原因我们并不知道，说成「微信没开」会把人支去
    重开微信，而真相可能是这台机器上 UIA 组件坏了。用「窗口不可用」+ 让人重试是
    表里最接近事实的一条。
    """
    return UiaState(FAILED, WINDOW_UNAVAILABLE)


def transition(previous: UiaState | None, current: UiaState) -> UiaState:
    """连过又断了，报的是 ``disconnected`` 而不是又一次 ``failed``（契约 §8 枚举）。

    这两个在企业端是两句不同的话：`failed` 是「一直没连上」，`disconnected` 是
    「刚才还好好的，现在掉了」——后者往往意味着微信被人手工关了或者崩了。
    """
    if previous is not None and previous.status == CONNECTED and current.status == FAILED:
        return UiaState(DISCONNECTED, CONNECTION_LOST)
    return current


def _instance(probe: dict[str, Any]) -> dict[str, Any] | None:
    """连上时能如实报的那点信息。

    ★ 只给 `processVersion`。契约里的 `accountRef`（用来判「一个微信被两个分身占用」）
    和 `nickname` 需要读到微信账号本身，而只读探测拿不到——客户端目前没有「读我自己的
    昵称/wxid」这个能力，编一个「稳定标识」出来只会让服务端的占用检查形同虚设。
    真要这条约束生效，得先在 `wechat_api` 里加一次性读取自身账号的能力。
    """
    selected = probe.get("selected_window")
    version = ""
    if isinstance(selected, dict):
        version = str(selected.get("process_version") or "")
    return {"processVersion": version} if version else None
