"""这台机器上登录的是哪个微信账号——**不碰界面**地问出来。

契约 §8 要求 `connected` 时给出 `wechatInstance.accountRef`：一个**稳定不透明**的账号标识，
服务端拿它检查「同一个微信有没有被两个招聘分身同时占用」。所以它必须真的跟着微信账号走：
编一个随机数或者拿设备号顶替，那条占用检查就形同虚设。

怎么问出来：微信进程**打开着自己账号目录下的文件**（`xwechat_files\\<账号>\\…`），
枚举它的已打开文件就能读到目录名，而目录名里就是 wxid。这条路的好处是**一次界面动作都没有**
——不点头像、不开个人信息面板，不会在用户正用着微信的时候抢焦点，也和风控节拍无关。

★ 只给哈希和掩码，**原始 wxid 不出这个模块**：它上报出去没有必要（服务端只需要「同一个账号
认得出是同一个」），少一处 PII 就少一处泄露面。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import structlog

_log = structlog.get_logger(__name__)

#: 账号数据目录长这样：`…\xwechat_files\wxid_6shq4q0wu92m12_c174\…`
#: `all_users` 是公共目录，不是某个账号。
_ACCOUNT_DIR = re.compile(r"[\\/]xwechat_files[\\/]([^\\/]+)[\\/]", re.IGNORECASE)
_SHARED_DIRS = {"all_users", "applet", "cache"}

#: 目录名尾巴上的 `_c174` 是**这台电脑这次安装**的编号，不是账号的一部分。
#: 必须去掉：同一个微信在两台电脑上登录时后缀不同，留着的话 accountRef 会算出两个值，
#: 服务端那条「一个微信只能被一个分身占用」就永远不会触发。
_INSTALL_SUFFIX = re.compile(r"_c\d+$")

_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}

#: 哈希加一个固定前缀（不是每台机器一份盐）——同一个微信在哪台电脑上都要算出同一个值。
_REF_NAMESPACE = "lxz-wechat-account:v1:"


@dataclass(frozen=True)
class WeChatAccount:
    """当前登录的微信账号。``raw`` 只在本机日志里出现，不上报。"""

    raw: str

    @property
    def account_ref(self) -> str:
        """稳定不透明标识（契约 §8）。原文不可逆推。"""
        digest = hashlib.sha256((_REF_NAMESPACE + self.raw).encode("utf-8")).hexdigest()
        return digest[:32]

    @property
    def account_mask(self) -> str:
        """脱敏账号，形如 ``wxid_****m12``（契约 §8 的 `accountMask`）。"""
        if len(self.raw) <= 8:
            return self.raw[:2] + "****"
        return f"{self.raw[:5]}****{self.raw[-3:]}"


def probe_account() -> WeChatAccount | None:
    """当前登录的微信账号；问不出来就 ``None``（**不猜**）。

    问不出来的正常情形：微信没在跑、进程属于别的用户、系统不让枚举已打开文件。
    这时候宁可少给一个字段，也不要给一个编出来的标识。
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - 打包里一定有
        return None

    names: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        if (proc.info.get("name") or "").lower() not in _PROCESS_NAMES:
            continue
        try:
            for handle in proc.open_files():
                found = _ACCOUNT_DIR.search(handle.path)
                if found and found.group(1).lower() not in _SHARED_DIRS:
                    names.add(found.group(1))
        except Exception as exc:  # noqa: BLE001 - 权限/进程刚退出，都不该往上炸
            _log.debug("枚举微信打开的文件失败", error=str(exc)[:80])

    if not names:
        return None
    if len(names) > 1:
        # 一台机器上开了两个微信：分不清哪个是「这个分身的」，就别乱认一个。
        _log.warning("这台机器上有多个微信账号在跑，认不出该用哪个", count=len(names))
        return None
    return WeChatAccount(_INSTALL_SUFFIX.sub("", names.pop()))
