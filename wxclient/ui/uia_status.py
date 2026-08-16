"""把只读 UIA 探测（`uia_probe`）的结果翻成契约 §8 的微信连接状态。

分成纯函数放这儿，是因为**这层判断没法在真机以外复现，但它决定企业端显示什么**：
「微信没开」和「微信开了但没登录」要给两句不同的话（一句是「打开微信」，一句是
「扫码登录」），报错了方向人就会去做无用功。纯函数至少能把映射表本身测死。

★ 一条克制：**拿不准就不报**。非 Windows（开发机）直接返回 None——在 Linux 上报一个
「微信没找到」，等于往企业端灌一条这台机器根本不存在的事实。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
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

#: 读不到的字段填的占位符（用户裁决 2026-08-15）。服务端把 wechatInstance 里的字段
#: 都设成了必填，给一个**一眼看得出是「没读到」**的值，好过编一个像真的假值。
UNKNOWN_NICKNAME = "-"

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
        return replace(self, capability_status=status)

    @property
    def identity(self) -> tuple[str, str, str]:
        """「算不算同一件事」的那几个字段。**只此一份**。

        两个地方要用它：去重（同一件事不重复上报）和幂等键（同一件事重试沿用同一个
        eventId）。分开写两份的话，哪天多一个身份字段而只改了一处，两边就会对同一次
        变化给出不同答案——去重说「变了」、幂等键说「没变」，服务端拿 409 顶回来。
        ``instance`` 不在其中：微信版本号变了不值得再报一次。
        """
        return (self.status, self.failure_code, self.capability_status)

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
        return other is not None and self.identity == other.identity


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

    `accountRef` / `accountMask` 由 `uia_probe.account` 从微信进程打开的账号目录算出来
    （不碰界面，见那个模块）。

    `nickname` 一律是占位符 ``-``（用户裁决 2026-08-15）：昵称只存在微信自己的加密库里，
    唯一的读法是点进「个人信息」面板去驱动界面——那会在用户正用着微信时抢焦点，而 vendor
    的那条路在微信 4.x 上还是坏的。服务端把这个字段设成了必填，所以给一个**一眼看得出是
    「没读到」的占位符**，而不是拿分身名之类的东西冒充微信昵称——那会让企业端显示一个
    看着像真的、其实是假的微信身份。哪天真能读到昵称了，把这里换掉即可。
    """
    account = _account(_wechat_pids(probe))
    if account is None:
        # 认不出账号时这条 connected 本来就会被服务端退回（accountRef 必填），
        # 单给一个占位昵称没有意义——干脆不带 wechatInstance。
        return None
    selected = probe.get("selected_window")
    version = str(selected.get("process_version") or "") if isinstance(selected, dict) else ""
    # 服务端要求这四个字段都在（连 processVersion 也是必填），少一个整条 connected
    # 就被退回、企业端永远进不了「运行中」。读不到的填 `-`，和昵称同一个约定：
    # 一眼看得出是「没读到」，而不是一个编出来的版本号。
    return {
        "processVersion": version or UNKNOWN_NICKNAME,
        "accountRef": account.account_ref,
        "accountMask": account.account_mask,
        "nickname": UNKNOWN_NICKNAME,
    }


def _wechat_pids(probe: dict[str, Any]) -> tuple[int, ...]:
    """这一拍看到的微信进程号。**它就是账号缓存的键**。

    ★ 原来缓存是个「查一次就再也不查」的模块全局：微信退出登录、换个号再登进来，
    进程换了，我们却还在报上一个账号的 accountRef——而服务端那条「一个微信只能被一个
    分身占用」正是按它判的。按进程号做键，换号自然重认一次，不用谁记得去清缓存。
    """
    pids: set[int] = set()
    windows = probe.get("windows")
    for window in windows if isinstance(windows, list) else []:
        if isinstance(window, dict):
            pids.add(int(window.get("process_id") or 0))
    selected = probe.get("selected_window")
    if isinstance(selected, dict):
        pids.add(int(selected.get("process_id") or 0))
    return tuple(sorted(pid for pid in pids if pid))


@lru_cache(maxsize=1)
def _account(_pids: tuple[int, ...]) -> Any:
    """当前微信账号。**同一批进程只认一次**——枚举进程打开的文件在真机上要 2 秒多，
    15 秒一拍的探测扛不住每拍都查。参数只当缓存键用，故意不读。"""
    from wxclient.uia_probe.account import probe_account

    return probe_account()


def forget_account() -> None:
    """丢掉账号缓存（测试用；生产靠进程号变化自然失效）。"""
    _account.cache_clear()
