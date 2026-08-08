from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import uuid4


_MOMENTS_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".mp4"}
_ALLOWED_SUFFIXES = _MOMENTS_SUFFIXES | {
    ".pdf", ".txt", ".md", ".csv", ".json", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".7z",
}
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class MediaStore:
    """Stores MCP uploads without exposing arbitrary Windows filesystem paths."""

    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def save_base64(self, filename: str, content_base64: str) -> dict[str, Any]:
        suffix = Path(filename).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise ValueError(f"Unsupported upload type: {suffix or '(missing extension)'}")
        encoded = content_base64.strip()
        if encoded.startswith("data:"):
            try:
                encoded = encoded.split(",", 1)[1]
            except IndexError as exc:
                raise ValueError("Invalid data URL") from exc
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Invalid base64 upload content") from exc
        if not content:
            raise ValueError("Uploaded file is empty")
        if len(content) > self.max_bytes:
            raise ValueError(f"Uploaded file exceeds {self.max_bytes} bytes")

        media_id = uuid4().hex
        path = self.root / f"{media_id}{suffix}"
        path.write_bytes(content)
        return {
            "media_id": media_id,
            "filename": Path(filename).name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def resolve(self, media_id: str) -> Path:
        if not _MEDIA_ID_RE.fullmatch(media_id):
            raise ValueError("Invalid media_id")
        matches = list(self.root.glob(f"{media_id}.*"))
        if len(matches) != 1 or matches[0].suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ValueError(f"Unknown media_id: {media_id}")
        return matches[0].resolve()

    def resolve_many(self, media_ids: list[str]) -> list[str]:
        return [str(self.resolve(media_id)) for media_id in media_ids]

    def resolve_many_for_moments(self, media_ids: list[str]) -> list[str]:
        paths = [self.resolve(media_id) for media_id in media_ids]
        unsupported = [path.name for path in paths if path.suffix.lower() not in _MOMENTS_SUFFIXES]
        if unsupported:
            raise ValueError(f"Moments only supports image/video uploads: {unsupported}")
        return [str(path) for path in paths]
