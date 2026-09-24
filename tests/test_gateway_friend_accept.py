"""自动通过别人发来的好友申请（用户 2026-09-12 裁决「要改成自动通过」）。

现场：候选人扫招聘老师的名片主动加好友，「新的朋友」一直挂着未读，没人点通过——之前整条链上
没有任何一处会点（spec 090 的边界）。现在 ``FriendWatcher`` 开着 ``auto_accept`` 就走指令那条路
下 ``accept_new_friends``，真通过了谁、认得出号的报给业务端。

守四件事：
★ 通过必须走 ``CommandExecutor``（能力闸 / 配额 / 时间窗 / 幂等一道不绕），不是直接敲底座；
★ 「通过了谁」只认驱动层的 ``accepted``，不拿「新的朋友」整个列表当通过（老条目会被再报一遍）；
★ 通过的人**不**自己进监听（2026-09-24：监听只认 ``GET /client/contacts`` 同步名单）；
★ 开关关着 / dry-run 机器 / 没有执行器 → 退回原来「只看不通过」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wxclient.gateway import (
    FriendWatcher,
    GatewayRunner,
    GatewaySettings,
    LocalFileTicketProvider,
)
from wxclient.gateway.commands import COMMANDS, CommandResult, accepted_names
from wxclient.gateway.sessions import GatewayStore
from wxclient.gateway.watchlist import Watchlist
from wxclient.queue import TaskType

ACCOUNT = "acct-1"
NOW = datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)  # 本地 10:00


class _Runtime:
    """底座：只在没开自动通过时才会被直接敲（只看不通过那条老路）。"""

    def __init__(self, items: list[str] | None = None) -> None:
        self.calls: list[tuple[Any, dict]] = []
        self._items = items or []

    async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
        self.calls.append((task_type, payload or {}))
        return {"result": {"items": list(self._items)}}


class _Commands:
    """假的指令执行器：记下指令，按预设回结果。"""

    def __init__(self, *, items=None, accepted=None, status="done", reason="") -> None:
        self.executed: list[dict] = []
        self._items = items or []
        self._accepted = accepted
        self._status = status
        self._reason = reason

    async def execute(self, command, on_stage=None):
        self.executed.append(command)
        if self._status != "done":
            return CommandResult(command["command_id"], self._status, self._reason)
        result: dict[str, Any] = {"items": list(self._items)}
        if self._accepted is not None:
            result["accepted"] = list(self._accepted)
        if accepted_names(result):
            result["friend_state"] = "accepted"
        return CommandResult(command["command_id"], "done", result=result)


class AcceptedNamesTests(unittest.TestCase):
    def test_x_只认真通过的那几个_老条目不算(self) -> None:
        """「新的朋友」列表里早就通过的老条目也在 `items` 里，拿它当通过会把老朋友再报一遍。"""
        self.assertEqual(accepted_names({"items": ["老朋友", "王建国"], "accepted": ["王建国"]}), ["王建国"])
        self.assertEqual(accepted_names({"items": ["老朋友"], "accepted": []}), [])

    def test_老驱动层没有_accepted_键时退回_items(self) -> None:
        self.assertEqual(accepted_names({"items": ["王建国", " ", "王建国"]}), ["王建国"])
        self.assertEqual(accepted_names(None), [])
        self.assertEqual(accepted_names({"items": "坏数据"}), [])


class AcceptQuotaSettleTests(unittest.TestCase):
    """配额结算只数真通过的：`items` 是「新的朋友」整个列表（含老条目），按它结算会把一小时 5 次整本烧光。"""

    def test_x_按_accepted_结算_不按_items(self) -> None:
        spec = COMMANDS["accept_new_friends"]
        self.assertEqual(spec.reserve_count({"limit": 5}), 5)
        self.assertEqual(spec.counts({}, {"items": ["老1", "老2", "老3", "老4", "老5", "王建国"], "accepted": ["王建国"]}), 1)
        self.assertEqual(spec.counts({}, {"items": ["老1", "老2"], "accepted": []}), 0)

    def test_老驱动层没有_accepted_键_退回按_items(self) -> None:
        self.assertEqual(COMMANDS["accept_new_friends"].counts({}, {"items": ["王建国"]}), 1)


class FriendWatcherAutoAcceptTests(unittest.IsolatedAsyncioTestCase):
    def _watcher(self, runtime, **over) -> FriendWatcher:
        kwargs: dict[str, Any] = {
            "accounts": [ACCOUNT],
            "runtime": runtime,
            "interval_seconds": 900.0,
            "allow_real_actions": True,
            "confirmation": "确认",
            "auto_accept": True,
            "clock": lambda: NOW,
        }
        kwargs.update(over)
        return FriendWatcher(**kwargs)

    async def test_x_通过走指令那条路_不直接敲底座(self) -> None:
        runtime = _Runtime()
        commands = _Commands(items=["老朋友", "王建国"], accepted=["王建国"])
        got: list[tuple[dict, Any, list[str]]] = []

        async def on_accepted(command, result, names):
            got.append((command, result, list(names)))

        watcher = self._watcher(runtime, commands=commands, on_accepted=on_accepted)
        self.assertTrue(watcher.auto_accept)
        self.assertEqual(await watcher.tick(), 2)
        self.assertEqual(runtime.calls, [], "自动通过不许绕开指令层直接敲底座")
        self.assertEqual(len(commands.executed), 1)
        command = commands.executed[0]
        self.assertEqual(command["op"], "accept_new_friends")
        self.assertTrue(command["command_id"])
        self.assertLessEqual(command["args"]["limit"], 5)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2], ["王建国"], "只把真通过的交出去")
        self.assertEqual(got[0][1].result.get("friend_state"), "accepted")

    async def test_一个都没通过就不回调(self) -> None:
        commands = _Commands(items=["老朋友"], accepted=[])
        called = []

        async def on_accepted(*a):
            called.append(a)

        await self._watcher(_Runtime(), commands=commands, on_accepted=on_accepted).tick()
        self.assertEqual(called, [])

    async def test_指令被拒_配额满或时间窗外_这一趟不算数_不拖垮循环(self) -> None:
        commands = _Commands(status="rejected", reason="加好友这小时的额度用完了")
        self.assertEqual(await self._watcher(_Runtime(), commands=commands).tick(), 0)

        class _Boom(_Commands):
            async def execute(self, command, on_stage=None):
                raise RuntimeError("微信没开")

        self.assertEqual(await self._watcher(_Runtime(), commands=_Boom()).tick(), 0)

    async def test_x_开关关着_dry_run_机器_没有执行器_都退回只看不通过(self) -> None:
        for over in ({"auto_accept": False}, {"allow_real_actions": False}, {"commands": None}):
            runtime = _Runtime(["王建国"])
            commands = _Commands(items=["王建国"], accepted=["王建国"])
            kwargs = {"commands": commands}
            kwargs.update(over)
            watcher = self._watcher(runtime, **kwargs)
            self.assertFalse(watcher.auto_accept, str(over))
            await watcher.tick()
            self.assertEqual(commands.executed, [], str(over))
            self.assertEqual([t for t, _ in runtime.calls], [TaskType.CHECK_NEW_FRIENDS], str(over))
            self.assertNotIn(TaskType.ACCEPT_NEW_FRIENDS, [t for t, _ in runtime.calls])

    async def test_不在工作时间窗一趟都不动(self) -> None:
        commands = _Commands(items=["王建国"], accepted=["王建国"])
        watcher = self._watcher(_Runtime(), commands=commands, active_hours="12:00-13:00")
        self.assertEqual(await watcher.tick(), 0)
        self.assertEqual(commands.executed, [])


class RetiredStrangerTableTests(unittest.TestCase):
    def test_x_升级时清掉临时准入_连同落盘名单里经它加进去的名字(self) -> None:
        """那张表是本机自己攒的名字（090 扫会话列表 / 通过好友申请）。现场：「腾讯新闻」和自己的群
        被扫进监听、界面上删不掉。同步名单里真有的（张三）和原本就在名单里的（李四）不能误删。"""
        import sqlite3
        from contextlib import closing

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            store = GatewayStore(path)
            store.save_contacts(ACCOUNT, [{
                "number": "138", "session_name": "张三·普工", "profile_name": "", "contact_status": "accepted",
                "worker_id": "w-1", "updated_at": "2026-09-12T01:00:00.000Z",
            }])
            store.put_watchlist({"only": ["AI Agent 群", "张三·普工", "李四", "腾讯新闻"], "never": ["黑名单"]})
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    """CREATE TABLE gateway_strangers (
                         account_id TEXT NOT NULL, session_name TEXT NOT NULL,
                         first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                         origin TEXT NOT NULL DEFAULT 'scan',
                         PRIMARY KEY (account_id, session_name));
                       INSERT INTO gateway_strangers VALUES ('acct-1', '腾讯新闻', 't', 't', 'scan');
                       INSERT INTO gateway_strangers VALUES ('acct-1', 'AI Agent 群', 't', 't', 'scan');
                       INSERT INTO gateway_strangers VALUES ('acct-1', '张三·普工', 't', 't', 'friend_request');"""
                )
            store = GatewayStore(path)
            self.assertEqual(store.get_watchlist(), {"only": ["张三·普工", "李四"], "never": ["黑名单"]})
            with closing(sqlite3.connect(path)) as conn:
                left = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='gateway_strangers'"
                ).fetchall()
            self.assertEqual(left, [])
            GatewayStore(path)  # 再开一次：表已经没了，不再动名单
            self.assertEqual(store.get_watchlist()["only"], ["张三·普工", "李四"])


class RunnerWiringTests(unittest.IsolatedAsyncioTestCase):
    """通过之后：只上报，不自己进监听名单。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt"}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, watchlist: Watchlist, friend_watcher: FriendWatcher | None = None) -> GatewayRunner:
        class _Silent:
            async def fetch(self, account_id: str):
                return []

            async def deliver(self, account_id: str, peer_id: str, card):
                return None

        return GatewayRunner(
            settings=GatewaySettings(ai_ws_url="ws://127.0.0.1:1/ws", local_ticket_file=self.tickets),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=_Silent(),
            sink=_Silent(),
            accounts=[ACCOUNT],
            watchlist=watchlist,
            friend_watcher=friend_watcher,
            clock=lambda: NOW,
        )

    async def test_x_通过之后不自己进监听(self) -> None:
        self.store.save_contacts(ACCOUNT, [{
            "number": "138", "session_name": "张三·普工", "profile_name": "", "contact_status": "accepted",
            "worker_id": "w-1", "updated_at": "2026-09-12T01:00:00.000Z",
        }])
        commands = _Commands(items=["老朋友", "王建国", "张三·普工"], accepted=["王建国", "张三·普工"])
        watcher = FriendWatcher(
            accounts=[ACCOUNT], runtime=_Runtime(), interval_seconds=900.0, allow_real_actions=True,
            auto_accept=True, commands=commands, clock=lambda: NOW,
        )
        wl = Watchlist()
        runner = self._runner(wl, watcher)
        self.assertIsNotNone(watcher.on_accepted, "runner 装好后回调必须接上")
        await watcher.tick()
        # ★ 监听只认同步名单：王建国刚被通过、业务端还没把他写进 contacts → 不在监听里
        runner._apply_derived_watchlist()
        self.assertEqual(runner.watched_peers(), ["张三·普工"])
        meta = runner._turn_meta("王建国", [])
        self.assertNotIn("first_contact", meta)
        self.assertNotIn("friend_request_accepted", meta)


if __name__ == "__main__":
    unittest.main()
