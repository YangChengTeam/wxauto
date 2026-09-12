"""自动通过别人发来的好友申请（用户 2026-09-12 裁决「要改成自动通过」）。

现场：候选人扫招聘老师的名片主动加好友，「新的朋友」一直挂着未读，没人点通过——之前整条链上
没有任何一处会点（spec 090 的边界）。现在 ``FriendWatcher`` 开着 ``auto_accept`` 就走指令那条路
下 ``accept_new_friends``，真通过了谁再由 runner 临时纳入监听、第一轮交 AI 端接话并建档。

守四件事：
★ 通过必须走 ``CommandExecutor``（能力闸 / 配额 / 时间窗 / 幂等一道不绕），不是直接敲底座；
★ 「通过了谁」只认驱动层的 ``accepted``，不拿「新的朋友」整个列表当通过（老条目会被再报一遍）；
★ 通过的人立刻进监听、不过期、不占陌生人名额，元信息带 ``friend_request_accepted``；
★ 开关关着 / dry-run 机器 / 没有执行器 → 退回原来「只看不通过」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wxclient.gateway import (
    FriendWatcher,
    GatewayRunner,
    GatewaySettings,
    LocalFileTicketProvider,
)
from wxclient.gateway.commands import CommandResult, accepted_names
from wxclient.gateway.sessions import (
    STRANGER_ORIGIN_FRIEND_REQUEST,
    STRANGER_ORIGIN_SCAN,
    GatewayStore,
)
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


class StoreOriginTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_x_通过的人不过期_扫到的照旧清(self) -> None:
        old = NOW - timedelta(days=30)
        self.store.note_strangers(ACCOUNT, ["路过问一句的"], now=old)
        self.store.note_strangers(ACCOUNT, ["扫名片加的"], now=old, origin=STRANGER_ORIGIN_FRIEND_REQUEST)
        self.assertEqual(self.store.stranger_origin(ACCOUNT, "路过问一句的"), STRANGER_ORIGIN_SCAN)
        self.assertEqual(self.store.stranger_origin(ACCOUNT, "扫名片加的"), STRANGER_ORIGIN_FRIEND_REQUEST)
        self.assertIsNone(self.store.stranger_origin(ACCOUNT, "没来过的"))
        purged = self.store.purge_strangers_before(ACCOUNT, NOW - timedelta(days=7))
        self.assertEqual(purged, ["路过问一句的"])
        self.assertEqual(self.store.stranger_names(ACCOUNT), ["扫名片加的"])

    def test_通过的人不占陌生人名额_同名升级来源(self) -> None:
        self.store.note_strangers(ACCOUNT, [f"陌生人{i}" for i in range(3)], now=NOW, limit=3)
        self.assertEqual(self.store.note_strangers(ACCOUNT, ["再来一个"], now=NOW, limit=3), [], "名额满了")
        self.assertEqual(
            self.store.note_strangers(ACCOUNT, ["扫名片加的"], now=NOW, limit=3, origin=STRANGER_ORIGIN_FRIEND_REQUEST),
            ["扫名片加的"],
        )
        self.store.note_strangers(ACCOUNT, ["陌生人0"], now=NOW, origin=STRANGER_ORIGIN_FRIEND_REQUEST)
        self.assertEqual(self.store.stranger_origin(ACCOUNT, "陌生人0"), STRANGER_ORIGIN_FRIEND_REQUEST)

    def test_老库补列_默认按_scan_算(self) -> None:
        """升级前就在表里的行没有 origin 列，补列后按 scan 算——保鲜期照旧管它们。"""
        import sqlite3
        from contextlib import closing

        path = Path(self._tmp.name) / "old.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """CREATE TABLE gateway_strangers (
                     account_id TEXT NOT NULL, session_name TEXT NOT NULL,
                     first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                     PRIMARY KEY (account_id, session_name));
                   INSERT INTO gateway_strangers VALUES ('acct-1', '老行', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00');"""
            )
        store = GatewayStore(path)
        self.assertEqual(store.stranger_origin(ACCOUNT, "老行"), STRANGER_ORIGIN_SCAN)
        self.assertEqual(store.purge_strangers_before(ACCOUNT, NOW), ["老行"])


class RunnerWiringTests(unittest.IsolatedAsyncioTestCase):
    """通过之后：立刻进监听名单、那一轮的元信息带两个标记。"""

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

    async def test_x_通过之后立刻进监听_元信息带标记(self) -> None:
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
        # 已经是候选人的张三不当陌生人；王建国进临时准入且来源是 friend_request
        self.assertEqual(self.store.stranger_names(ACCOUNT), ["王建国"])
        self.assertEqual(self.store.stranger_origin(ACCOUNT, "王建国"), STRANGER_ORIGIN_FRIEND_REQUEST)
        self.assertEqual(sorted(runner.watched_peers()), ["张三·普工", "王建国"])
        meta = runner._turn_meta("王建国", [])
        self.assertTrue(meta.get("first_contact"))
        self.assertTrue(meta.get("friend_request_accepted"))
        # 扫会话列表进来的人只有 first_contact，没有第二个标记
        self.store.note_strangers(ACCOUNT, ["路过的"], now=NOW)
        meta2 = runner._turn_meta("路过的", [])
        self.assertTrue(meta2.get("first_contact"))
        self.assertNotIn("friend_request_accepted", meta2)
        # 归位成正式候选人后，第二个标记也没有
        self.assertNotIn("first_contact", runner._turn_meta("张三·普工", []))


if __name__ == "__main__":
    unittest.main()
