"""人工接管中的候选人，连读都不读（用户裁决 2026-08-28）。

★ 这是**改过口径**的地方。原来接管只挂写侧（`guard.OutboundGuard._takeover_denial`），
读照旧跑，理由写在那个 docstring 里：「人真回了话，后端那条『未回复跟进』照样要被取消，
把读也停了恢复之后就是一段空白」。用户看过之后仍然要停读——招聘老师自己坐下来聊的时候，
助手不该再去开他那个聊天窗口（开窗抢焦点、清未读、白吃一个读名额）。

**代价是明知故犯的**：接管期间不再上报「候选人回话」那条消息事件。
下面第三组用例守的是那个「空白」不许变成别的毛病——交回分身时游标要抹掉，
否则一睁眼就把老师自己打的那一串当新消息倒给模型。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway import GatewayStore
from wxclient.gateway.sessions import AI_MANAGED, HUMAN_TAKEOVER
from wxclient.gateway.watchlist import Watchlist
from wxclient.gateway.wechat_io import QueueMessageSource

ACCOUNT = "acct-1"


class _Runtime:
    """读谁就把谁记下来。内容不重要，这组用例只关心「碰没碰这个会话」。"""

    def __init__(self) -> None:
        self.read: list[str] = []

    async def submit(self, task_type, payload):
        self.read.append(str((payload or {}).get("friend") or ""))
        return {"status": "succeeded", "result": {"messages": []}}


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.runtime = _Runtime()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def source(self, *names: str) -> QueueMessageSource:
        watchlist = Watchlist()
        watchlist.apply_backend({"only": list(names)})
        return QueueMessageSource(runtime=self.runtime, store=self.store, watchlist=watchlist)

    def take_over(self, name: str) -> None:
        self.store.set_candidate_mode(ACCOUNT, name, HUMAN_TAKEOVER, reason="我自己跟")

    def hand_back(self, name: str) -> None:
        self.store.set_candidate_mode(ACCOUNT, name, AI_MANAGED)


class SkipsTakenOverPeersTests(_Base):
    async def test_接管中的会话这一轮不打开(self) -> None:
        """★ 判据是「有没有去读」，不是「读回来处理没处理」——打开聊天窗口本身就是打扰。"""
        self.take_over("张三")
        await self.source("张三").fetch(ACCOUNT)
        self.assertEqual(self.runtime.read, [])

    async def test_只停被接管的那个人(self) -> None:
        """一个人一把闸。停整台机器的那个总闸随 089 拆过一次，别在这儿变相长回来。"""
        self.take_over("张三")
        await self.source("张三", "李四").fetch(ACCOUNT)
        self.assertEqual(self.runtime.read, ["李四"])

    async def test_没设过档位的人照常读(self) -> None:
        """从来没设过 = `ai_managed`。默认接管等于装上就不干活。"""
        await self.source("王五").fetch(ACCOUNT)
        self.assertEqual(self.runtime.read, ["王五"])

    async def test_交回分身之后又读了(self) -> None:
        self.take_over("张三")
        await self.source("张三").fetch(ACCOUNT)
        self.hand_back("张三")
        await self.source("张三").fetch(ACCOUNT)
        self.assertEqual(self.runtime.read, ["张三"])

    async def test_全被接管时一个都不读(self) -> None:
        """名单非空但全被接管——这一轮等于空名单，不该退化成「那就都读吧」。"""
        self.take_over("张三")
        self.take_over("李四")
        await self.source("张三", "李四").fetch(ACCOUNT)
        self.assertEqual(self.runtime.read, [])


class ForgetsTheCursorTests(_Base):
    """交回分身时抹游标：接管期间没轮询，直接续上就是把老师自己打的话当新消息。"""

    def _cursor(self, name: str = "张三") -> str | None:
        return self.store.get_cursor(ACCOUNT, name)[0]

    def test_本机按钮交回时抹掉游标(self) -> None:
        self.store.set_cursor(ACCOUNT, "张三", "sig-接管前", 7)
        self.take_over("张三")
        self.hand_back("张三")
        self.assertIsNone(self._cursor(), "游标还在 = 会把接管期间那一堆当新消息")

    def test_按下接管时不抹(self) -> None:
        """抹的是**交回**那一下。按下接管就抹，等于把接管前那段也丢了。"""
        self.store.set_cursor(ACCOUNT, "张三", "sig-接管前", 7)
        self.take_over("张三")
        self.assertEqual(self._cursor(), "sig-接管前")

    def test_一直是托管的人不动他的游标(self) -> None:
        """没接管过就没有「交回」这回事。每轮同步都抹一次 = 每轮都从头认一次。"""
        self.store.set_cursor(ACCOUNT, "张三", "sig-正常", 7)
        self.hand_back("张三")
        self.assertEqual(self._cursor(), "sig-正常")

    def _age_the_takeover(self, name: str = "张三") -> None:
        """把本机这一行的写入时间往前推。

        091 有个 180 秒隔离窗：本机刚按下的接管，不许被一条**可能在按下之前就发出**的
        同步应答用 `ai_managed` 盖回去。这组用例要验的是窗口之外的正常交回，
        所以得先让这一行「不新鲜」——真机上等 3 分钟自然就过了。
        """
        with sqlite3.connect(Path(self._tmp.name) / "gateway.db") as conn:
            conn.execute(
                "UPDATE gateway_candidate_mode SET updated_at='2020-01-01T00:00:00+00:00' "
                "WHERE account_id=? AND session_name=?",
                (ACCOUNT, name),
            )
            conn.commit()

    def test_同步名单把人交回来时也抹(self) -> None:
        """★ 第二条路：别人在企业端点的「交回」，是随 `GET /client/contacts` 回来的。

        只修本机按钮那条，换机 / 别人改的那一路就照样会倒历史给模型。
        """
        self.store.save_contacts(ACCOUNT, [{
            "number": "138", "session_name": "张三", "contact_status": "accepted",
            "worker_id": "w-1", "conversation_mode": HUMAN_TAKEOVER,
        }])
        self.store.set_cursor(ACCOUNT, "张三", "sig-接管前", 7)
        self.assertEqual(self.store.candidate_mode(ACCOUNT, "张三").mode, HUMAN_TAKEOVER)
        self._age_the_takeover()

        self.store.save_contacts(ACCOUNT, [{
            "number": "138", "session_name": "张三", "contact_status": "accepted",
            "worker_id": "w-1", "conversation_mode": AI_MANAGED,
        }])
        self.assertEqual(self.store.candidate_mode(ACCOUNT, "张三").mode, AI_MANAGED)
        self.assertIsNone(self._cursor())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
