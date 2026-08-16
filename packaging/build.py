"""一条命令打出安装包（**只能在 Windows 上跑**）。

    python packaging/build.py              # 生成图 → PyInstaller → Inno Setup
    python packaging/build.py --skip-exe    # 只重打安装包（onedir 已经在了，改 iss 时用）

产物：``dist\\劳小智微信助手_v<版本>_安装包.exe``

四步是有顺序的，别调换：

1. **两张图先生成**（启动首屏 + 安装向导竖幅）。它们是按 `theme.py` 画的，
   改了配色不重跑就会出现「安装向导一套色、程序里另一套色」。
2. **PyInstaller 打 onedir**。为什么不是 onefile：见 lxz-wechat-gateway.spec 顶上的注释。
3. **Inno Setup 把 onedir 包起来**。安装包做的事只有解压 + 建快捷方式 + 写卸载信息，
   不做任何「装的时候顺手配一下」——配置属于程序自己，不属于安装程序。
4. **验包**：先跑打出来的 exe 的 `--self-check`（真 import 一遍重活、真探一次微信），
   再看安装包在不在、多大、版本号对不对。

★ 第 4 步里的 `--self-check` 不是走过场。冻结包缺一个模块或一个数据文件，源码跑、单测跑、
甚至在打包机上双击 onedir 里的 exe 都可能不报错——只有装到别人机器上真去驱动微信那一刻
才炸。2026-08-16 一天之内撞了两次（`logger/logging.conf` 没收进去、`win32ui` 被 excludes
砍掉了），两次的表现都是「微信明明开着，界面说找不到微信」。所以这一步失败 = 不出包。

版本号唯一来源是 `wxclient/__init__.py` 的 ``__version__``，不在这里也不在 iss 里另写一份。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGING = REPO / "packaging"
DIST = REPO / "dist"
SPEC = PACKAGING / "lxz-wechat-gateway.spec"
ISS = PACKAGING / "installer.iss"
APP_NAME = "劳小智微信助手"

#: Inno Setup 装在哪。**从环境变量推，别写死 C 盘**——系统盘不一定是 C
#: （这台打包机的 SSH 会话里就是 Z:），写死的表现是「明明装了却说找不到」。
PROGRAM_FILES_VARS = ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432")


def app_version() -> str:
    """从 `wxclient/__init__.py` 读版本号。**单一来源**，别在别处再写一份。"""
    text = (REPO / "wxclient" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:
        raise SystemExit("wxclient/__init__.py 里找不到 __version__")
    return match.group(1)


def find_iscc() -> Path:
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return Path(found)
    for var in PROGRAM_FILES_VARS:
        base = os.environ.get(var)
        if not base:
            continue
        candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
        if candidate.exists():
            return candidate
    raise SystemExit(
        "找不到 Inno Setup 的命令行编译器 ISCC.exe。\n"
        "去 https://jrsoftware.org/isdl.php 装 Inno Setup 6，或把它加进 PATH。"
    )


def self_check(exe: Path) -> None:
    """跑一次打出来的 exe 的 `--self-check`：真 import 重活、真探一次微信。

    只看退出码和它写下的那份报告——`console=False` 的包 stdout 是没人收的，
    所以结果落在文件里。探测**结果**是什么不重要（打包机上可能没开微信），
    重要的是它没抛异常。
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "self-check.txt"
        print(f"\n$ {exe} --self-check\n", flush=True)
        result = subprocess.run([str(exe), "--self-check", str(report)], timeout=180)
        text = report.read_text(encoding="utf-8") if report.exists() else "(没写出报告)"
        print(text, flush=True)
        if result.returncode != 0:
            raise SystemExit(
                "验包没过：打出来的包跑不起微信探测。\n"
                "多半是 spec 的 excludes 砍掉了依赖自己要用的东西，"
                "或者某个数据文件（.conf/.json 之类）没被收进去。"
            )


def run(argv: list[str], *, cwd: Path) -> None:
    print(f"\n$ {' '.join(str(a) for a in argv)}\n", flush=True)
    result = subprocess.run(argv, cwd=cwd)
    if result.returncode != 0:
        raise SystemExit(f"这一步失败了（退出码 {result.returncode}），后面的不做了")


def main() -> int:
    parser = argparse.ArgumentParser(description="打 Windows 安装包")
    parser.add_argument("--skip-exe", action="store_true",
                        help="跳过 PyInstaller，直接拿现成的 onedir 打安装包")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("安装包只能在 Windows 上打（PyInstaller 收的是本机的 DLL）")

    version = app_version()
    onedir = DIST / APP_NAME
    print(f"== {APP_NAME} {version} ==")

    # ① 图
    run([sys.executable, str(PACKAGING / "make_splash.py")], cwd=REPO)
    run([sys.executable, str(PACKAGING / "make_wizard_images.py")], cwd=REPO)

    # ② onedir
    if args.skip_exe:
        if not (onedir / f"{APP_NAME}.exe").exists():
            raise SystemExit(f"--skip-exe 要求 {onedir} 已经打好了，但那儿没有 exe")
        print(f"\n[跳过] PyInstaller，沿用 {onedir}")
    else:
        run([sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC)], cwd=REPO)
        if not (onedir / f"{APP_NAME}.exe").exists():
            raise SystemExit(f"PyInstaller 跑完了，但 {onedir} 下没有 exe")

    # ③ 验包：这一步失败就别往下打了，打出来也是个不能用的包
    self_check(onedir / f"{APP_NAME}.exe")

    # ④ 安装包
    run([str(find_iscc()),
         f"/DAppVersion={version}",
         f"/DSourceDir={onedir}",
         f"/DOutputDir={DIST}",
         str(ISS)], cwd=PACKAGING)

    # ⑤ 收尾
    setup = DIST / f"{APP_NAME}_v{version}_安装包.exe"
    if not setup.exists():
        raise SystemExit(f"ISCC 说成功了，但没找到 {setup}——检查 iss 里的 OutputBaseFilename")
    size_mb = setup.stat().st_size / 1024 / 1024
    # ★ 只用中文和 ASCII，别用 emoji：Windows 控制台默认是 GBK，一个 ✅ 就能让整条
    # 打包在**最后一行**炸掉（UnicodeEncodeError），而包其实早就打好了——白折腾一轮。
    print(f"\n[完成] {setup}")
    print(f"       {size_mb:.1f} MB / 版本 {version}")
    if size_mb < 20:
        print("       [警告] 这个体积不对劲（正常一百多 MB），多半是 onedir 没打全就被包进去了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
