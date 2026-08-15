"""这台机器的身份：设备 ID、设备名、系统版本。

契约 §5.1 对 `deviceId` 只有两条硬要求，两条都是为了同一件事——设备标识不该变成一个
可以跨系统追踪这台电脑的指纹：

* 20–128 位 URL-safe 随机串，**首次安装时生成**；
* **不得**用 MAC、硬盘序列号等硬件隐私标识。

所以它就是一串随机数，生成一次落盘，之后一直用。删了数据目录就是一台新设备——
服务端会把它当成换了机器（旧会话仍在，需要用户确认替换），这正是我们要的语义。
"""

from __future__ import annotations

import json
import platform
import secrets
from dataclasses import dataclass
from pathlib import Path

FILE_NAME = "device.json"

#: 32 字节 → base64url 43 位，落在契约要求的 20–128 之间。
_ID_BYTES = 32


@dataclass(frozen=True)
class Device:
    device_id: str
    device_name: str
    os_version: str = ""


def os_version() -> str:
    """例：``Windows 11 10.0.22631``。拿不到就给空串——这个字段本来就是可选的。"""
    try:
        system = platform.system()
        release = platform.release()
        version = platform.version()
        return " ".join(part for part in (system, release, version) if part)[:120]
    except Exception:  # noqa: BLE001 - 取不到系统版本不该拦住登录
        return ""


def default_name() -> str:
    """设备名只用来让人认出「这是我办公室那台」，所以取计算机名。"""
    name = ""
    try:
        name = platform.node().strip()
    except Exception:  # noqa: BLE001
        name = ""
    return (name or "未命名电脑")[:120]


def load(data_dir: Path) -> Device:
    """读这台机器的身份，没有就当场生成一个并落盘。

    ★ 生成失败（磁盘满、目录只读）也要返回一个**本次进程内可用**的 ID，不能让登录直接
    失败：代价只是下次重启又算一台新设备，比「今天登不上」轻得多。
    """
    path = Path(data_dir) / FILE_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        device_id = str(raw.get("device_id") or "")
        device_name = str(raw.get("device_name") or "") or default_name()
    except Exception:  # noqa: BLE001 - 没有/坏了都当成第一次
        device_id, device_name = "", default_name()

    if not (20 <= len(device_id) <= 128):
        device_id = secrets.token_urlsafe(_ID_BYTES)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"device_id": device_id, "device_name": device_name},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return Device(device_id=device_id, device_name=device_name, os_version=os_version())


def rename(data_dir: Path, device_name: str) -> Device:
    """改设备名（企业端的会话列表里显示的就是它）。"""
    current = load(data_dir)
    path = Path(data_dir) / FILE_NAME
    name = (device_name.strip() or current.device_name)[:120]
    try:
        path.write_text(
            json.dumps({"device_id": current.device_id, "device_name": name},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return Device(device_id=current.device_id, device_name=name,
                  os_version=current.os_version)
