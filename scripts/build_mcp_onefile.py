from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from build_gui_onefile import (
    _find_logger_conf,
    _omni_visual_vendor_data,
    _vendor_package_data,
    _vendor_package_modules,
)


def _build_args(repo_root: Path, name: str) -> list[str]:
    launcher = repo_root / "scripts" / "wxclient_mcp.py"
    vendor_dir = repo_root / "third_party" / "pywechat"
    # MCP targets WeChat 4.1+ through pyweixin. Do not bundle the legacy
    # pywechat package, whose old process probe still depends on WMI.
    vendor_packages = ("pyweixin",)
    assets_dir = repo_root / "wxclient" / "mcp_server" / "assets"
    build_dir = repo_root / "build" / "pyinstaller-mcp"
    dist_dir = repo_root / "dist"

    args = [
        str(launcher),
        "--onefile",
        "--clean",
        "--noconfirm",
        "--console",
        "--name",
        name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
        "--paths",
        str(repo_root),
        "--paths",
        str(vendor_dir),
        "--collect-all",
        "mcp",
        "--hidden-import",
        "pyautogui",
        "--hidden-import",
        "pywinauto",
        "--hidden-import",
        "win32api",
        "--hidden-import",
        "win32clipboard",
        "--hidden-import",
        "win32con",
        "--hidden-import",
        "win32gui",
        "--hidden-import",
        "pythoncom",
        "--hidden-import",
        "pywintypes",
        "--exclude-module",
        "PySide6",
        "--exclude-module",
        "PyQt5",
        "--exclude-module",
        "PyQt6",
        "--exclude-module",
        "torch",
        "--exclude-module",
        "torchvision",
        "--exclude-module",
        "scipy",
        "--exclude-module",
        "sklearn",
        "--exclude-module",
        "matplotlib",
        "--exclude-module",
        "pandas",
        "--exclude-module",
        "cv2",
    ]
    if assets_dir.exists():
        args.extend(
            ["--add-data", f"{assets_dir}{os.pathsep}wxclient/mcp_server/assets"]
        )
    args.extend(_vendor_package_data(vendor_dir, vendor_packages))
    args.extend(_omni_visual_vendor_data(repo_root))
    for module_name in _vendor_package_modules(vendor_dir, vendor_packages):
        args.extend(["--hidden-import", module_name])
    logger_conf = _find_logger_conf()
    if logger_conf is not None:
        args.extend(["--add-data", f"{logger_conf}{os.pathsep}logger"])
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description="Build wxclient MCP as a one-file EXE")
    parser.add_argument("--name", default="wxclient-mcp", help="Output executable name")
    parser.add_argument("--dry-run", action="store_true", help="Print the PyInstaller command")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    build_args = _build_args(repo_root, args.name)
    if args.dry_run:
        print("pyinstaller " + " ".join(shlex.quote(item) for item in build_args))
        return 0

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        print(
            "PyInstaller is not installed. Install it with `pip install -e \".[build,mcp]\"`.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    PyInstaller.__main__.run(build_args)
    output = repo_root / "dist" / f"{args.name}.exe"
    if output.exists():
        print(f"Built: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
