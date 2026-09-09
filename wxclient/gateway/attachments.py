"""对方发来的图片 / 文件：从微信另存到本机，再随对话轮送给 AI 端（104）。

链路：``read_chat_history`` 读到一条图片/文件气泡（`uia._annotate_attachment_row` 标的
``message_type``）→ 这里投一条 ``SAVE_MESSAGE_ATTACHMENT`` 任务把它另存到本机目录 →
``runner`` 起轮时把字节 base64 随 ``message.send.attachments`` 送到 AI 端 → AI 端落到会话
inbox 当作上传文件处理（简历解析 / 证件识别）。

守的线：
- **只认白名单扩展名**：微信给的默认文件名带扩展名，不在名单里的（exe、apk…）另存了也不送。
- **限体积**：单个和一轮总量都有上限（和 AI 端 ``MAX_INLINE_BYTES`` 同口径 8MB）。
- **不久留**：另存下来的是候选人的身份证、简历——真实 PII。过了保鲜期整目录清掉；
  这台电脑不是档案库，档案在业务后端。
- **拿不到就如实说**：另存失败（气泡过期、菜单没有「另存为」、对话框没弹）只在正文里
  告诉模型「对方发了一张图但没能保存」，不重试、不猜。
"""

from __future__ import annotations

import base64
import mimetypes
import shutil
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import structlog

from wxclient.queue import TaskType

from .download import safe_name

_log = structlog.get_logger(__name__)

#: 送给 AI 端的扩展名白名单。与 AI 端 `src/ws/turnAttachments.ts` 的名单同口径——
#: 那边不认的这边送过去也只是白占带宽。
ALLOWED_EXTENSIONS = frozenset(
    {
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt", ".csv", ".zip",
        ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic",
    }
)
#: 单个附件上限。身份证照片压缩后几百 KB，简历 PDF 通常 1–3MB；超过多半是原图视频。
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
#: 一轮随行总量（AI 端 `MAX_INLINE_BYTES` 同口径）。
MAX_TOTAL_BYTES = 8 * 1024 * 1024
#: 一轮最多送几个。对方连发十张图时后面的先不送，正文里如实说「还有 N 个没附上」。
MAX_ATTACHMENTS_PER_TURN = 6
#: 另存下来的文件在本机留多久。
RETENTION_HOURS = 24

#: 会被拿去另存的气泡类型。表情 / 视频不在内：表情不是内容，视频 AI 端也不看。
SAVABLE_TYPES = frozenset({"image", "file"})


def guess_content_type(file_name: str) -> str:
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or "application/octet-stream"


