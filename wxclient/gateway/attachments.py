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
- **拿不到就如实说**：另存失败（气泡过期、菜单没有「另存为」、对话框没弹、文件没落下）只在正文里
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

#: 「没能带上」的原因码 → 给日志页看的话。原因码随 ``inline_attachments`` 返回，话在这里配，
#: 日志渲染器只管照抄——同一个原因码两处各写一句话，迟早写岔。
LEFT_BEHIND_TEXT = {
    "missing": "文件已不在本机（过了保鲜期或被清掉）",
    "too_many": f"超出一轮最多 {MAX_ATTACHMENTS_PER_TURN} 份的上限",
    "over_budget": f"超出一轮 {MAX_TOTAL_BYTES // (1024 * 1024)}MB 的总量上限",
}
#: AI 端认领材料时的类型码（与 AI 端 `wechat_reply_card.json` 的 `documents[].material_type` 同口径）→ 给日志页看的话。
MATERIAL_TYPE_TEXT = {
    "resume": "简历",
    "id_card_front": "身份证正面",
    "id_card_back": "身份证背面",
    "bank_card": "银行卡",
    "certificate": "证书",
    "health_certificate": "健康证",
    "physical_exam_report": "体检报告",
}


#: 附件从发现到送达 AI 端的进度日志（像 AI 端阶段那样一步一行）：
#: 1/4 发现 → 2/4 另存 → 3/4 上传 → 4/4 已送达（file_id 或随消息内联）。失败带 ``失败=True`` 与原因。
PROGRESS_EVENT = "附件进度"


def _kb(size: int) -> str:
    return f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f} MB"


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
        label = str(raw.get("file_name") or "") or ("一张图片" if kind == "image" else "一个文件")
        target = self.dir_for(account_id, peer_id)
        target.mkdir(parents=True, exist_ok=True)
        # 先看自己的目录里是不是已经存过这份（同名就算，上一轮存过、这条被重读、上次上传没成）：有就直接用。
        # 另存到同名文件上微信会弹「是否替换」，而另存那边按「目录里多出一个文件」判成败——覆盖不多出文件，
        # 重存反而会失败。只看我们自己的目录，不碰微信的存储目录（权限问题，用户 2026-09-29）。
        existing = self._already_saved(target, raw)
        if existing is not None:
            _log.info(PROGRESS_EVENT, 会话=peer_id, 文件=existing.name, 步骤="1/4",
                      说明="发现附件，本机目录里已经存过这份，不再另存")
            return self._accept(peer_id, kind, existing, existing.name, existing.stat().st_size, "本机已有")
        _log.info(PROGRESS_EVENT, 会话=peer_id, 文件=label, 步骤="1/4", 说明="发现附件，本机没有，开始另存到本机")
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
                PROGRESS_EVENT, 会话=peer_id, 文件=label, 步骤="2/4", 失败=True,
                说明="另存失败，这份不会送到 AI 端（对方会被请求再发一次）",
                原因=str((result or {}).get("last_error") or status)[:160],
            )
            return None
        body = (result or {}).get("result") or {}
        path = Path(str(body.get("path") or ""))
        if not path.is_file():
            _log.warning(PROGRESS_EVENT, 会话=peer_id, 文件=label, 步骤="2/4", 失败=True,
                         说明="另存任务报成功，但本机目录里找不到文件")
            return None
        size = int(body.get("bytes") or path.stat().st_size)
        return self._accept(peer_id, kind, path, str(body.get("file_name") or path.name), size, "已另存到本机")

    @staticmethod
    def _already_saved(target: Path, raw: dict[str, Any]) -> Path | None:
        """自己目录里同名的那份；没有回 None。名字相同就算同一份（用户 2026-09-29）。

        只认文件气泡：图片气泡没有名字（另存默认名是「微信图片_时间戳」），认不出是不是同一张——
        图片同名的情况由另存那一步在对话框里按默认名再认一次（`uia.existing_same_name`）。
        """
        if str(raw.get("message_type") or "") != "file":
            return None
        name = str(raw.get("file_name") or "").strip()
        if not name or Path(name).name != name:
            return None
        path = target / name
        try:
            return path if path.is_file() and path.stat().st_size > 0 else None
        except OSError:
            return None

    def _accept(self, peer_id: str, kind: str, path: Path, file_name: str, size: int, how: str) -> dict[str, Any] | None:
        """本机这份能不能送 AI 端（类型、体积），能就回附件描述；不能就删掉并记日志。"""
        if not allowed_file(file_name):
            _log.info(PROGRESS_EVENT, 会话=peer_id, 文件=file_name, 步骤="2/4", 失败=True,
                      说明=f"{how}，但类型 {Path(file_name).suffix.lower() or '（无扩展名）'} 不在名单里，不送 AI 端")
            path.unlink(missing_ok=True)
            return None
        if size > MAX_ATTACHMENT_BYTES:
            _log.info(PROGRESS_EVENT, 会话=peer_id, 文件=file_name, 步骤="2/4", 失败=True,
                      说明=f"{how}，但 {_kb(size)} 超过单份 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB 上限，不送 AI 端")
            path.unlink(missing_ok=True)
            return None
        _log.info(PROGRESS_EVENT, 会话=peer_id, 文件=file_name, 步骤="2/4", 说明=f"{how}（{_kb(size)}）")
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


