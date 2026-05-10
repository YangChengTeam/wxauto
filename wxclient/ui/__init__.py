from __future__ import annotations

from pathlib import Path


def run_gui(config: Path | None = None) -> int:
    from .app import run_gui as _run_gui

    return _run_gui(config)

__all__ = ["run_gui"]
