"""`wechat_db.reader` 的离线测试：拿明文 SQLite fixture 冒充解密后的镜像。

真机是 ``ReadProcessMemory`` 扫钥 + AES 解密成内存镜像；这里注入 ``open_connection``
直接开明文库，只验「布局解析 / 游标 / 自己对方认定 / 名字映射 / 变化探测」这些纯逻辑。
解密本身、真实微信 schema 仍需 Windows 真机验收。
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wxclient.wechat_db.reader import WeChatDbReader

SELF = "wxid_self"
PEER = "wxid_peer"


def _msg_table(username: str) -> str:
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


class _Fixture:
    """在临时目录里搭出 ``<account>/db_storage/{session,contact,message}/*.db`` 明文库。"""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # 账号目录名带本地 _abcd 后缀；reader 的「自己」应同时认它和去后缀那个。
        self.storage = root / "wxid_self_abcd" / "db_storage"
        for sub in ("session", "contact", "message"):
            (self.storage / sub).mkdir(parents=True)
        self._build_session()
        self._build_contact()
        self._build_messages()

    def _conn(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row  # 真机解密 opener 也设了 Row，reader 按名取列
        return conn

    def _build_session(self, last_local_id: int = 0, last_type: int = 1) -> None:
        conn = self._conn(self.storage / "session" / "session.db")
        conn.execute(
            "CREATE TABLE SessionTable (username TEXT, is_hidden INT, "
            "last_msg_locald_id INT, last_msg_type INT)"
        )
        conn.execute(
            "INSERT INTO SessionTable VALUES (?,?,?,?)", (PEER, 0, last_local_id, last_type)
        )
        conn.commit()
        conn.close()

    def set_session(self, last_local_id: int, last_type: int = 1) -> None:
        conn = self._conn(self.storage / "session" / "session.db")
        conn.execute(
            "UPDATE SessionTable SET last_msg_locald_id=?, last_msg_type=? WHERE username=?",
            (last_local_id, last_type, PEER),
        )
        conn.commit()
        conn.close()

    def _build_contact(self) -> None:
        conn = self._conn(self.storage / "contact" / "contact.db")
        conn.execute(
            "CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT,"
            " local_type INTEGER DEFAULT 1, delete_flag INTEGER DEFAULT 0)"
        )
        conn.execute("INSERT INTO contact (username, remark, nick_name) VALUES (?,?,?)", (PEER, "张三", "小张"))
        conn.commit()
        conn.close()

    def _build_messages(self) -> None:
        conn = self._conn(self.storage / "message" / "message_0.db")
        table = _msg_table(PEER)
        conn.execute(
            f'CREATE TABLE "{table}" (local_id INT, server_id INT, local_type INT, '
            f"sort_seq INT, real_sender_id INT, create_time INT, "
            f"message_content, compress_content)"
        )
        conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
        # rowid 1 = 自己，rowid 2 = 对方
        conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, ?)", (SELF,))
        conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (2, ?)", (PEER,))
        conn.commit()
        conn.close()

    def add_message(
        self, local_id: int, text: str, *, from_self: bool = False, local_type: int = 1
    ) -> None:
        conn = self._conn(self.storage / "message" / "message_0.db")
        table = _msg_table(PEER)
        conn.execute(
            f'INSERT INTO "{table}" (local_id, server_id, local_type, sort_seq, '
            f"real_sender_id, create_time, message_content, compress_content) "
            f"VALUES (?,?,?,?,?,?,?,?)",
            (local_id, local_id, local_type, local_id, 1 if from_self else 2,
             1_700_000_000 + local_id, text, None),
        )
        conn.commit()
        conn.close()
        self.set_session(local_id, local_type)

    def reader(self) -> WeChatDbReader:
        return WeChatDbReader(self.storage, open_connection=self._conn)

    def cleanup(self) -> None:
        self._tmp.cleanup()


class ReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        self.reader = self.fx.reader()
        # 先关连接再删临时目录：Windows 不让删打开着的库文件（后注册的 cleanup 先跑）。
        self.addCleanup(self.reader.close)

    def test_sessions_报出最后一条的_local_id_和类型(self) -> None:
        self.fx.add_message(5, "在吗")
        self.assertEqual(self.reader.sessions(), {PEER: (5, "text")})

    def test_resolve_username_按备注名(self) -> None:
        self.assertEqual(self.reader.resolve_username("张三"), PEER)

    def test_resolve_username_撞名返回None(self) -> None:
        conn = sqlite3.connect(self.fx.storage / "contact" / "contact.db")
        conn.execute("INSERT INTO contact (username, remark, nick_name) VALUES ('wxid_other','张三','别人')")
        conn.commit()
        conn.close()
        self.assertIsNone(self.reader.resolve_username("张三"))

    def _add_contact(self, username: str, remark: str, local_type: int, delete_flag: int = 0) -> None:
        conn = sqlite3.connect(self.fx.storage / "contact" / "contact.db")
        conn.execute(
            "INSERT INTO contact (username, remark, nick_name, local_type, delete_flag) VALUES (?,?,?,?,?)",
            (username, remark, "昵称", local_type, delete_flag),
        )
        conn.commit()
        conn.close()

    def test_friend_names_只认已是好友的备注名(self) -> None:
        self._add_contact("wxid_wait", "李四-仓库工", 3)      # 申请还没通过：备注落在陌生人行上
        self._add_contact("wxid_gone", "王五-普工", 1, 1)     # 删了
        names = ["李四-仓库工", "张三", "王五-普工", "查无此人"]
        self.assertEqual(self.reader.friend_names(names), ["张三"])

    def test_friend_names_撞名不算(self) -> None:
        self._add_contact("wxid_other", "张三", 1)
        self.assertEqual(self.reader.friend_names(["张三"]), [])

    def test_display_name_备注优先(self) -> None:
        self.assertEqual(self.reader.display_name(PEER), "张三")

    def test_new_messages_切出游标之后的_认自己和对方(self) -> None:
        self.fx.add_message(1, "你好", from_self=True)   # 招聘老师/分身
        self.fx.add_message(2, "你好啊")                  # 候选人
        self.fx.add_message(3, "在的")
        rows = self.reader.new_messages(PEER, 1)
        self.assertEqual([r["text"] for r in rows], ["你好啊", "在的"])
        self.assertTrue(all(r["is_self"] is False for r in rows))
        self.assertTrue(all(r["message_type"] == "text" for r in rows))

    def test_new_messages_自己发的标is_self(self) -> None:
        self.fx.add_message(1, "我是招聘专员", from_self=True)
        rows = self.reader.new_messages(PEER, 0)
        self.assertEqual(rows[0]["is_self"], True)

    def test_new_messages_媒体只带类型需委回UIA(self) -> None:
        self.fx.add_message(1, "", local_type=34)  # 语音
        self.fx.add_message(2, "", local_type=3)   # 图片
        rows = self.reader.new_messages(PEER, 0)
        self.assertEqual([r["message_type"] for r in rows], ["voice", "image"])
        self.assertTrue(all(r["needs_ui_read"] for r in rows))

    def test_new_messages_文本解不出正文交UIA不丢(self) -> None:
        self.fx.add_message(1, "")  # 如 zstd 解压失败：宁可开窗读，也别悄悄吞掉
        self.fx.add_message(2, "你好啊")
        rows = self.reader.new_messages(PEER, 0)
        self.assertEqual([(r["text"], r["needs_ui_read"]) for r in rows], [("", True), ("你好啊", False)])

    def test_new_messages_系统消息被跳过(self) -> None:
        self.fx.add_message(1, "你已添加了张三", local_type=10000)
        self.fx.add_message(2, "你好啊")
        self.assertEqual([r["text"] for r in self.reader.new_messages(PEER, 0)], ["你好啊"])

    def test_复合_local_type_取低32位(self) -> None:
        # 25769803825 = (6<<32)|49 → 文件(49)，不是未知类型
        self.fx.add_message(1, "", local_type=25769803825)
        rows = self.reader.new_messages(PEER, 0)
        self.assertEqual(rows[0]["message_type"], "file")

    def test_session_cursor_是最大_local_id(self) -> None:
        self.fx.add_message(3, "a")
        self.fx.add_message(7, "b")
        self.assertEqual(self.reader.session_cursor(PEER), 7)

    def test_poll_changes_首次建基线_其后变了才True(self) -> None:
        self.fx.add_message(1, "a")
        self.assertFalse(self.reader.poll_changes())  # 首次建基线
        self.assertFalse(self.reader.poll_changes())  # 没变
        self.fx.add_message(2, "b")
        self.assertTrue(self.reader.poll_changes())   # 变了

    def test_群消息正文切掉发送者前缀(self) -> None:
        self.fx.add_message(1, "wxid_peer:\n大家好")
        self.assertEqual(self.reader.new_messages(PEER, 0)[0]["text"], "大家好")


if __name__ == "__main__":
    unittest.main()


class BuildReaderTests(unittest.TestCase):
    """换过号的机器上留着旧账号目录：不能因此整条回退 UIA，要挑密钥对得上的那个。"""

    def test_旧账号对不上就试下一个(self) -> None:
        from unittest import mock

        from wxclient.wechat_db import reader as module

        tried: list[str] = []

        def attach(self: WeChatDbReader) -> None:
            tried.append(self._storage.parent.name)
            if self._storage.parent.name != "wxid_now":
                raise ValueError("当前登录微信与本地库不匹配")

        storages = [Path("x/wxid_old/db_storage"), Path("x/wxid_now/db_storage")]
        with mock.patch.object(module, "locate_storages", return_value=storages), mock.patch.object(
            WeChatDbReader, "attach", attach
        ):
            reader = module.build_reader()
        self.assertIsNotNone(reader)
        self.assertEqual(reader._storage.parent.name, "wxid_now")
        self.assertEqual(tried, ["wxid_old", "wxid_now"])

    def test_全都对不上回退None(self) -> None:
        from unittest import mock

        from wxclient.wechat_db import reader as module

        def attach(self: WeChatDbReader) -> None:
            raise ValueError("不匹配")

        with mock.patch.object(module, "locate_storages", return_value=[Path("x/a/db_storage")]), mock.patch.object(
            WeChatDbReader, "attach", attach
        ):
            self.assertIsNone(module.build_reader())


class LocateStoragesTests(unittest.TestCase):
    """数据目录挪了盘、配置读不到的机器：先问正在跑的微信打开着哪些库，问不出来再猜。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def _account(self, base: Path, name: str, mtime: int = 0) -> Path:
        storage = base / name / "db_storage"
        (storage / "session").mkdir(parents=True)
        if mtime:
            session = storage / "session" / "session.db"
            session.touch()
            import os

            os.utime(session, (mtime, mtime))
        return storage

    @staticmethod
    def _psutil(processes: list[tuple[str, object]]):
        from types import SimpleNamespace

        def proc(name: str, files: object):
            def open_files():
                if isinstance(files, Exception):
                    raise files
                return [SimpleNamespace(path=str(p)) for p in files]

            return SimpleNamespace(info={"name": name}, open_files=open_files)

        return SimpleNamespace(process_iter=lambda attrs=None: [proc(n, f) for n, f in processes])

    def test_微信开着的库文件指出挪了盘的账号目录(self) -> None:
        from unittest import mock

        from wxclient.wechat_db import reader as module

        mine = self._account(self.tmp / "D" / "xwechat_files", "wxid_now_ab12")
        other = self._account(self.tmp / "E" / "xwechat_files", "wxid_other")
        fake = self._psutil([
            ("explorer.exe", [other / "session" / "session.db"]),
            ("Weixin.exe", [mine / "message" / "message_0.db", mine / "session" / "session.db-wal",
                            self.tmp / "log.txt", self.tmp / "gone" / "db_storage" / "x" / "x.db"]),
        ])
        with mock.patch.dict("sys.modules", {"psutil": fake}):
            self.assertEqual(module._open_storages(), [mine])

    def test_问不出来就给空不往上炸(self) -> None:
        from unittest import mock

        from wxclient.wechat_db import reader as module

        denied = self._psutil([("Weixin.exe", PermissionError("denied"))])
        with mock.patch.dict("sys.modules", {"psutil": denied}):
            self.assertEqual(module._open_storages(), [])
        with mock.patch.dict("sys.modules", {"psutil": None}):
            self.assertEqual(module._open_storages(), [])

    def test_每份配置都看_空的和指着没有的位置的跳过_默认文档目录兜底(self) -> None:
        from unittest import mock

        from wxclient.wechat_db import reader as module

        moved = self.tmp / "D"
        (moved / "xwechat_files").mkdir(parents=True)
        home = self.tmp / "home"
        (home / "Documents" / "xwechat_files").mkdir(parents=True)
        configs = self.tmp / "config"
        configs.mkdir()
        (configs / "a.ini").write_text("", encoding="utf-8")
        (configs / "b.ini").write_text(str(self.tmp / "nowhere"), encoding="utf-8")
        (configs / "c.ini").write_text("﻿" + str(moved) + "\n", encoding="utf-8")
        with mock.patch.object(module.glob, "glob", return_value=[str(p) for p in sorted(configs.iterdir())]), \
                mock.patch.object(module.Path, "home", return_value=home):
            self.assertEqual(
                module._guessed_bases(), [moved / "xwechat_files", home / "Documents" / "xwechat_files"]
            )

    def test_微信开着的排最前_其余按最近活跃_不重复(self) -> None:
        from wxclient.wechat_db import reader as module

        base = self.tmp / "xwechat_files"
        old = self._account(base, "wxid_old", mtime=3000)
        now = self._account(base, "wxid_now", mtime=1000)
        mid = self._account(base, "wxid_mid", mtime=2000)
        (base / "all_users").mkdir()
        self.assertEqual(module._rank_storages([now], [base]), [now, old, mid])
        self.assertEqual(module._rank_storages([], [base, self.tmp / "missing"]), [old, mid, now])
