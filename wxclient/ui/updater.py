"""自动更新：查有没有新版本、把安装包下回来、校验、交给安装程序。

形状照着 ChatGPT 桌面端：**不弹窗打扰**——后台定时去问一次产品站，有新版就在侧栏亮一行
「有新版本 vX，点击安装」；点了才下载，下完自动装，装完自动重新打开。

三件必须做对的事：

1. **来源只有一个**：产品站根下的 `latest.json`（由 `publish.sh` 在上架时和安装包一起写出去，
   同一份 SHA-256）。客户端不猜 URL、不解析网页，也不会去别的地方找包。
2. **装之前必须校验**：下回来的包按 `latest.json` 里的 SHA-256 与字节数逐一核过才交给
   安装程序——传输截断不会报错，只会让用户装到半个包（`publish.sh` 上传后核 hash 是同一个理由）。
   校验不过的文件当场删掉，不留一个下次会被误当成「已下载」的残件。
3. **先自己退干净，再起安装程序**。Inno 的 CloseApplications 会去关还开着的旧版，但它关窗口
   走的是 WM_CLOSE——而本程序登录态下点 × 的意思是「缩回托盘」（`window.close_requested`），
   等于关不掉，最后只能被强杀，网关线程正在发的那条消息就断在半路。所以安装程序只在
   `__main__.shutdown` 收完尾（offline 已上报、网关线程已收口）之后才起（`after_stop`）。

用标准库 `urllib` 而不是 `httpx`，和 `client_api` 同一个理由：界面进程为了启动快没拉 http 栈，
两个 GET 不值得为它付这份钱。

安全边界：只认 https；哪个来源给的清单就只从它给的 URL 下；不签名（我们没有代码签名证书），
所以信任链止于产品站本身——这和用户自己去产品站点「下载安装包」是同一档信任。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

import structlog

_log = structlog.get_logger(__name__)

#: 产品站发布时写出的版本清单（`packages/product-site/scripts/publish.sh`）。
DEFAULT_MANIFEST_URL = "https://worker.laoxiaozhi.cn/wxauto/latest.json"
#: 装不了（从源码跑）时让人自己去下的那一页。
SITE_URL = "https://worker.laoxiaozhi.cn/wxauto/"

#: 启动后先等一会儿再查：那几秒网关正在装配，抢带宽也抢主线程。
FIRST_CHECK_DELAY_SECONDS = 30
#: 之后多久查一次。客户端是常驻的，一天查四次足够——发版本身就是一天最多一次的事。
CHECK_INTERVAL_SECONDS = 6 * 3600
#: 查一次失败后隔多久再试（网络抖一下不该等六小时）。
RETRY_INTERVAL_SECONDS = 15 * 60

MANIFEST_TIMEOUT_SECONDS = 15.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
_CHUNK = 256 * 1024

#: 交给 Inno Setup 的参数。`/SILENT` 只留一个进度条不问问题；`/AUTOUPDATE=1` 是我们自己定的
#: 开关，`installer.iss` 里 `[Run]` 看到它才在装完后把程序重新拉起来——管理员静默部署时不拉。
INSTALLER_ARGS = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                  "/AUTOUPDATE=1")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(RuntimeError):
    """更新过程中的一次失败。``str(exc)`` 是能直接摆到界面上的中文。"""


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    sha256: str
    size: int
    released_at: str = ""
    notes: str = ""

    @property
    def file_name(self) -> str:
        name = Path(unquote(urlsplit(self.url).path)).name
        return name or f"update_v{self.version}.exe"

    @property
    def size_text(self) -> str:
        return f"{self.size / (1024 * 1024):.1f} MB"


def parse_version(text: str) -> tuple[int, ...]:
    """``"0.1.53"`` → ``(0, 1, 53)``。数字之外的尾巴（``-rc1``）忽略；空串 → ``()``。"""
    parts: list[int] = []
    for piece in str(text).strip().lstrip("vV").split("."):
        match = re.match(r"\d+", piece)
        if match is None:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    """``candidate`` 比 ``current`` 新？两边都解析不出数字时算不新——宁可不更新也别循环装。"""
    wanted, have = parse_version(candidate), parse_version(current)
    if not wanted or not have:
        return False
    return wanted > have


def parse_manifest(raw: bytes | str) -> Release:
    """把 `latest.json` 变成 :class:`Release`。缺字段、不是 https、hash 形状不对都当坏清单。"""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise UpdateError("版本清单不是合法的 JSON") from exc
    if not isinstance(data, dict):
        raise UpdateError("版本清单形状不对")
    version = str(data.get("version") or "").strip()
    url = str(data.get("url") or "").strip()
    sha256 = str(data.get("sha256") or "").strip().lower()
    size = data.get("size")
    if not parse_version(version):
        raise UpdateError("版本清单里没有可用的版本号")
    if not url.startswith("https://"):
        raise UpdateError("版本清单里的下载地址不是 https")
    if not _SHA256_RE.match(sha256):
        raise UpdateError("版本清单里的 SHA-256 形状不对")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise UpdateError("版本清单里没有安装包大小")
    return Release(
        version=version, url=url, sha256=sha256, size=size,
        released_at=str(data.get("released_at") or "").strip(),
        notes=str(data.get("notes") or "").strip(),
    )


def _open_url(url: str, timeout: float) -> BinaryIO:
    import urllib.request

    request = urllib.request.Request(url, headers={
        "User-Agent": "lxz-wechat-assistant-updater",
        "Cache-Control": "no-cache",
    })
    return urllib.request.urlopen(request, timeout=timeout)  # 只认 https，见 parse_manifest


def _spawn_installer(command: list[str], cwd: Path) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    # ★ close_fds=True 不是默认值的复述：单实例锁是一个打开着的文件句柄（`single_instance`），
    #   要是被安装程序继承过去，装完新版起来时会发现「已经在运行」——而那个「在运行」的
    #   是安装程序手里那份句柄。
    subprocess.Popen(command, cwd=str(cwd), close_fds=True, creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


class Updater:
    """查更新 / 下载 / 起安装程序。所有网络活都在后台线程上；回调在那条线程上叫，
    界面那头自己 ``root.after(0, ...)`` 跳回 tk 线程。

    同一时刻只跑一件事（查或下）：第二个请求直接被拒（返回 False），免得两条线程往同一个文件写。
    """

    def __init__(
        self,
        current_version: str,
        data_dir: Path,
        *,
        manifest_url: str = "",
        open_url: Callable[[str, float], BinaryIO] = _open_url,
        spawn: Callable[[list[str], Path], None] = _spawn_installer,
        installable: bool | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.current_version = current_version
        self.manifest_url = manifest_url or DEFAULT_MANIFEST_URL
        self.updates_dir = Path(data_dir) / "updates"
        self._open_url = open_url
        self._spawn = spawn
        # 从源码跑（开发机）没有「安装目录」可覆盖，装了也只是把一份 exe 装到 Programs 下，
        # 和正在跑的这份毫无关系。这时只带人去产品站。
        self._installable = bool(getattr(sys, "frozen", False)) if installable is None else installable
        self._clock = clock
        self._busy = threading.Lock()
        self.last_checked_at: float | None = None
        self.latest: Release | None = None

    @property
    def installable(self) -> bool:
        return self._installable

    # ── 查 ────────────────────────────────────────────────────────

    def check(self) -> Release | None:
        """同步查一次。有更新回 :class:`Release`，没有回 None；网络/清单问题抛 :class:`UpdateError`。"""
        url = f"{self.manifest_url}{'&' if '?' in self.manifest_url else '?'}t={int(self._clock())}"
        try:
            with self._open_url(url, MANIFEST_TIMEOUT_SECONDS) as response:
                raw = response.read(64 * 1024)
        except UpdateError:
            raise
        except Exception as exc:  # urllib 的错误族太散，界面只需要一句话
            raise UpdateError(f"连不上产品站，稍后会再试（{type(exc).__name__}）") from exc
        release = parse_manifest(raw)
        self.last_checked_at = self._clock()
        if not is_newer(release.version, self.current_version):
            self.latest = None
            _log.info("检查更新：已是最新版本", current=self.current_version, latest=release.version)
            return None
        self.latest = release
        _log.info("检查更新：发现新版本", current=self.current_version, latest=release.version,
                  size=release.size)
        return release

    def check_async(self, done: Callable[[Release | None, UpdateError | None], None]) -> bool:
        """后台查一次，结果经 ``done(release, error)`` 回来。正忙着（查或下）时回 False 且不查。"""
        if not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                release = self.check()
            except UpdateError as exc:
                _log.warning("检查更新失败", reason=str(exc))
                self._busy.release()
                done(None, exc)
                return
            except Exception as exc:  # noqa: BLE001 - 后台线程里的异常没人看得见，翻成一句话
                _log.exception("检查更新时出错")
                self._busy.release()
                done(None, UpdateError(f"检查更新时出错（{type(exc).__name__}）"))
                return
            self._busy.release()
            done(release, None)

        threading.Thread(target=run, name="wx-ui-update-check", daemon=True).start()
        return True

    # ── 下 ────────────────────────────────────────────────────────

    def cached_package(self, release: Release) -> Path | None:
        """上次已经下好且校验过的那个包（重开客户端后不必再下 25 MB）。"""
        target = self.updates_dir / release.file_name
        if target.is_file() and target.stat().st_size == release.size \
                and _sha256_of(target) == release.sha256:
            return target
        return None

    def download(self, release: Release,
                 progress: Callable[[int, int], None] | None = None) -> Path:
        """把安装包下到 ``updates/`` 并校验；回校验过的路径。任何一步不对都抛 :class:`UpdateError`。"""
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        cached = self.cached_package(release)
        if cached is not None:
            self._drop_other_packages(keep=cached)
            if progress is not None:
                progress(release.size, release.size)
            return cached

        target = self.updates_dir / release.file_name
        part = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        received = 0
        try:
            with self._open_url(release.url, DOWNLOAD_TIMEOUT_SECONDS) as response, \
                    part.open("wb") as sink:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    sink.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if received > release.size:
                        raise UpdateError("下载到的安装包比清单说的大，已丢弃")
                    if progress is not None:
                        progress(received, release.size)
        except UpdateError:
            part.unlink(missing_ok=True)
            raise
        except Exception as exc:  # 同上，网络错误族翻成一句话
            part.unlink(missing_ok=True)
            raise UpdateError(f"下载安装包失败（{type(exc).__name__}）") from exc

        if received != release.size:
            part.unlink(missing_ok=True)
            raise UpdateError(f"安装包没下完整（{received}/{release.size} 字节），已丢弃")
        if digest.hexdigest() != release.sha256:
            part.unlink(missing_ok=True)
            raise UpdateError("安装包校验不通过（SHA-256 对不上），已丢弃")

        target.unlink(missing_ok=True)
        part.replace(target)
        self._drop_other_packages(keep=target)
        _log.info("更新包已下载并校验", version=release.version, path=str(target), bytes=received)
        return target

    def download_async(self, release: Release, *,
                       progress: Callable[[int, int], None] | None,
                       done: Callable[[Path | None, UpdateError | None], None]) -> bool:
        if not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                path = self.download(release, progress)
            except UpdateError as exc:
                _log.warning("下载更新失败", version=release.version, reason=str(exc))
                self._busy.release()
                done(None, exc)
                return
            except Exception as exc:  # noqa: BLE001
                _log.exception("下载更新时出错", version=release.version)
                self._busy.release()
                done(None, UpdateError(f"下载更新时出错（{type(exc).__name__}）"))
                return
            self._busy.release()
            done(path, None)

        threading.Thread(target=run, name="wx-ui-update-download", daemon=True).start()
        return True

    def _drop_other_packages(self, keep: Path) -> None:
        for stale in self.updates_dir.glob("*.exe"):
            if stale != keep:
                try:
                    stale.unlink()
                except OSError:
                    pass  # 删不掉就留着，下次再说

    # ── 装 ────────────────────────────────────────────────────────

    def installer_command(self, package: Path) -> list[str]:
        log_path = self.updates_dir / "install.log"
        return [str(package), *INSTALLER_ARGS, f"/LOG={log_path}"]

    def launch_installer(self, package: Path) -> None:
        """起安装程序然后**立刻返回**——调用方（`__main__.shutdown`）随后就退出，剩下的归 Inno。"""
        if not self._installable:
            raise UpdateError("这份是从源码跑的，不能原地安装；请到产品站下载安装包")
        package = Path(package)
        if not package.is_file():
            raise UpdateError("安装包不见了，请重新下载")
        self._spawn(self.installer_command(package), package.parent)
        _log.info("安装程序已启动，客户端退出后由它完成升级并重新打开", package=package.name)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(release: Release) -> str:
    """点了侧栏那行之后弹的那段话。"""
    lines = [f"新版本 v{release.version}" + (f"（{release.released_at} 发布）" if release.released_at else "")]
    if release.notes:
        lines += ["", release.notes]
    lines += ["", f"点「立即更新」会下载安装包（{release.size_text}）并自动安装。",
              "安装时服务会停止约一分钟，装完客户端会自动重新打开。"]
    return "\n".join(lines)


__all__: list[str] = [
    "CHECK_INTERVAL_SECONDS", "DEFAULT_MANIFEST_URL", "FIRST_CHECK_DELAY_SECONDS",
    "RETRY_INTERVAL_SECONDS", "SITE_URL", "Release", "UpdateError", "Updater",
    "describe", "is_newer", "parse_manifest", "parse_version",
]
