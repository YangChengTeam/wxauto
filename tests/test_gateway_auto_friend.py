"""092 · 监听名单里还不是好友的人自动补加好友。

本文件的重点**不是**「加成功了没有」，而是**每一条不该动手的路都真的没动手**。
加好友是微信风控最敏感的动作，误加一次的代价远大于漏加一次——漏了人还在名单里，
下一轮还会再来。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway import GatewayStore
from wxclient.gateway.autofriend import AutoFriendWatcher
from wxclient.gateway.watchlist import Watchlist
from wxclient.gateway.wechat_io import QueueMessageSource
from wxclient.queue import TaskType

ACCOUNT = "wxid_test"
NAME = "张三"
NUMBER = "13800138000"
T0 = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)


class _Runtime:
    """假的队列运行时：只应答 search_contacts。"""

    def __init__(self, result=None, status="succeeded", boom=False):
        self._result = result if result is not None else {"match_count": 0, "exact_count": 0}
        self._status = status
        self._boom = boom
        self.calls: list[tuple] = []

    async def submit(self, task_type, payload):
        self.calls.append((task_type, payload))
        if self._boom:
            raise RuntimeError("微信没起来")
        return {"status": self._status, "result": self._result}


@dataclass
class _Result:
    status: str = "done"
    reason: str = ""
    result: dict = field(default_factory=lambda: {"friend_state": "requested"})


class _Commands:
    """假的指令执行器：只记下被要求做了什么。"""

    def __init__(self):
        self.executed: list[dict] = []

    async def execute(self, command, on_stage=None):
        self.executed.append(command)

        return _Result()


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.commands = _Commands()
        self.now = T0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def watcher(self, runtime=None, **kwargs) -> AutoFriendWatcher:
        return AutoFriendWatcher(
            store=self.store,
            runtime=runtime or _Runtime(),
            commands=self.commands,
            clock=lambda: self.now,
            **kwargs,
        )

    def know_contact(self, name=NAME, number=NUMBER) -> None:
        """让本机知道「这个会话名对应这个号」（同步名单落下来的那份）。"""
        self.store.save_contacts(ACCOUNT, [{
            "number": number, "session_name": name,
            "updated_at": "2026-08-27T09:00:00.000Z",
        }])


class AddsWhenItShouldTests(_Base):
    async def test_it_adds_when_every_condition_holds(self) -> None:
        self.know_contact()
        await self.watcher().on_unreachable(ACCOUNT, NAME, "打开会话失败")
        self.assertEqual(len(self.commands.executed), 1)
        command = self.commands.executed[0]
        self.assertEqual(command["op"], "add_friend")
        self.assertEqual(command["args"]["number"], NUMBER)
        self.assertEqual(command["args"]["remark"], NAME)

    async def test_the_greeting_is_left_empty_on_purpose(self) -> None:
        """★ 用户 2026-08-27 裁决：验证语不填，让微信用它自己预填的那句。

        ★★ 红线：内部起因（「打开会话失败」这种话）**绝不**能出现在验证语里——
        那就是「内部指令变成一条候选人真能读到的微信」那个事故形状。
        """
        self.know_contact()
        await self.watcher().on_unreachable(ACCOUNT, NAME, "打开会话「张三」失败")
        args = self.commands.executed[0]["args"]
        self.assertEqual(args["greetings"], "")
        self.assertNotIn("reason", args)
        self.assertNotIn("失败", str(args))


class NeverAddsTests(_Base):
    """★★ 本文件的重点：每一条不该动手的路。"""

    async def test_it_does_nothing_when_the_switch_is_off(self) -> None:
        self.know_contact()
        runtime = _Runtime()
        await self.watcher(runtime=runtime, enabled=False).on_unreachable(ACCOUNT, NAME)
        self.assertEqual(self.commands.executed, [])
        self.assertEqual(runtime.calls, [], "关掉之后连通讯录都不该去搜")

    async def test_it_does_nothing_without_a_number(self) -> None:
        """陌生人、还没归位的会话本机没有号。`add_friend` 的 number 必填，这条路走不通。"""
        runtime = _Runtime()
        await self.watcher(runtime=runtime).on_unreachable(ACCOUNT, "查无此人")
        self.assertEqual(self.commands.executed, [])
        self.assertEqual(runtime.calls, [])

    async def test_it_does_nothing_when_he_is_already_a_contact(self) -> None:
        """搜到了 = 他是好友，读不到是别的原因（多半是备注名对不上）。加就错了。"""
        self.know_contact()
        runtime = _Runtime({"match_count": 1, "exact_count": 1})
        await self.watcher(runtime=runtime).on_unreachable(ACCOUNT, NAME)
        self.assertEqual(self.commands.executed, [])

    async def test_a_failed_search_is_not_proof_that_he_is_a_stranger(self) -> None:
        """★★ 最容易做错的一条：**搜失败 ≠ 没这个人**。

        微信卡了、窗口没起来、搜索框找不到——都会让这一趟失败。
        把它当成「不是好友」就会给一个本来就是好友的人再发一次申请。
        """
        self.know_contact()
        await self.watcher(runtime=_Runtime(boom=True)).on_unreachable(ACCOUNT, NAME)
        self.assertEqual(self.commands.executed, [], "搜失败时一个动作都不许做")

    async def test_a_non_succeeded_search_status_is_not_proof_either(self) -> None:
        self.know_contact()
        await self.watcher(runtime=_Runtime(status="failed")).on_unreachable(ACCOUNT, NAME)
        self.assertEqual(self.commands.executed, [])

    async def test_an_unreadable_search_shape_is_not_proof_either(self) -> None:
        """返回的结构读不出计数时，宁可不判——别拿一个空 dict 当「没这个人」。"""
        self.know_contact()
        await self.watcher(runtime=_Runtime({})).on_unreachable(ACCOUNT, NAME)
        self.assertEqual(self.commands.executed, [])

    async def test_it_never_auto_adds_the_same_number_twice(self) -> None:
        self.know_contact()
        watcher = self.watcher()
        await watcher.on_unreachable(ACCOUNT, NAME)
        self.now = T0 + timedelta(days=30)
        await watcher.on_unreachable(ACCOUNT, NAME)
        self.assertEqual(len(self.commands.executed), 1, "同一个号只自动加一次")

    async def test_a_contacts_sync_cannot_reset_the_dedup(self) -> None:
        """★★ 去重台账**不能**是 gateway_contacts.status——那张表同步名单会覆盖。

        真拿它当去重键的话，服务端同步一次就把同一个人再加一遍：
        加好友重复执行就是风控事故。
        """
        self.know_contact()
        watcher = self.watcher()
        await watcher.on_unreachable(ACCOUNT, NAME)
        self.assertEqual(len(self.commands.executed), 1)
        # 同步名单把这一行整个覆盖一遍（status 被服务端的值盖掉）
        self.know_contact()
        self.now = T0 + timedelta(hours=2)
        await watcher.on_unreachable(ACCOUNT, NAME)
        self.assertEqual(len(self.commands.executed), 1, "同步名单不许把去重冲掉")

    async def test_two_different_people_are_paced_apart(self) -> None:
        """名单一次刷进来十几个读不到的，不能连着十几条好友申请。"""
        self.know_contact(NAME, NUMBER)
        self.know_contact("李四", "13900139000")
        watcher = self.watcher()
        await watcher.on_unreachable(ACCOUNT, NAME)
        await watcher.on_unreachable(ACCOUNT, "李四")
        self.assertEqual(len(self.commands.executed), 1, "同一刻不许连加两个")
        # 节拍到了就该轮到第二个
        self.now = T0 + timedelta(seconds=301)
        await watcher.on_unreachable(ACCOUNT, "李四")
        self.assertEqual(len(self.commands.executed), 2)


class LedgerTests(_Base):
    async def test_the_ledger_is_written_before_the_action(self) -> None:
        """★ 先记账再动手：动手成功而记账失败（磁盘满、进程被杀）会导致下一轮重加。"""
        self.know_contact()
        seen: list[str] = []

        class _Watching(_Commands):
            async def execute(self_inner, command, on_stage=None):
                seen.append(self.store.auto_friend_tried(ACCOUNT, NUMBER))
                return await super().execute(command, on_stage)

        self.commands = _Watching()
        await self.watcher().on_unreachable(ACCOUNT, NAME)
        self.assertTrue(seen and seen[0], "动手的那一刻台账上必须已经有了")

    async def test_the_ledger_survives_a_restart(self) -> None:
        self.know_contact()
        await self.watcher().on_unreachable(ACCOUNT, NAME)
        reopened = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.assertTrue(reopened.auto_friend_tried(ACCOUNT, NUMBER))

    def test_the_ledger_table_is_not_the_contacts_table(self) -> None:
        """结构性保证：两张表分开，同步名单碰不到台账那张。"""
        self.store.note_auto_friend(ACCOUNT, NUMBER, NAME, T0.isoformat())
        with closing(sqlite3.connect(self.store._path)) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("gateway_auto_friend", names)
        self.assertIn("gateway_contacts", names)


class ReportsTests(_Base):
    async def test_it_reports_the_contact_after_adding(self) -> None:
        """加完照旧走既有的上报，后端那套「好友通过就恢复初次触达」才接得上。"""
        self.know_contact()
        reported: list[tuple] = []
        watcher = self.watcher()

        async def _on_added(command, result):
            reported.append((command, result))

        watcher.on_added = _on_added
        await watcher.on_unreachable(ACCOUNT, NAME)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0][0]["op"], "add_friend")


class SearchArgsTests(_Base):
    async def test_it_searches_by_the_session_name(self) -> None:
        self.know_contact()
        runtime = _Runtime()
        await self.watcher(runtime=runtime).on_unreachable(ACCOUNT, NAME)
        task_type, payload = runtime.calls[0]
        self.assertEqual(task_type, TaskType.SEARCH_CONTACTS)
        self.assertEqual(payload["query"], NAME)


class StrikesTests(unittest.IsolatedAsyncioTestCase):
    """★ 一轮读不到不算数——微信卡一下、窗口没起来都会读不到。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.shouts: list[tuple] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _source(self, statuses):
        class _Rt:
            def __init__(self): self._left = list(statuses)
            async def submit(self, task_type, payload):
                return {"status": self._left.pop(0), "last_error": "打开会话失败"}

        async def shout(account_id, peer_id, reason=""):
            self.shouts.append((account_id, peer_id, reason))

        watchlist = Watchlist()
        watchlist.apply_backend({"only": [NAME]})
        return QueueMessageSource(
            runtime=_Rt(), store=self.store, watchlist=watchlist, on_unreachable=shout,
        )

    async def test_one_miss_does_not_shout(self) -> None:
        source = self._source(["failed"])
        await source.fetch(ACCOUNT)
        self.assertEqual(self.shouts, [], "一轮读不到不算数")

    async def test_it_shouts_exactly_once_at_the_third_miss(self) -> None:
        source = self._source(["failed"] * 5)
        for _ in range(5):
            await source.fetch(ACCOUNT)
        self.assertEqual(len(self.shouts), 1, "连击到了喊一次，之后别再刷")
        self.assertEqual(self.shouts[0][1], NAME)

    async def test_recovering_resets_the_count(self) -> None:
        source = self._source(["failed", "failed", "succeeded", "failed", "failed"])
        for _ in range(5):
            await source.fetch(ACCOUNT)
        self.assertEqual(self.shouts, [], "中间读通过一次，连击就该清零")


if __name__ == "__main__":
    unittest.main()
