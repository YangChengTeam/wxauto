"""自动更新（`wxclient/ui/updater.py`）：查、下、校验、起安装程序。

不碰网络、不碰 Tk：`open_url` 与 `spawn` 都注入假的。真机上「装完会不会自己起来」由
`packaging/installer.iss` 的 `[Run]` 决定，这里只能锁住我们传给它的参数。
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from wxclient.ui import updater
from wxclient.ui.updater import Release, UpdateError, Updater

PACKAGE = b"MZ" + bytes(range(256)) * 40
SHA = hashlib.sha256(PACKAGE).hexdigest()
URL = "https://worker.laoxiaozhi.cn/wxauto/download/%E5%8A%B3%E5%B0%8F%E6%99%BA_v0.1.60_%E5%AE%89%E8%A3%85%E5%8C%85.exe"


def manifest(**overrides: object) -> bytes:
    data: dict[str, object] = {
        "version": "0.1.60", "url": URL, "sha256": SHA, "size": len(PACKAGE),
        "released_at": "2026-09-10", "notes": "修了两个问题",
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


class FakeNet:
    """按 URL 回内容；记下每次请求。"""

    def __init__(self, routes: dict[str, bytes | Exception]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> io.BytesIO:
        self.calls.append(url)
        key = url.split("?")[0]
        body = self.routes[key]
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(body)


class Spawn:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.cwds: list[Path] = []

    def __call__(self, command: list[str], cwd: Path) -> None:
        self.commands.append(command)
        self.cwds.append(cwd)


def make(current: str = "0.1.53", *, routes: dict | None = None, installable: bool = True,
         data_dir: Path | None = None) -> tuple[Updater, FakeNet, Spawn]:
    net = FakeNet(routes if routes is not None else {updater.DEFAULT_MANIFEST_URL: manifest()})
    spawn = Spawn()
    up = Updater(current, data_dir or Path(tempfile.mkdtemp()), open_url=net, spawn=spawn,
                 installable=installable, clock=lambda: 1_700_000_000.0)
    return up, net, spawn


class VersionTests(unittest.TestCase):
    def test_versions_compare_numerically_not_as_strings(self) -> None:
        self.assertTrue(updater.is_newer("0.1.60", "0.1.9"))
        self.assertTrue(updater.is_newer("0.2.0", "0.1.99"))
        self.assertFalse(updater.is_newer("0.1.53", "0.1.53"))
        self.assertFalse(updater.is_newer("0.1.52", "0.1.53"))

    def test_garbage_is_never_newer(self) -> None:
        """两边解析不出来就不更新——宁可不更新，也别把人拖进「装完还提示更新」的循环。"""
        self.assertFalse(updater.is_newer("latest", "0.1.53"))
        self.assertFalse(updater.is_newer("0.1.60", ""))
        self.assertEqual(updater.parse_version("v0.1.60-rc1"), (0, 1, 60))


class ManifestTests(unittest.TestCase):
    def test_a_good_manifest_becomes_a_release(self) -> None:
        release = updater.parse_manifest(manifest())

        self.assertEqual(release.version, "0.1.60")
        self.assertEqual(release.size, len(PACKAGE))
        self.assertEqual(release.file_name, "劳小智_v0.1.60_安装包.exe", "文件名从 URL 里解码回来")
        self.assertIn("MB", release.size_text)

    def test_a_manifest_that_lies_is_refused(self) -> None:
        for bad in (
            b"not json",
            b"[]",
            manifest(version="latest"),
            manifest(url="http://worker.laoxiaozhi.cn/x.exe"),
            manifest(sha256="abc"),
            manifest(size=0),
            manifest(size="25000000"),
        ):
            with self.subTest(bad=bad[:40]), self.assertRaises(UpdateError):
                updater.parse_manifest(bad)


class CheckTests(unittest.TestCase):
    def test_finds_a_newer_version(self) -> None:
        up, net, _ = make("0.1.53")

        release = up.check()

        self.assertIsNotNone(release)
        assert release is not None
        self.assertEqual(release.version, "0.1.60")
        self.assertIs(up.latest, release)
        self.assertIn("?t=1700000000", net.calls[0], "带时间戳绕过 CDN/浏览器缓存")

    def test_says_nothing_when_already_current(self) -> None:
        up, _, _ = make("0.1.60")

        self.assertIsNone(up.check())
        self.assertIsNone(up.latest)

    def test_network_trouble_is_one_chinese_sentence(self) -> None:
        up, _, _ = make(routes={updater.DEFAULT_MANIFEST_URL: OSError("boom")})

        with self.assertRaises(UpdateError) as caught:
            up.check()
        self.assertIn("连不上产品站", str(caught.exception))

    def test_async_check_reports_back_on_a_thread_and_refuses_a_second_one_meanwhile(self) -> None:
        gate = threading.Event()
        results: list[tuple[object, object]] = []

        class SlowNet(FakeNet):
            def __call__(self, url: str, timeout: float) -> io.BytesIO:
                gate.wait(5)
                return super().__call__(url, timeout)

        net = SlowNet({updater.DEFAULT_MANIFEST_URL: manifest()})
        up = Updater("0.1.53", Path(tempfile.mkdtemp()), open_url=net, spawn=Spawn(), installable=True)
        finished = threading.Event()

        def done(release: object, error: object) -> None:
            results.append((release, error))
            finished.set()

        self.assertTrue(up.check_async(done))
        self.assertFalse(up.check_async(done), "正在查的时候再点一次不该起第二条线程")
        gate.set()
        self.assertTrue(finished.wait(5))
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0][0], Release)
        self.assertTrue(up.check_async(done), "上一次回来之后就能再查")
        self.assertTrue(finished.wait(5))


class DownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.release = updater.parse_manifest(manifest())

    def test_downloads_verifies_and_reports_progress(self) -> None:
        up, _, _ = make(routes={URL: PACKAGE}, data_dir=self.dir)
        seen: list[tuple[int, int]] = []

        path = up.download(self.release, lambda done, total: seen.append((done, total)))

        self.assertEqual(path, self.dir / "updates" / self.release.file_name)
        self.assertEqual(path.read_bytes(), PACKAGE)
        self.assertFalse(list(path.parent.glob("*.part")), "下完不留 .part")
        self.assertEqual(seen[-1], (len(PACKAGE), len(PACKAGE)))

    def test_a_truncated_download_is_thrown_away(self) -> None:
        up, _, _ = make(routes={URL: PACKAGE[:-100]}, data_dir=self.dir)

        with self.assertRaises(UpdateError) as caught:
            up.download(self.release)
        self.assertIn("没下完整", str(caught.exception))
        self.assertFalse(list((self.dir / "updates").iterdir()), "残件不能留下来被当成「已下载」")

    def test_a_tampered_download_is_thrown_away(self) -> None:
        wrong = bytes([PACKAGE[0] ^ 1]) + PACKAGE[1:]
        up, _, _ = make(routes={URL: wrong}, data_dir=self.dir)

        with self.assertRaises(UpdateError) as caught:
            up.download(self.release)
        self.assertIn("SHA-256", str(caught.exception))
        self.assertFalse(list((self.dir / "updates").iterdir()))

    def test_a_bigger_than_declared_body_is_cut_off_early(self) -> None:
        up, _, _ = make(routes={URL: PACKAGE + b"junk"}, data_dir=self.dir)

        with self.assertRaises(UpdateError):
            up.download(self.release)
        self.assertFalse(list((self.dir / "updates").iterdir()))

    def test_a_package_already_verified_is_not_downloaded_again(self) -> None:
        up, net, _ = make(routes={URL: PACKAGE}, data_dir=self.dir)
        first = up.download(self.release)
        stale = first.with_name("劳小智_v0.1.59_安装包.exe")
        stale.write_bytes(b"old")

        second = up.download(self.release)

        self.assertEqual(first, second)
        self.assertEqual(net.calls.count(URL), 1, "第二次直接用校验过的那份")
        self.assertFalse(stale.exists(), "旧版本的包顺手清掉")

    def test_a_stale_file_with_the_right_name_but_wrong_bytes_is_replaced(self) -> None:
        up, net, _ = make(routes={URL: PACKAGE}, data_dir=self.dir)
        target = self.dir / "updates" / self.release.file_name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"half of last time")

        path = up.download(self.release)

        self.assertEqual(path.read_bytes(), PACKAGE)
        self.assertEqual(net.calls.count(URL), 1)


class InstallTests(unittest.TestCase):
    def test_launches_inno_silently_with_the_relaunch_flag(self) -> None:
        up, _, spawn = make(routes={URL: PACKAGE})
        package = up.download(updater.parse_manifest(manifest()))

        up.launch_installer(package)

        (command,) = spawn.commands
        self.assertEqual(command[0], str(package))
        for flag in ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/AUTOUPDATE=1"):
            self.assertIn(flag, command)
        self.assertTrue(any(arg.startswith("/LOG=") for arg in command))
        self.assertEqual(spawn.cwds, [package.parent])

    def test_a_source_checkout_cannot_install_in_place(self) -> None:
        up, _, spawn = make(routes={URL: PACKAGE}, installable=False)
        package = up.download(updater.parse_manifest(manifest()))

        with self.assertRaises(UpdateError):
            up.launch_installer(package)
        self.assertEqual(spawn.commands, [])

    def test_a_missing_package_is_not_launched(self) -> None:
        up, _, spawn = make()

        with self.assertRaises(UpdateError):
            up.launch_installer(up.updates_dir / "nope.exe")
        self.assertEqual(spawn.commands, [])

    def test_the_installer_relaunch_is_gated_on_our_flag_only(self) -> None:
        """`[Run]` 那一条只认 `/AUTOUPDATE=1`——管理员批量静默部署时不该在每台机器上把程序弹出来。"""
        iss = (Path(__file__).resolve().parent.parent / "packaging" / "installer.iss").read_text("utf-8")

        self.assertIn("Check: LaunchedByUpdater", iss)
        self.assertIn("{param:AUTOUPDATE|0}", iss)
        self.assertIn("skipifsilent", iss, "原来那条「立即运行」仍然只在向导模式下出现")


class DescribeTests(unittest.TestCase):
    def test_the_prompt_says_version_size_and_what_will_happen(self) -> None:
        text = updater.describe(updater.parse_manifest(manifest()))

        self.assertIn("v0.1.60", text)
        self.assertIn("2026-09-10", text)
        self.assertIn("修了两个问题", text)
        self.assertIn("MB", text)
        self.assertIn("自动重新打开", text)


if __name__ == "__main__":
    unittest.main()


class InstallerEnvironTests(unittest.TestCase):
    """安装程序（及其拉起的新版）不继承本进程灌进去的 WXCLIENT_* 配置：
    配置只有一份权威（gateway.env），新版自己从文件读（2026-09-22 真机：老默认 30 沿链活了三个版本）。"""

    def test_own_config_keys_are_dropped_and_the_rest_kept(self) -> None:
        env = {
            "WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR": "30",
            "WXCLIENT_UI_AUTOSTART": "1",
            "PATH": "C:\\Windows",
            "LOCALAPPDATA": "C:\\Users\\x\\AppData\\Local",
        }
        cleaned = updater.installer_environ(env)
        self.assertEqual(set(cleaned), {"PATH", "LOCALAPPDATA"})
        self.assertEqual(env["WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR"], "30", "不改调用方的那份")
