"""最后一条是对方说的，就要回（用户 2026-09-29）。

线上现象：候选人主动发了句「你好啊」，实时日志是「读到 5 条，没有新消息（游标之后一条都
没有，所以这一轮不会回），最近一条：你好啊」——分身就是不回。

这里守两件事：

1. **根因**：交回分身时抹游标，原来连序号一起删，消息 id（``会话#序号``）从 ``#1`` 重来，
   撞上去重表里早就记着的旧 id，入站那边一声不吭丢掉——游标却已经翻过去了。
2. **兜底**：不管游标为什么翻过去了，只要这一窗最后一条是对方今天说的、没人在回，就补派一次。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wxclient.gateway import GatewayStore
from wxclient.gateway.sessions import AI_MANAGED, HUMAN_TAKEOVER
from wxclient.gateway.watchlist import Watchlist
from wxclient.gateway.wechat_io import QueueMessageSource, latest_cursor

ACCOUNT = "acct-1"
PEER = "果不其然"


class _Runtime:
    """读哪个会话都回同一窗聊天记录。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def submit(self, task_type, payload):
        return {"status": "succeeded", "result": {"messages": [dict(m) for m in self.messages]}}


def _peer(text: str) -> dict:
    return {"text": text, "is_self": False}


def _self(text: str) -> dict:
    return {"text": text, "is_self": True}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.runtime = _Runtime()
        watchlist = Watchlist()
        watchlist.apply_backend({"only": [PEER]})
        self.source = QueueMessageSource(runtime=self.runtime, store=self.store, watchlist=watchlist)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def window(self, *rows: dict) -> None:
        self.runtime.messages = list(rows)

    def park_cursor_on_latest(self, seq: int = 3) -> None:
        """模拟「游标已经翻过最后一条」——不管当初是怎么翻过去的。"""
        self.store.set_cursor(ACCOUNT, PEER, latest_cursor(self.runtime.messages), seq)

    async def texts(self) -> list[str]:
        return [m.text for m in await self.source.fetch(ACCOUNT)]


class OwedReplyTests(_Base):
    async def test_最后一条是对方的_游标翻过去了也补派一次(self) -> None:
        self.window(_self("您好，我是招聘专员"), _peer("嗯"), _self("在找工作吗"), _peer("你好啊"))
        self.park_cursor_on_latest()
        self.assertEqual(await self.texts(), ["你好啊"])

    async def test_同一个位置只补一次(self) -> None:
        """补了 AI 端还是决定不说话，就到此为止——不能每趟读都再派一轮。"""
        self.window(_self("在找工作吗"), _peer("你好啊"))
        self.park_cursor_on_latest()
        self.assertEqual(await self.texts(), ["你好啊"])
        self.assertEqual(await self.texts(), [])

    async def test_尾部连着几条对方的一起补(self) -> None:
        self.window(_self("在找工作吗"), _peer("在"), _peer("你好啊"))
        self.park_cursor_on_latest()
        self.assertEqual(await self.texts(), ["在", "你好啊"])

    async def test_最后一条是自己的不补(self) -> None:
        self.window(_peer("你好啊"), _self("您好，在的"))
        self.park_cursor_on_latest()
        self.assertEqual(await self.texts(), [])

    async def test_这个人正在回_待处理表里有他就不抢(self) -> None:
        self.window(_self("在找工作吗"), _peer("你好啊"))
        self.park_cursor_on_latest()
        self.store.put_pending_inbound(ACCOUNT, PEER, "pi_x", [{"text": "你好啊"}])
        self.assertEqual(await self.texts(), [])

    async def test_UI_把自己发的认反了不补(self) -> None:
        """本机记录里这句是自己说的——补了就是自问自答。"""
        self.window(_peer("你好啊"), _peer("您好，岗位在苏州"))
        self.park_cursor_on_latest()
        self.store.record_messages(ACCOUNT, PEER, [
            {"msg_id": f"{PEER}#1", "at": "2026-09-29T01:00:00+00:00", "text": "你好啊"},
            {"msg_id": f"{PEER}#2", "at": "2026-09-29T01:00:05+00:00", "text": "您好，岗位在苏州",
             "from_self": True, "from_gateway": True},
        ])
        self.assertEqual(await self.texts(), [])

    async def test_不是今天的不补(self) -> None:
        self.window(_self("在找工作吗"), {"text": "你好啊", "is_self": False, "timestamp_is_today": False})
        self.park_cursor_on_latest()
        self.assertEqual(await self.texts(), [])

    async def test_窗口被翻上去的一屏老历史不补(self) -> None:
        """本机处理过的最近那条不在窗里 = 窗口不在底部，最后一条只是老历史（2026-09-23 事故）。"""
        self.store.record_messages(ACCOUNT, PEER, [
            {"msg_id": f"{PEER}#9", "at": "2026-09-29T01:00:00+00:00", "text": "第8句"},
        ])
        self.window(_peer("第1句"), _peer("第2句"))
        self.park_cursor_on_latest()
        self.assertEqual(await self.texts(), [])

    async def test_处理过但没回出去的也补一次(self) -> None:
        """那一轮收了没说话（或说了没发出去）：最后一条仍是对方的，再给一次机会。"""
        self.window(_self("在找工作吗"), _peer("你好啊"))
        self.park_cursor_on_latest()
        self.store.record_messages(ACCOUNT, PEER, [
            {"msg_id": f"{PEER}#3", "at": "2026-09-29T01:00:00+00:00", "text": "你好啊"},
        ])
        self.assertEqual(await self.texts(), ["你好啊"])

    async def test_正常有新消息时照旧只给新的(self) -> None:
        self.window(_self("在找工作吗"), _peer("你好啊"))
        self.park_cursor_on_latest()
        self.window(_self("在找工作吗"), _peer("你好啊"), _peer("在吗"))
        self.assertEqual(await self.texts(), ["在吗"])


class HandBackKeepsSeqTests(_Base):
    """交回分身抹游标只抹签名：序号从 0 重来，新消息的 id 就撞上去重表里的旧 id。"""

    def test_交回后按第一次读_序号接着用(self) -> None:
        self.store.set_cursor(ACCOUNT, PEER, "sig-接管前", 7)
        self.store.set_candidate_mode(ACCOUNT, PEER, HUMAN_TAKEOVER, reason="我自己跟")
        self.store.set_candidate_mode(ACCOUNT, PEER, AI_MANAGED)
        self.assertEqual(self.store.get_cursor(ACCOUNT, PEER), (None, 7))

    async def test_交回之后候选人说的话拿到没用过的id(self) -> None:
        self.store.set_cursor(ACCOUNT, PEER, "sig-接管前", 2)
        self.store.mark_seen(ACCOUNT, PEER, [f"{PEER}#1", f"{PEER}#2"])
        self.store.set_candidate_mode(ACCOUNT, PEER, HUMAN_TAKEOVER, reason="我自己跟")
        self.store.set_candidate_mode(ACCOUNT, PEER, AI_MANAGED)

        self.window(_self("我是王老师"), _peer("你好啊"))
        messages = await self.source.fetch(ACCOUNT)
        self.assertEqual([m.text for m in messages], ["你好啊"])
        ids = [m.msg_id for m in messages]
        self.assertEqual(
            self.store.filter_unseen(ACCOUNT, PEER, ids), ids,
            "id 撞上旧的 = 入站去重一声不吭丢掉，这句永远不回",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