def collect_attachments(messages: Sequence[Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """这一批消息带的附件里，这一轮送哪几份：返回 (要送的, 没能带上的清单)。

    要送的每项 ``{"file_name", "content_type", "kind", "data"}``（``data`` 是字节）。两种送法共用这一份挑选：
    直接传到 AI 端（`agent_files.AgentFileUploader`，2026-09-28 起的主路）和随 WS 内联（兜底，见
    `inline_attachments`）——个数和总量上限只在这里判一次，两条路看到的是同一批。

    清单每条 ``{"file_name", "reason"}``，``reason`` 是 ``LEFT_BEHIND_TEXT`` 的键——超出个数或总量上限的
    **不送**，交给调用方在正文里如实说；文件已经不在了（过保鲜期 / 被清）也算没带上。原来只回一个数，
    日志页上只见「有 2 份没带上」却说不出是哪两份、为什么；对方追问「我发的身份证你收到没」时这台电脑答不上来。
    """
    out: list[dict[str, Any]] = []
    left_behind: list[dict[str, str]] = []
    total = 0
    for message in messages:
        for entry in getattr(message, "attachments", ()) or ():
            path = Path(str(entry.get("path") or ""))
            file_name = str(entry.get("file_name") or path.name)
            if not path.is_file():
                left_behind.append({"file_name": file_name, "reason": "missing"})
                continue
            data = path.read_bytes()
            if len(out) >= MAX_ATTACHMENTS_PER_TURN:
                left_behind.append({"file_name": file_name, "reason": "too_many"})
                continue
            if total + len(data) > MAX_TOTAL_BYTES:
                left_behind.append({"file_name": file_name, "reason": "over_budget"})
                continue
            total += len(data)
            out.append(
                {
                    "file_name": file_name,
                    "content_type": guess_content_type(file_name),
                    "kind": str(entry.get("kind") or "file"),
                    "data": data,
                }
            )
    return out, left_behind


def to_inline(files: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """`collect_attachments` 挑出来的那批 → AI 端 ``message.send.attachments`` 的形状（兜底路）。"""
    return [
        {
            "file_name": f["file_name"],
            "content_type": f["content_type"],
            "kind": f["kind"],
            "data_base64": base64.b64encode(f["data"]).decode("ascii"),
        }
        for f in files
    ]


def inline_attachments(messages: Sequence[Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """把这一批消息带的附件读成 AI 端 ``message.send.attachments`` 的形状。挑选口径见 `collect_attachments`。"""
    files, left_behind = collect_attachments(messages)
    return to_inline(files), left_behind
