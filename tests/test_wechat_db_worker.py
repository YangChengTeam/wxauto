"""`wechat_db.worker`：本地库读取放子进程，父进程拿替身一问一答。

真起一个子进程（源码模式 ``python -m`` 那条路之外，这里用 ``-c`` 把 `build_reader` 换成明文
fixture），验协议、类型还原、出错不掉线、父进程关管道子进程就退。扫钥与解密本身走真机验收。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from wxclient.wechat_db.worker import WorkerReader

TESTS = Path(__file__).resolve().parent

_CHILD = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {tests!r})
    from test_wechat_db_reader import _Fixture
    import wxclient.wechat_db.reader as reader
    fx = _Fixture()
    fx.add_message(1, "在吗")
    fx.add_message(2, "我在", from_self=True)
    reader.build_reader = lambda: {build}
    from wxclient.wechat_db.worker import serve
    code = serve()
    fx.cleanup()
    sys.exit(code)
    """
)


def _spawn(build: str = "fx.reader()") -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD.format(tests=str(TESTS), build=build)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.process = _spawn()
        self.addCleanup(self.process.kill)
        self.assertEqual(self.process.stdout.readline().strip(), b'{"ok": true}')
        self.reader = WorkerReader(self.process)

    def test_查询结果跨进程原样还原(self) -> None:
        sessions = self.reader.sessions()
        self.assertIsInstance(next(iter(sessions.values())), tuple)
        rows = self.reader.new_messages("wxid_peer", 0)
        self.assertEqual([(r["text"], r["is_self"]) for r in rows], [("在吗", False), ("我在", True)])
        self.assertEqual(self.reader.session_cursor("wxid_peer"), 2)

    def test_单次查询出错不掉线(self) -> None:
        with self.assertRaises(RuntimeError):
            self.reader._call("close")                      # 不在白名单
        self.assertEqual(len(self.reader.new_messages("wxid_peer", 1)), 1)

    def test_父进程关管道子进程就退(self) -> None:
        self.reader.close()
        self.assertEqual(self.process.wait(timeout=10), 0)


class WorkerAttachFailTests(unittest.TestCase):
    def test_连不上回一行ok_false并退出(self) -> None:
        process = _spawn(build="None")
        self.addCleanup(process.kill)
        self.assertIn(b'"ok": false', process.stdout.readline())
        self.assertEqual(process.wait(timeout=10), 1)
