"""093 · 已申请、还没通过的好友，隔一阵复查一次是不是通过了。

本文件的重点是**每一条不该下结论的路都真的没下结论**：搜失败、dry-run 空壳、节拍没到，
都不许报「他通过了」。误报的代价是把一个还没通过的人放进监听名单，
之后每轮白开一次窗口（并触发 092 去补加好友）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    LocalFileTicketProvider,
)
from wxclient.gateway.friendcheck import MAX_CHECKS, PendingFriendWatcher
from wxclient.gateway.watchlist import Watchlist
from wxclient.queue import TaskType

ACCOUNT = "wxid_test"
NAME = "张三-仓库工"
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


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.now = T0
        self.accepted: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def watcher(self, runtime=None, **kwargs) -> PendingFriendWatcher:
        async def on_accepted(account_id: str, session_name: str) -> None:
            self.accepted.append((account_id, session_name))

        params = {
            "accounts": [ACCOUNT],
            "runtime": runtime or _Runtime(),
            "store": self.store,
            "interval_seconds": 900.0,
            "on_accepted": on_accepted,
            "clock": lambda: self.now,
        }
        params.update(kwargs)
        return PendingFriendWatcher(**params)

    def requested(self, number: str = NUMBER, name: str = NAME) -> None:
        """本机记一条「申请发出去了，还没通过」。"""
        self.store.note_friend_request(ACCOUNT, number, name)


class TestPicksWhoToCheck(_Base):
    def test_只挑还没通过的人(self) -> None:
        self.requested()
        self.store.mark_contact_status(ACCOUNT, "13900139000", "accepted")
        self.requested("13900139000", "李四")
        # 上面那条 note_friend_request 不会把 accepted 退回 requested（CASE WHEN 保护）
        number, name = self.store.next_friend_check(ACCOUNT, max_checks=MAX_CHECKS)
        self.assertEqual((number, name), (NUMBER, NAME))

    def test_没有会话名的不挑(self) -> None:
        self.store.note_friend_request(ACCOUNT, NUMBER, "   ")
        self.assertEqual(self.store.next_friend_check(ACCOUNT, max_checks=MAX_CHECKS), ("", ""))

    def test_最久没查的优先(self) -> None:
        self.requested()
        self.requested("13900139000", "李四")
        self.store.note_friend_check(ACCOUNT, NUMBER, T0.isoformat(), counted=True)
        number, _ = self.store.next_friend_check(ACCOUNT, max_checks=MAX_CHECKS)
        self.assertEqual(number, "13900139000")  # 没查过的排最前

    def test_查够次数就不再挑他(self) -> None:
        self.requested()
        for _ in range(MAX_CHECKS):
            self.store.note_friend_check(ACCOUNT, NUMBER, T0.isoformat(), counted=True)
        self.assertEqual(self.store.next_friend_check(ACCOUNT, max_checks=MAX_CHECKS), ("", ""))


class TestVerdict(_Base):
    async def test_搜到了就是通过了(self) -> None:
        self.requested()
        runtime = _Runtime({"match_count": 1, "exact_count": 1})
        self.assertEqual(await self.watcher(runtime).tick(), [NAME])
        self.assertEqual(self.accepted, [(ACCOUNT, NAME)])
        # 搜的是备注名，不是号
        self.assertEqual(runtime.calls[0][0], TaskType.SEARCH_CONTACTS)
        self.assertEqual(runtime.calls[0][1]["query"], NAME)

    async def test_搜不到就是还没通过(self) -> None:
        self.requested()
        self.assertEqual(await self.watcher().tick(), [])
        self.assertEqual(self.accepted, [])

    async def test_搜失败不算还没通过(self) -> None:
        """★ 搜不到 ≠ 搜失败：搜失败那一趟既不报通过，也不许计进放弃次数。

        计过次的话，一段「微信没起来」就能把人一路计到放弃阈值上，
        他真通过了也再没人去看一眼。所以这里断言的是**次数没动**（`max_checks=1` 还挑得到他），
        而**时间动了**（否则下一轮还卡在同一个人身上，后面排队的谁也轮不上）。
        """
        self.requested()
        watcher = self.watcher(_Runtime(boom=True))
        self.assertEqual(await watcher.tick(), [])
        self.assertEqual(self.accepted, [])
        self.assertEqual(self.store.next_friend_check(ACCOUNT, max_checks=1), (NUMBER, NAME))
        self.assertNotEqual(self.store.friend_check_mark(ACCOUNT, NUMBER), "")

    async def test_dry_run_空壳不算还没通过(self) -> None:
        self.requested()
        self.assertEqual(await self.watcher(_Runtime({"dry_run": True})).tick(), [])
        self.assertEqual(self.accepted, [])
        self.assertEqual(
            self.store.next_friend_check(ACCOUNT, max_checks=1), (NUMBER, NAME)
        )  # 次数没加，还轮得到他

    async def test_任务没成功也不下结论(self) -> None:
        self.requested()
        runtime = _Runtime({"exact_count": 1}, status="failed")
        self.assertEqual(await self.watcher(runtime).tick(), [])
        self.assertEqual(self.accepted, [])


class TestPacing(_Base):
    async def test_节拍没到就不动微信(self) -> None:
        """★ 走的是**共享**的 read_friends 闸：别人刚查过，这轮就得等。"""
        self.requested()
        from wxclient.gateway.guard import READ_FRIENDS_ACTION

        self.store.try_touch_read(ACCOUNT, READ_FRIENDS_ACTION, self.now, 300.0)
        runtime = _Runtime({"exact_count": 1})
        self.assertEqual(await self.watcher(runtime).tick(), [])
        self.assertEqual(runtime.calls, [])  # 一次微信都没动

    async def test_节拍到了才查(self) -> None:
        self.requested()
        from wxclient.gateway.guard import READ_FRIENDS_ACTION

        self.store.try_touch_read(ACCOUNT, READ_FRIENDS_ACTION, self.now, 300.0)
        self.now += timedelta(seconds=301)
        runtime = _Runtime({"exact_count": 1})
        self.assertEqual(await self.watcher(runtime).tick(), [NAME])

    async def test_不在工作时间窗一次也不查(self) -> None:
        """半夜自动翻通讯录是最典型的「机器在跑」。窗外连搜都不搜。

        时间给 naive（`within_active_hours` 把 naive 当本机墙上时间），
        免得测试结果跟着跑测试那台机器的时区变。
        """
        self.requested()
        runtime = _Runtime({"exact_count": 1})
        self.now = datetime(2026, 8, 27, 23, 30)  # noqa: DTZ001 —— 故意 naive，见 docstring
        watcher = self.watcher(runtime, active_hours="09:00-18:00")
        self.assertEqual(await watcher.tick(), [])
        self.assertEqual(runtime.calls, [])

    async def test_间隔为零就完全不查(self) -> None:
        self.requested()
        runtime = _Runtime({"exact_count": 1})
        self.assertEqual(await self.watcher(runtime, interval_seconds=0).tick(), [])
        self.assertEqual(runtime.calls, [])


class TestOnlyOnePerTick(_Base):
    async def test_一轮只查一个人(self) -> None:
        """查一个人 = 动一次微信搜索。名单里十个待通过的，不能一轮全搜一遍。"""
        self.requested()
        self.requested("13900139000", "李四")
        self.requested("13700137000", "王五")
        runtime = _Runtime()
        await self.watcher(runtime).tick()
        self.assertEqual(len(runtime.calls), 1)


class _SilentSource:
    async def fetch(self, account_id: str) -> list:  # pragma: no cover - 这组用例不读微信
        return []


class _SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("这条测试不该往微信发东西")


class _Contacts:
    """假的会话名上报口：只记下报了什么。"""

    def __init__(self) -> None:
        self.reported: list[dict] = []

    async def report(self, **kwargs) -> dict:
        self.reported.append(kwargs)
        return {"matched": True, "worker_id": "w-1"}


class TestLandsThroughRunner(unittest.IsolatedAsyncioTestCase):
    """★ 这组才是这次改动的重点：复查确认之后，**走的是既有那条上报路**。

    `runner._note_friend_accepted`（「收到他的消息 = 他通过了」）一直存在，但它唯一的触发点
    是入站对话——而没进名单的人根本不会被读到消息，所以那段代码从来没被调用过。
    复查补的就是这个调用，不重写一个字的上报逻辑。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt"}), encoding="utf-8")
        self.contacts = _Contacts()
        self.watchlist = Watchlist()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, checker: PendingFriendWatcher) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(ai_ws_url="ws://127.0.0.1:1/ws", local_ticket_file=self.tickets),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=_SilentSource(),
            sink=_SilentSink(),
            accounts=[ACCOUNT],
            watchlist=self.watchlist,
            contacts=self.contacts,
            friend_checker=checker,
        )

    async def test_确认通过_报给后端并立刻进名单(self) -> None:
        self.store.note_friend_request(ACCOUNT, NUMBER, NAME)
        self.assertEqual(self.store.watched_session_names(ACCOUNT), [])  # 还没通过，不在名单

        checker = PendingFriendWatcher(
            accounts=[ACCOUNT],
            runtime=_Runtime({"match_count": 1, "exact_count": 1}),
            store=self.store,
            interval_seconds=900.0,
            clock=lambda: T0,
        )
        self._runner(checker)          # 装配那一下会把 on_accepted 挂上
        self.assertEqual(await checker.tick(), [NAME])

        # ① 报给后端的是 accepted，号是本机映射查出来的那个，remark 就是会话名
        self.assertEqual(len(self.contacts.reported), 1)
        report = self.contacts.reported[0]
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["number"], NUMBER)
        self.assertEqual(report["remark"], NAME)
        # ② 本机也记一格，同一个人不会被报第二次
        self.assertEqual(self.store.pending_friend_request(ACCOUNT, NAME), "")
        # ③ 立刻进监听名单——不等下一次会话名同步，否则中间他说的话仍然没人读
        self.assertEqual(self.watchlist.watched(), [NAME])

    async def test_没通过_什么都不报(self) -> None:
        self.store.note_friend_request(ACCOUNT, NUMBER, NAME)
        checker = PendingFriendWatcher(
            accounts=[ACCOUNT],
            runtime=_Runtime({"match_count": 0, "exact_count": 0}),
            store=self.store,
            interval_seconds=900.0,
            clock=lambda: T0,
        )
        self._runner(checker)
        self.assertEqual(await checker.tick(), [])
        self.assertEqual(self.contacts.reported, [])
        self.assertEqual(self.watchlist.watched(), [])

    async def test_没配上报口也不会每轮重查同一个人(self) -> None:
        """没登录/没接业务端时 `_note_friend_accepted` 整段跳过，本机那一格得有人兜。

        不兜的话复查每轮都重新确认同一个人、白动一次微信搜索，后面排队的谁也轮不上。
        """
        self.store.note_friend_request(ACCOUNT, NUMBER, NAME)
        checker = PendingFriendWatcher(
            accounts=[ACCOUNT],
            runtime=_Runtime({"exact_count": 1}),
            store=self.store,
            interval_seconds=900.0,
            clock=lambda: T0,
        )
        self.contacts = None            # 没配上报口
        self._runner(checker)
        self.assertEqual(await checker.tick(), [NAME])
        self.assertEqual(self.store.next_friend_check(ACCOUNT, max_checks=MAX_CHECKS), ("", ""))


if __name__ == "__main__":
    unittest.main()
