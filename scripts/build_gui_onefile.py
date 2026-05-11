from __future__ import annotations

import argparse
import importlib.util
import os
import shlex
import sys
from pathlib import Path


def _find_logger_conf() -> Path | None:
    spec = importlib.util.find_spec("logger")
    if spec is None or spec.origin is None:
        return None
    conf_path = Path(spec.origin).resolve().parent / "logging.conf"
    return conf_path if conf_path.exists() else None


def _build_args(repo_root: Path, name: str, console: bool) -> list[str]:
    launcher = repo_root / "scripts" / "wxclient_gui.py"
    assets_dir = repo_root / "wxclient" / "ui" / "assets"
    icon_file = assets_dir / "app-icon.ico"
    splash_file = assets_dir / "app-logo-256.png"
    logger_conf = _find_logger_conf()
    vendor_dir = repo_root / "third_party" / "pywechat"
    build_dir = repo_root / "build" / "pyinstaller"
    dist_dir = repo_root / "dist"
    add_data = f"{assets_dir}{os.pathsep}wxclient/ui/assets"

    args = [
        str(launcher),
        "--onefile",
        "--clean",
        "--noconfirm",
    ]
    args.extend(
        [
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
            "--add-data",
            add_data,
            "--icon",
            str(icon_file),
            "--splash",
            str(splash_file),
            "--collect-all",
            "PySide6",
            "--collect-submodules",
            "pyweixin",
            "--hidden-import",
            "pyautogui",
            "--hidden-import",
            "pywinauto",
            "--hidden-import",
            "win32api",
            "--hidden-import",
            "win32clipboard",
            "--hidden-import",
            "win32com.client",
            "--hidden-import",
            "win32con",
            "--hidden-import",
            "win32gui",
            "--hidden-import",
            "pythoncom",
            "--hidden-import",
            "pywintypes",
            "--exclude-module",
            "PyQt5",
            "--exclude-module",
            "PyQt6",
            "--exclude-module",
            "PySide2",
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
            "numba",
            "--exclude-module",
            "cv2",
            "--exclude-module",
            "av",
            "--exclude-module",
            "yt_dlp",
            "--exclude-module",
            "onnxruntime",
            "--exclude-module",
            "transformers",
            "--exclude-module",
            "librosa",
            "--exclude-module",
            "IPython",
            "--exclude-module",
            "jupyter",
            "--exclude-module",
            "notebook",
        ]
    )
    if logger_conf is not None:
        args.extend(["--add-data", f"{logger_conf}{os.pathsep}logger"])
    args.append("--console" if console else "--windowed")
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description="Build wxclient GUI as a one-file EXE")
    parser.add_argument("--name", default="wxclient-gui", help="Output executable name")
    parser.add_argument("--console", action="store_true", help="Keep a console window")
    parser.add_argument("--dry-run", action="store_true", help="Print the PyInstaller command")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    build_args = _build_args(repo_root=repo_root, name=args.name, console=args.console)

    if args.dry_run:
        command = "pyinstaller " + " ".join(shlex.quote(item) for item in build_args)
        print(command)
        return 0

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        print(
            "PyInstaller is not installed. Install it with `pip install -e \".[build]\"` "
            "or `pip install pyinstaller` first.",
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
