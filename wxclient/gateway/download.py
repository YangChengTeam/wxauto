"""把 AI 端随指令送来的产出物（招聘海报图等）落到本机，再交给微信发出去（067）。

为什么是「随指令送字节」而不是「给个地址自己下载」（2026-08-13 真机走过一遍才定的）：
本网关的票是 ``scope=wechat`` 的**受限票**，按 AI 端的归属鉴权它连那条会话都进不去（403）。
要让它能下载，就得把作用域边界撬开——那等于「这台 Windows 上的票一旦泄露，
该员工所有会话里的文件（简历 PII！）都能被拉走」。而推指令的那条 WS 本来就是这位员工
已鉴权的连接、文件归属 AI 端刚核对过，直接把字节递过来**不新增任何权限**，
也顺带没了「地址拼错拿回一张 index.html 当海报发出去」那类坑。

这里守的线：
- **文件名只当名字用**：洗掉路径分隔符与上跳，永远落在自己的临时目录里。
- **限体积**：一条指令的总字节有上限，坏指令撑不爆磁盘。
- **用完就删**：产出物不在这台电脑上留底。
"""

from __future__ import annotations

import base64
import binascii
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: 一条指令随行的字节总量上限（与 AI 端 MAX_INLINE_BYTES 同口径）。
MAX_TOTAL_BYTES = 8 * 1024 * 1024

#: 文件名里不许出现的东西——它要用来在本机建文件，绝不能带路径。
_UNSAFE_NAME = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


class DownloadError(RuntimeError):
    """文件没能落地。回执里要说人话，所以消息直接面向用户。"""


def safe_name(raw: str, fallback: str) -> str:
    name = _UNSAFE_NAME.sub("_", str(raw or "").strip()).strip(". ")
    return name[:120] if name else fallback


@dataclass(frozen=True)
class FileRef:
    """AI 端随指令下发的一个文件：ID、原名、字节。**没有路径**（路径是本机才有的东西）。"""

    file_id: str
    file_name: str
    content_b64: str

    @staticmethod
    def parse(raw: Any) -> FileRef | None:
        if not isinstance(raw, dict):
            return None
        content = str(raw.get("content_b64") or "")
        if not content:
            return None
        file_id = str(raw.get("file_id") or "").strip()
        return FileRef(file_id, safe_name(raw.get("file_name"), f"{file_id or 'file'}.bin"), content)


class FileFetcher:
    """把随指令来的字节写进一次性临时目录，交出本机路径。"""

    def __init__(self, *, max_total_bytes: int = MAX_TOTAL_BYTES) -> None:
        self._max = max_total_bytes

    @asynccontextmanager
    async def materialize(self, account_id: str, refs: list[FileRef]):
        """落盘这批文件；退出时**连临时目录一起删**。"""
        if not refs:
            raise DownloadError("没有可发送的文件")
        total = 0
        with TemporaryDirectory(prefix="lxz_send_") as tmp:
            dest = Path(tmp)
            paths: list[Path] = []
            for index, ref in enumerate(refs):
                try:
                    content = base64.b64decode(ref.content_b64, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise DownloadError(f"文件「{ref.file_name}」内容损坏，没有发送") from exc
                if not content:
                    raise DownloadError(f"文件「{ref.file_name}」是空的，微信发不了空文件")
                total += len(content)
                if total > self._max:
                    raise DownloadError(f"这批文件超过 {self._max // (1024 * 1024)}MB 上限，没有发送")
                # 同名文件各自占一个子目录，免得后一个把前一个覆盖掉。
                folder = dest / str(index)
                folder.mkdir()
                path = folder / ref.file_name
                path.write_bytes(content)
                paths.append(path)
                _log.info("已落地待发送文件", file_id=ref.file_id, name=ref.file_name, bytes=len(content))
            yield paths
