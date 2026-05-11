from __future__ import annotations

import argparse
from pathlib import Path

from wxclient.ui.app import run_gui


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="wxclient GUI launcher")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to the GUI JSON config file",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    return run_gui(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