def allowed_file(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in ALLOWED_EXTENSIONS


class AttachmentFetcher:
    """把一条图片/文件气泡另存到本机。所有微信动作仍走 ``queue -> wechat_api``，这里只 submit。"""

    def __init__(self, *, runtime: Any, root: Path, clock=None) -> None:
        self._runtime = runtime
        self._root = Path(root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_purge: datetime | None = None

    @property
    def root(self) -> Path:
        return self._root

    def dir_for(self, account_id: str, peer_id: str) -> Path:
        """一个会话一个目录。会话名是微信昵称，可能带任何字符——只拿它的哈希当目录名。"""
        digest = sha256(f"{account_id}\x00{peer_id}".encode()).hexdigest()[:16]
        return self._root / safe_name(account_id, "account") / digest

    async def fetch(self, account_id: str, peer_id: str, raw: dict[str, Any]) -> dict[str, Any] | None:
        """另存一条气泡。成功回 ``{"kind", "file_name", "path", "bytes"}``；失败回 None（已记日志）。"""
        kind = str(raw.get("message_type") or "")
        if kind not in SAVABLE_TYPES:
            return None
        self.purge_stale()
        target = self.dir_for(account_id, peer_id)
        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "friend": peer_id,
            "target_dir": str(target),
            "runtime_id": str(raw.get("runtime_id") or ""),
            "order": int(raw.get("order", -1) or -1),
            "text": str(raw.get("text") or ""),
        }
        result = await self._runtime.submit(TaskType.SAVE_MESSAGE_ATTACHMENT, payload)
        status = str((result or {}).get("status") or "")
        if status not in {"", "succeeded"}:
            _log.warning(
                "对方发来的附件没能另存下来",
                会话=peer_id,
                类型=kind,
                原因=str((result or {}).get("last_error") or "")[:120],
            )
            return None
        body = (result or {}).get("result") or {}
        path = Path(str(body.get("path") or ""))
        if not path.is_file():
            _log.warning("另存任务成功了但文件不在", 会话=peer_id, 类型=kind)
            return None
        size = int(body.get("bytes") or path.stat().st_size)
        file_name = str(body.get("file_name") or path.name)
        if not allowed_file(file_name):
            _log.info("附件类型不在名单里，不送 AI 端", 会话=peer_id, 扩展名=Path(file_name).suffix.lower())
            path.unlink(missing_ok=True)
            return None
        if size > MAX_ATTACHMENT_BYTES:
            _log.info("附件太大，不送 AI 端", 会话=peer_id, 字节=size)
            path.unlink(missing_ok=True)
            return None
        _log.info("对方发来的附件已另存", 会话=peer_id, 类型=kind, 文件=file_name, 字节=size)
        return {"kind": kind, "file_name": file_name, "path": str(path), "bytes": size}

    def purge_stale(self) -> int:
        """清掉过保鲜期的附件目录。每小时最多扫一次；返回删掉的文件数。"""
        now = self._clock()
        if self._last_purge is not None and now - self._last_purge < timedelta(hours=1):
            return 0
        self._last_purge = now
        if not self._root.exists():
            return 0
        cutoff = now.timestamp() - RETENTION_HOURS * 3600
        removed = 0
        for entry in self._root.rglob("*"):
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        # 空目录也顺手收掉，别越攒越多。
        for entry in sorted((p for p in self._root.rglob("*") if p.is_dir()), reverse=True):
            with _suppress_oserror():
                entry.rmdir()
        if removed:
            _log.info("清掉过期附件", 文件数=removed, 保留小时=RETENTION_HOURS)
        return removed

    def drop_all(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)


class _suppress_oserror:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def inline_attachments(messages: Sequence[Any]) -> tuple[list[dict[str, Any]], int]:
    """把这一批消息带的附件读成 AI 端 ``message.send.attachments`` 的形状。

    返回 (随行附件, 没能带上的个数)。超出个数或总量上限的**不送**，交给调用方在
    正文里如实说；文件已经不在了（过保鲜期 / 被清）也算没带上。
    """
    out: list[dict[str, Any]] = []
    skipped = 0
    total = 0
    for message in messages:
        for entry in getattr(message, "attachments", ()) or ():
            path = Path(str(entry.get("path") or ""))
            if not path.is_file():
                skipped += 1
                continue
            data = path.read_bytes()
            if len(out) >= MAX_ATTACHMENTS_PER_TURN or total + len(data) > MAX_TOTAL_BYTES:
                skipped += 1
                continue
            total += len(data)
            file_name = str(entry.get("file_name") or path.name)
            out.append(
                {
                    "file_name": file_name,
                    "content_type": guess_content_type(file_name),
                    "kind": str(entry.get("kind") or "file"),
                    "data_base64": base64.b64encode(data).decode("ascii"),
                }
            )
    return out, skipped


def attachment_bytes(messages: Sequence[Any], file_name: str) -> bytes | None:
    """按文件名找回这一批里某个附件的字节（卡片 ``documents[]`` 回指用）。"""
    for message in messages:
        for entry in getattr(message, "attachments", ()) or ():
            if str(entry.get("file_name") or "") == file_name:
                path = Path(str(entry.get("path") or ""))
                if path.is_file():
                    return path.read_bytes()
    return None
