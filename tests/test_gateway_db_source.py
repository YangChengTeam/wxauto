"""`gateway.db_source.DbMessageSource` 的离线测试。

用假 reader（给定会话表和新消息）和假 UIA 替身（只记有没有被委回），配真 `GatewayStore`，
验证「一个会话同一时刻只归一条链路」：文本直读、首见/媒体委回、UI 持有后追平再交回、
人工接管跳过且推游标。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
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
        self.unread: dict[str, int] = {}

    def set_session(self, local_id: int, message_type: str = "text") -> None:
        self._sessions[USER] = (local_id, message_type)

    def set_rows(self, rows: list[dict]) -> None:
        self._rows[USER] = rows

    def sessions(self) -> dict[str, tuple[int, str | None]]:
        return dict(self._sessions)

    def unread_counts(self) -> dict[str, int]:
        return dict(self.unread)

    def resolve_username(self, name: str) -> str | None:
        return USER if name == NAME else None

    def display_name(self, username: str) -> str:
        return {USER: NAME, "wxid_other": "李四", "wxid_mem": "王五"}.get(username, username)

    def new_messages(self, username: str, after: int) -> list[dict]:
        return [r for r in self._rows.get(username, []) if r["local_id"] > after]


class _FakeUi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.returns: list[InboundMessage] = []
        self.stranger_scans = 0
        self.polls = 0

    async def read_peer(self, account_id, peer_id, *, unread=None):
        self.calls.append(peer_id)
        return list(self.returns)

    async def fetch(self, account_id):
        self.polls += 1
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


def _row(
    local_id: int, text: str, *, message_type: str = "text", is_self: bool = False, at: int | None = None
) -> dict:
    return {
        "local_id": local_id,
        "create_time": at if at is not None else 1_700_000_000 + local_id,
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
    async def test_首次见从库里只接尾部今天对方发的_不开窗(self) -> None:
        now = int(time.time())
        self.reader.set_session(5)
        self.reader.set_rows([
            _row(2, "上个月的", at=now - 40 * 86400),
            _row(3, "我回过了", is_self=True, at=now - 60),
            _row(4, "在吗", at=now - 30),
            _row(5, "还招人吗", at=now),
        ])
        out = await self.fetch()
        self.assertEqual(self.ui.calls, [])                 # 首见也不开窗
        self.assertEqual([m.text for m in out], ["在吗", "还招人吗"])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 5)

    async def test_首次见历史都不是今天_一条不接只建基线(self) -> None:
        self.reader.set_session(5)
        self.reader.set_rows([_row(5, "老消息", at=int(time.time()) - 3 * 86400)])
        self.assertEqual(await self.fetch(), [])
        self.assertEqual(self.ui.calls, [])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 5)

    async def test_首次见尾部有媒体才委回UIA(self) -> None:
        self.reader.set_session(5)
        self.reader.set_rows([_row(5, "", message_type="image", at=int(time.time()))])
        self.ui.returns = [_ui_msg("[图片]")]
        out = await self.fetch()
        self.assertEqual(self.ui.calls, [NAME])
        self.assertEqual([m.text for m in out], ["[图片]"])
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

    async def test_UI持有_库里没新东西就不再开窗(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, 2)
        self.reader.set_session(4, "image")
        self.reader.set_rows([_row(3, "看这个"), _row(4, "", message_type="image")])
        self.ui.returns = [_ui_msg("[图片]")]
        await self.fetch()                                   # 委回一次
        self.ui.returns = []
        for _ in range(5):                                   # 3 秒一轮：停在图片上也不能每轮开窗
            await self.fetch()
        self.assertEqual(self.ui.calls, [NAME])

    async def test_UI持有_来了文本新消息读一次就交回本地库(self) -> None:
        self.store.set_db_cursor(ACCOUNT, NAME, 2)
        self.reader.set_session(4, "image")
        self.reader.set_rows([_row(4, "", message_type="image")])
        await self.fetch()
        self.reader.set_session(5, "text")
        self.ui.returns = [_ui_msg("收到了吗")]
        out = await self.fetch()
        self.assertEqual([m.text for m in out], ["收到了吗"])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 5)
        self.reader.set_session(6, "text")
        self.reader.set_rows([_row(6, "在吗")])
        out = await self.fetch()                             # 之后归本地库，不再开窗
        self.assertEqual([m.text for m in out], ["在吗"])
        self.assertEqual(self.ui.calls, [NAME, NAME])

    def test_本地库按固定节拍跑_不走快慢档(self) -> None:
        from wxclient.gateway.inbound import InboundLoop

        async def handler(*_args, **_kwargs):
            return None

        loop = InboundLoop(
            accounts=[ACCOUNT], source=self.source, store=self.store, handler=handler,
            poll_interval_seconds=180.0, active_poll_interval_seconds=30.0,
        )
        loop.set_pace(idle_seconds=600.0, active_seconds=120.0, floor=30.0)  # AI 端下发也不管它
        self.assertEqual(loop._interval_for(AT), 3.0)


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
    """名单外红点仍走 UIA，但只在本地库看到名单外单聊新挂上红点时才扫（每扫一次就拉一次前台）。"""

    def setUp(self) -> None:
        super().setUp()
        self.source._read_unread = True
        self.store.set_db_cursor(ACCOUNT, NAME, 5)
        self.reader.set_session(5)

    async def test_没有红点就从不扫_首轮也不扫(self) -> None:
        self.reader._sessions["wxid_other"] = (10, "text")
        for _ in range(3):
            await self.fetch()
        self.assertEqual(self.ui.stranger_scans, 0)

    async def test_启动时就挂着的红点只建基线_不扫(self) -> None:
        self.reader._sessions["wxid_other"] = (10, "text")
        self.reader.unread = {"wxid_other": 1}
        for _ in range(3):
            await self.fetch()
        self.assertEqual(self.ui.stranger_scans, 0)

    async def test_名单外单聊来新消息挂红点才扫(self) -> None:
        self.reader._sessions["wxid_other"] = (10, "text")
        await self.fetch()
        self.reader._sessions["wxid_other"] = (11, "text")  # 自己发的：涨了但没红点
        await self.fetch()
        self.reader._sessions["wxid_other"] = (12, "text")
        self.reader.unread = {"wxid_other": 1}
        await self.fetch()
        self.reader._sessions["wxid_new"] = (1, "text")  # 新冒出来的会话也算
        self.reader.unread["wxid_new"] = 1
        await self.fetch()
        self.assertEqual(self.ui.stranger_scans, 2)

    async def test_名单内或群和公众号的红点不触发扫描(self) -> None:
        self.reader._sessions["123@chatroom"] = (1, "text")
        self.reader._sessions["gh_abc"] = (1, "text")
        self.reader.unread = {"123@chatroom": 3, "gh_abc": 5, USER: 1, "brandsessionholder": 2}
        self.reader.set_session(6)
        self.reader.set_rows([_row(6, "在吗")])
        await self.fetch()
        self.assertEqual(self.ui.stranger_scans, 0)


class NewMessageLogTests(_Base):
    """用户 2026-11-04：库里读到的新消息都打日志，不管在不在名单；回不回是名单的事。"""

    def setUp(self) -> None:
        super().setUp()
        self.store.set_db_cursor(ACCOUNT, NAME, 5)
        self.reader.set_session(5)
        self.reader._sessions["wxid_other"] = (10, "text")
        self.reader._sessions["123@chatroom"] = (3, "text")
        self.reader._sessions["gh_abc"] = (7, "text")

    async def _logged(self) -> list[dict]:
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            await self.fetch()
        return [e for e in logs if e["event"] == "本地库读到新消息"]

    async def test_首轮只建基线_不打历史(self) -> None:
        self.reader._rows["wxid_other"] = [_row(10, "老消息")]
        self.assertEqual(await self._logged(), [])

    async def test_名单内外_群_公众号的新消息都打(self) -> None:
        await self.fetch()
        self.reader.set_session(6)
        self.reader.set_rows([_row(6, "在吗")])
        self.reader._sessions["wxid_other"] = (12, "image")
        self.reader._rows["wxid_other"] = [_row(11, "你好", is_self=True), _row(12, "", message_type="image")]
        self.reader._sessions["123@chatroom"] = (4, "text")
        member = _row(4, "大家好")
        member["sender_username"] = "wxid_mem"
        self.reader._rows["123@chatroom"] = [member]
        self.reader._sessions["gh_abc"] = (8, "text")
        self.reader._rows["gh_abc"] = [_row(8, "推文")]
        logged = await self._logged()
        got = [(e["会话"], e["会话类型"], e["名单内"], e["发送方"], e["类型"], e["内容"]) for e in logged]
        self.assertEqual(got, [
            (NAME, "单聊", "是", "对方", "文字", "在吗"),
            ("李四", "单聊", "否", "自己", "文字", "你好"),
            ("李四", "单聊", "否", "对方", "图片", "[图片]"),
            ("123@chatroom", "群聊", "否", "王五", "文字", "大家好"),
            ("gh_abc", "公众号", "否", "对方", "文字", "推文"),
        ])

    async def test_涨了但切不出消息_也打一条会话有变化(self) -> None:
        from structlog.testing import capture_logs

        await self.fetch()
        self.reader._sessions["wxid_other"] = (11, None)   # 比如撤回 / 系统提示：new_messages 切不出
        with capture_logs() as logs:
            await self.fetch()
        changed = [e for e in logs if e["event"] == "本地库会话有变化"]
        self.assertEqual([(e["会话"], e["最新一条类型"]) for e in changed], [("李四", "其他")])


class AttachLaterTests(unittest.IsolatedAsyncioTestCase):
    """扫钥失败不能整场退回 UIA：连上之前 UIA 顶着、按 UIA 节拍，后台每分钟再试，连上就切本地库。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.reader = _FakeReader()
        self.ui = _FakeUi()
        self.attempts = 0
        self.ready = False
        watchlist = Watchlist()
        watchlist.apply_backend({"only": [NAME]})

        def attach():
            self.attempts += 1
            return self.reader if self.ready else None

        self.source = DbMessageSource(
            attach=attach, store=self.store, ui_source=self.ui, watchlist=watchlist,
            clock=lambda: AT, read_unread_strangers=False,
        )

    async def _settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)
            if self.source._attach_task is None or self.source._attach_task.done():
                break
            await asyncio.sleep(0.01)

    async def test_没连上_UIA顶着_按UIA节拍(self) -> None:
        from unittest import mock

        from wxclient.gateway import db_source

        with mock.patch.object(db_source, "ATTACH_RETRY_SECONDS", 0.01), \
                mock.patch.object(db_source, "FIRST_ATTACH_WAIT_SECONDS", 0.02):
            self.ui.returns = [_ui_msg("你好")]
            out = await self.source.fetch(ACCOUNT)
            self.assertEqual([m.text for m in out], ["你好"])
            self.assertEqual(self.ui.polls, 1)
            self.assertIsNone(self.source.fixed_poll_interval_seconds)
            await asyncio.sleep(0.05)
            self.assertGreater(self.attempts, 1, "连不上要一直重试")
            self.source._attach_task.cancel()

    async def test_后来连上_切本地库_名单游标直接基线不重放(self) -> None:
        from unittest import mock

        from wxclient.gateway import db_source

        with mock.patch.object(db_source, "ATTACH_RETRY_SECONDS", 0.01), \
                mock.patch.object(db_source, "FIRST_ATTACH_WAIT_SECONDS", 0.02):
            await self.source.fetch(ACCOUNT)                 # UIA 顶了一轮
            self.ready = True
            await asyncio.sleep(0.05)
            await self._settle()
        self.assertEqual(self.source.fixed_poll_interval_seconds, 3.0)
        now = int(time.time())
        self.reader.set_session(5)
        self.reader.set_rows([_row(5, "UIA 已经读过的", at=now)])
        self.ui.polls = 0
        out = await self.source.fetch(ACCOUNT)
        self.assertEqual(out, [])                            # 不走首见、不重放
        self.assertEqual(self.ui.polls, 0)
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 5)
        self.reader.set_session(6)
        self.reader.set_rows([_row(6, "新的", at=now)])
        self.assertEqual([m.text for m in await self.source.fetch(ACCOUNT)], ["新的"])


