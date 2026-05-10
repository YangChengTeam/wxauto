from __future__ import annotations

from typing import Protocol


class ChatAgent(Protocol):
    def reply(self, message: str, context: list[str] | None = None) -> str:
        """Return a chat reply for one inbound message."""
