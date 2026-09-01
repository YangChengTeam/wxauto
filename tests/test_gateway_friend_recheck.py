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
from wxclient.gateway.friendcheck import DEFAULT_DEADLINE_HOURS, PendingFriendWatcher
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

    def requested(self, number: str = NUMBER, name: str = NAME, at=None) -> None:
        """本机记一条「申请发出去了，还没通过」。"""
        self.store.note_friend_request(ACCOUNT, number, name, (at or self.now).isoformat())

    def not_before(self, hours: float = DEFAULT_DEADLINE_HOURS) -> str:
        """按期限算出「早于这个时刻就不再查」的界。"""
        return (self.now - timedelta(hours=hours)).isoformat()


class TestPicksWhoToCheck(_Base):
    def test_通过了就销账不再挑他(self) -> None:
        self.requested()
        self.requested("13900139000", "李四")
        self.store.drop_friend_request(ACCOUNT, "13900139000")
        number, name = self.store.next_friend_check(ACCOUNT, not_before=self.not_before())
        self.assertEqual((number, name), (NUMBER, NAME))

    def test_没有会话名的不挑(self) -> None:
        self.store.note_friend_request(ACCOUNT, NUMBER, "   ")
        self.assertEqual(
            self.store.next_friend_check(ACCOUNT, not_before=self.not_before()), ("", "")
        )

    def test_最久没查的优先(self) -> None:
        self.requested()
        self.requested("13900139000", "李四")
        self.store.note_friend_check(ACCOUNT, NUMBER, T0.isoformat())
        number, _ = self.store.next_friend_check(ACCOUNT, not_before=self.not_before())
        self.assertEqual(number, "13900139000")  # 没查过的排最前

    def test_过了期限就不再挑他(self) -> None:
        """期限从**申请那一刻**算起：查得慢不该让他无限续期。"""
        self.requested(at=T0 - timedelta(hours=100))
        self.assertEqual(
            self.store.next_friend_check(ACCOUNT, not_before=self.not_before(72.0)), ("", "")
        )

    def test_期限内的照挑(self) -> None:
        self.requested(at=T0 - timedelta(hours=10))
        number, _ = self.store.next_friend_check(ACCOUNT, not_before=self.not_before(72.0))
        self.assertEqual(number, NUMBER)

    def test_后端写的requested不算数(self) -> None:
        """`gateway_contacts` 里那条是「计划点名要加谁」，我们根本还没加——不能拿去搜。"""
        self.store.save_contacts(ACCOUNT, [
            {"number": "13700137000", "session_name": "王五", "contact_status": "requested"},
        ])
        self.assertEqual(
            self.store.next_friend_check(ACCOUNT, not_before=self.not_before()), ("", "")
        )

    def test_同步把状态冲掉也还认得出他(self) -> None:
        """后端没回 contact_status → `gateway_contacts.status` 被写成空串。
        台账只由本机写，冲不掉，所以「他通过了」仍然认得出来。"""
        self.requested()
        self.store.save_contacts(ACCOUNT, [
            {"number": NUMBER, "session_name": NAME, "contact_status": ""},
        ])
        self.assertEqual(self.store.pending_friend_request(ACCOUNT, NAME), NUMBER)
        number, _ = self.store.next_friend_check(ACCOUNT, not_before=self.not_before())
        self.assertEqual(number, NUMBER)


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
        """★ 搜不到 ≠ 搜失败：搜失败那一趟不报通过，也不该让他丢掉被查的资格。

        断言两件事：**他还在待查集里**（一段「微信没起来」不该把人搜没了），
        而**时间动了**（否则下一轮还卡在同一个人身上，后面排队的谁也轮不上）。
        放弃现在只看期限（从申请那一刻算起），搜失败多少次都不会提前把人耗掉。
        """
        self.requested()
        watcher = self.watcher(_Runtime(boom=True))
        self.assertEqual(await watcher.tick(), [])
        self.assertEqual(self.accepted, [])
        self.assertEqual(self.store.next_friend_check(ACCOUNT, not_before=self.not_before()), (NUMBER, NAME))
        self.assertNotEqual(self.store.friend_check_mark(ACCOUNT, NUMBER), "")

    async def test_dry_run_空壳不算还没通过(self) -> None:
        self.requested()
        self.assertEqual(await self.watcher(_Runtime({"dry_run": True})).tick(), [])
        self.assertEqual(self.accepted, [])
        self.assertEqual(
            self.store.next_friend_check(ACCOUNT, not_before=self.not_before()), (NUMBER, NAME)
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

    async def test_刚加过好友这轮就让路(self) -> None:
        """★ 复查给主动加好友让路（2026-08-27 安全验证）。

        搜完一个陌生号紧接着再搜一个人，在微信那边就是连着两次搜索。
        让路的是本循环：复查是死循环，这轮不查下轮再查；加好友被拒就直接丢了。
        """
        self.requested()
        from wxclient.gateway.guard import FRIEND_WRITE_ACTION

        self.store.try_touch_read(ACCOUNT, FRIEND_WRITE_ACTION, self.now, 300.0)
        runtime = _Runtime({"exact_count": 1})
        self.assertEqual(await self.watcher(runtime).tick(), [])
        self.assertEqual(runtime.calls, [])

        self.now += timedelta(seconds=301)
        self.assertEqual(await self.watcher(runtime).tick(), [NAME])

    async def test_让路只看不占_加好友那本账不动(self) -> None:
        """★ 让路用的是 `seconds_since_read`（只看不记）。

        用 `try_touch_read` 去问的话，问一句就把加好友的节拍推后 300 秒——
        本来是给它让路，结果成了替它把闸关上。
        """
        self.requested()
        from wxclient.gateway.guard import FRIEND_WRITE_ACTION

        await self.watcher(_Runtime()).tick()
        self.assertIsNone(
            self.store.seconds_since_read(ACCOUNT, FRIEND_WRITE_ACTION, self.now),
            "复查跑过一轮，加好友那本账必须还是空的",
        )

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

    `runner._note_friend_accepted`（「和他有过一次成功的消息往来 = 他通过了」）一直存在，
    但 2026-09-01 之前唯一的触发点是入站对话——而没进名单的人根本不会被读到消息，
    所以那段代码从来没被调用过。复查补的就是这个调用，不重写一个字的上报逻辑。
    """

    def not_before(self, hours: float = 72.0) -> str:
        return (T0 - timedelta(hours=hours)).isoformat()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt"}), encoding="utf-8")
        self.contacts = _Contacts()
        self.watchlist = Watchlist()
        self.now = T0

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
            clock=lambda: self.now,      # 好友通过的上报要等半小时，测试得能拨表
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
        runner = self._runner(checker)          # 装配那一下会把 on_accepted 挂上
        self.assertEqual(await checker.tick(), [NAME])

        # ① 立刻进监听名单——不等下一次会话名同步，否则中间他说的话仍然没人读
        self.assertEqual(self.watchlist.watched(), [NAME])
        # ② **当场就报**（用户 2026-09-01 去掉了原来那半小时的延迟队列）
        self.assertEqual(len(self.contacts.reported), 1)
        report = self.contacts.reported[0]
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["number"], NUMBER)
        self.assertEqual(report["remark"], NAME)
        # ③ 报成功了才销账：销了之后复查不会再挑中他
        self.assertEqual(self.store.pending_friend_request(ACCOUNT, NAME), "")
        self.assertEqual(
            self.store.next_friend_check(ACCOUNT, not_before=self.not_before()), ("", "")
        )
        _ = runner

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

    async def test_报不出去就留着下一轮再报(self) -> None:
        """没登录/没接业务端时报不出去——**台账要留着**，下一轮复查再确认再报。

        代价是后端不可达时会重复搜同一个人（受 15 分钟间隔和 ≥300s 共享闸约束）；
        换来的是上报绝不会静默丢掉，而那正是 2026-09-01 要治的病：
        requested 报上去了、人也通过了，accepted 再没上来过。
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
        # 没销账：下一轮还会挑中他，直到真报出去为止
        number, _ = self.store.next_friend_check(ACCOUNT, not_before=self.not_before())
        self.assertEqual(number, NUMBER)


if __name__ == "__main__":
    unittest.main()
