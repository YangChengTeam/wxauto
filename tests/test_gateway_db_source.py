"""`gateway.db_source.DbMessageSource` 的离线测试。

用假 reader（给定会话表和新消息）和假 UIA 替身（只记有没有被委回），配真 `GatewayStore`，
验证「一个会话同一时刻只归一条链路」：文本直读、首见/媒体委回、UI 持有后追平再交回、
人工接管跳过且推游标。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from wxclient.gateway import GatewayStore
from wxclient.gateway.db_source import _UI_OWNED, DbMessageSource
from wxclient.gateway.inbound import InboundMessage
from wxclient.gateway.sessions import AI_MANAGED, HUMAN_TAKEOVER
from wxclient.gateway.watchlist import Watchlist

ACCOUNT = "acct-1"
NAME = "张三"
USER = "wxid_peer"
AT = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)


def _ui_msg(text: str) -> InboundMessage:
    return InboundMessage(account_id=ACCOUNT, peer_id=NAME, msg_id=f"{NAME}#ui", text=text, at=AT)


class _FakeReader:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[int, str | None]] = {}
        self._rows: dict[str, list[dict]] = {}

    def set_session(self, local_id: int, message_type: str = "text") -> None:
        self._sessions[USER] = (local_id, message_type)

    def set_rows(self, rows: list[dict]) -> None:
        self._rows[USER] = rows

    def sessions(self) -> dict[str, tuple[int, str | None]]:
        return dict(self._sessions)

    def resolve_username(self, name: str) -> str | None:
        return USER if name == NAME else None

    def new_messages(self, username: str, after: int) -> list[dict]:
        return [r for r in self._rows.get(username, []) if r["local_id"] > after]


class _FakeUi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.returns: list[InboundMessage] = []
        self.stranger_scans = 0

    async def read_peer(self, account_id, peer_id, *, unread=None):
        self.calls.append(peer_id)
        return list(self.returns)

    async def read_unread_strangers(self, account_id, listed):
        self.stranger_scans += 1
        return []

    def set_unreachable_handler(self, handler):
        pass

    def set_not_found_handler(self, handler):
        pass

    def set_hot_window(self, seconds):
        pass

    def set_full_interval(self, seconds):
        pass


def _row(local_id: int, text: str, *, message_type: str = "text", is_self: bool = False) -> dict:
    return {
        "local_id": local_id,
        "create_time": 1_700_000_000 + local_id,
        "sort_seq": local_id,
        "text": text,
        "is_self": is_self,
        "sender_username": USER if not is_self else "wxid_self",
        "message_type": message_type,
        "needs_ui_read": message_type != "text",
    }


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.reader = _FakeReader()
        self.ui = _FakeUi()
        watchlist = Watchlist()
        watchlist.apply_backend({"only": [NAME]})
        self.source = DbMessageSource(
            reader=self.reader,
            store=self.store,
            ui_source=self.ui,
            watchlist=watchlist,
            clock=lambda: AT,
            read_unread_strangers=False,
        )

    async def fetch(self) -> list[InboundMessage]:
        return await self.source.fetch(ACCOUNT)


class TextTests(_Base):
    async def test_首次见委回UIA_并把游标基线到最新(self) -> None:
        self.reader.set_session(5)
        self.ui.returns = [_ui_msg("你好啊")]
        out = await self.fetch()
        self.assertEqual(self.ui.calls, [NAME])             # 首见交 UIA
        self.assertEqual([m.text for m in out], ["你好啊"])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 5)

    async def test_文本新消息直接从库里出_不碰UIA(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, 2)
        self.reader.set_session(4)
        self.reader.set_rows([_row(3, "在吗"), _row(4, "你好啊")])
        out = await self.fetch()
        self.assertEqual(self.ui.calls, [])                 # 没委回 UIA
        self.assertEqual([m.text for m in out], ["在吗", "你好啊"])
        self.assertEqual([m.msg_id for m in out], [f"{NAME}#3", f"{NAME}#4"])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 4)

    async def test_没有新消息返回空(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, 4)
        self.reader.set_session(4)
        self.assertEqual(await self.fetch(), [])
        self.assertEqual(self.ui.calls, [])

    async def test_名字对不上交UIA(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, 0)
        self.reader._sessions = {}          # resolve 得到 username 但不在会话表
        out = await self.fetch()
        self.assertEqual(self.ui.calls, [NAME])
        self.assertEqual(out, [])


class MediaTests(_Base):
    async def test_含媒体整段委回UIA_并标UI持有(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, 2)
        self.reader.set_session(4, "image")
        self.reader.set_rows([_row(3, "看这个"), _row(4, "", message_type="image")])
        self.ui.returns = [_ui_msg("看这个"), _ui_msg("[图片]")]
        out = await self.fetch()
        self.assertEqual(self.ui.calls, [NAME])
        self.assertEqual([m.text for m in out], ["看这个", "[图片]"])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), _UI_OWNED)

    async def test_结尾语音先压住_不委回也不推游标(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, 2)
        self.reader.set_session(3, "voice")
        self.reader.set_rows([_row(3, "", message_type="voice")])
        out = await self.fetch()
        self.assertEqual(out, [])
        self.assertEqual(self.ui.calls, [])                 # 只有结尾语音，先不处理
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 2)

    async def test_UI持有_最后一条变回文本且UIA追平_交回本地库(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, _UI_OWNED)
        self.reader.set_session(6, "text")
        self.ui.returns = []                                 # UIA 这轮没东西 = 追平了
        out = await self.fetch()
        self.assertEqual(self.ui.calls, [NAME])
        self.assertEqual(out, [])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 6)  # 交回本地库

    async def test_UI持有_UIA还在吐_保持持有(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, _UI_OWNED)
        self.reader.set_session(6, "voice")
        self.ui.returns = [_ui_msg("[语音]")]
        out = await self.fetch()
        self.assertEqual([m.text for m in out], ["[语音]"])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), _UI_OWNED)  # 仍持有


class TakeoverTests(_Base):
    async def test_人工接管这轮不读_并把游标推到最新(self) -> None:
        self.store.set_candidate_mode(ACCOUNT, NAME, HUMAN_TAKEOVER, reason="手动")
        self.store.set_db_cursor(ACCOUNT, NAME, 2)
        self.reader.set_session(9)
        out = await self.fetch()
        self.assertEqual(out, [])
        self.assertEqual(self.ui.calls, [])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 9)  # 推到最新，交回不回放

    async def test_交回分身后恢复读取(self) -> None:
        self.store.set_candidate_mode(ACCOUNT, NAME, AI_MANAGED, reason="")
        self.store.set_db_cursor(ACCOUNT, NAME, 2)
        self.reader.set_session(4)
        self.reader.set_rows([_row(3, "我回来了"), _row(4, "在吗")])
        out = await self.fetch()
        self.assertEqual([m.text for m in out], ["我回来了", "在吗"])


if __name__ == "__main__":
    unittest.main()


class StrangerScanTests(_Base):
    """名单外红点仍走 UIA，但只在本地库看到名单外单聊有新动静时才扫（每扫一次就拉一次前台）。"""

    def setUp(self) -> None:
        super().setUp()
        self.source._read_unread = True
        self.store.set_db_cursor(ACCOUNT, NAME, 5)
        self.reader.set_session(5)

    async def test_首轮扫一次_之后没动静不扫(self) -> None:
        self.reader._sessions["wxid_other"] = (10, "text")
        await self.fetch()
        await self.fetch()
        await self.fetch()
        self.assertEqual(self.ui.stranger_scans, 1)

    async def test_名单外单聊来新消息才扫(self) -> None:
        self.reader._sessions["wxid_other"] = (10, "text")
        await self.fetch()
        self.reader._sessions["wxid_other"] = (11, "text")
        await self.fetch()
        self.reader._sessions["wxid_new"] = (1, "text")  # 新冒出来的会话也算
        await self.fetch()
        self.assertEqual(self.ui.stranger_scans, 3)

    async def test_名单内或群和公众号的动静不触发扫描(self) -> None:
        self.reader._sessions["123@chatroom"] = (1, "text")
        self.reader._sessions["gh_abc"] = (1, "text")
        await self.fetch()
        self.reader.set_session(6)
        self.reader.set_rows([_row(6, "在吗")])
        self.reader._sessions["123@chatroom"] = (2, "text")
        self.reader._sessions["gh_abc"] = (2, "text")
        await self.fetch()
        self.assertEqual(self.ui.stranger_scans, 1)