class FirstAttachWaitTests(unittest.IsolatedAsyncioTestCase):
    """第一轮先等本地库连上（子进程扫钥十几秒），连上了就直接读库，不让 UIA 先顶一轮开窗。"""

    async def test_第一轮等到连上_不走UIA(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = GatewayStore(Path(tmp) / "gateway.db")
            reader, ui = _FakeReader(), _FakeUi()
            reader.set_session(5)

            def attach():
                time.sleep(0.05)                             # 子进程扫钥要一会儿
                return reader

            watchlist = Watchlist()
            watchlist.apply_backend({"only": [NAME]})
            source = DbMessageSource(attach=attach, store=store, ui_source=ui, watchlist=watchlist,
                                     clock=lambda: AT, read_unread_strangers=False)
            await source.fetch(ACCOUNT)
            self.assertEqual(ui.polls, 0)
            self.assertEqual(source.fixed_poll_interval_seconds, 3.0)


class ObserveOutsideHoursTests(_Base):
    """工作时间窗外只看不收：有变化照样打日志，但不推游标、不出消息、不开窗。"""

    async def test_窗外打日志_不推游标(self) -> None:
        from structlog.testing import capture_logs

        self.store.set_db_cursor(ACCOUNT, NAME, 5)
        self.reader.set_session(5)
        self.source._reader = self.reader
        await self.source.observe()                          # 建基线
        self.reader.set_session(6)
        self.reader.set_rows([_row(6, "晚上好")])
        with capture_logs() as logs:
            await self.source.observe()
        self.assertEqual([e["内容"] for e in logs if e["event"] == "本地库读到新消息"], ["晚上好"])
        self.assertEqual(self.store.db_cursor(ACCOUNT, NAME), 5)   # 游标没动，进窗后照常收
        self.assertEqual(self.ui.calls, [])
        out = await self.fetch()
        self.assertEqual([m.text for m in out], ["晚上好"])

    async def test_InboundLoop窗外调observe(self) -> None:
        from wxclient.gateway.inbound import InboundLoop

        seen = []

        class _Src:
            async def observe(self):
                seen.append(1)

            async def fetch(self, account_id):
                raise AssertionError("窗外不该 fetch")

        async def handler(*_a, **_k):
            return None

        loop = InboundLoop(accounts=[ACCOUNT], source=_Src(), store=self.store, handler=handler,
                           is_active=lambda _now: False)
        self.assertEqual(await loop.tick(), 0)
        self.assertEqual(seen, [1])


class WakeOnAttachTests(unittest.IsolatedAsyncioTestCase):
    """本地库后台连上时叫醒入站循环：不用睡完 UIA 那一觉（真机 180 秒）才开始看库。"""

    async def test_连上就叫醒(self) -> None:
        from wxclient.gateway.inbound import InboundLoop

        with tempfile.TemporaryDirectory() as tmp:
            store = GatewayStore(Path(tmp) / "gateway.db")
            reader, ui = _FakeReader(), _FakeUi()
            gate = {"ready": False}
            source = DbMessageSource(attach=lambda: reader if gate["ready"] else None, store=store,
                                     ui_source=ui, clock=lambda: AT, read_unread_strangers=False)
            observed = []
            real = source.observe

            async def observe():
                observed.append(source._reader is not None)
                await real()

            source.observe = observe

            async def handler(*_a, **_k):
                return None

            loop = InboundLoop(accounts=[ACCOUNT], source=source, store=store, handler=handler,
                               poll_interval_seconds=180.0, is_active=lambda _now: False)
            from unittest import mock

            from wxclient.gateway import db_source

            with mock.patch.object(db_source, "ATTACH_RETRY_SECONDS", 0.01):
                task = asyncio.create_task(loop.run())
                await asyncio.sleep(0.05)                    # 第一趟：没连上，开始 180 秒那一觉
                gate["ready"] = True
                await asyncio.sleep(0.2)                     # 后台连上 → 叫醒 → 再跑一趟
                loop.stop()
                await asyncio.wait_for(task, 2)
            self.assertIn(True, observed, "连上之后应当马上看一趟库，而不是睡满 180 秒")
