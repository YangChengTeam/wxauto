"""陌生人主动发起（spec 090 / 接管托管同步接口 v1 §9.5）。

守的是两件相反的事，一件都不能松：

★ **能进来**：候选人主动加好友、主动发第一句话时，他不在任何招聘计划里、也没有档案，
  因此不在 `gateway_contacts`、不在监听名单里——他的消息网关**根本读不到**。
  没有这一层准入，「AI 主动收集资料、主动建档」整条流程从第一步就走不下去。

★ **不能滥进来**：名单是 fail-closed 白名单，它的存在理由就是「装上不会自动回复专员的
  家人朋友」（069 / 需求文档 §13.1）。所以准入**默认关**、只收今天有动静的、有名额上限、
  有保鲜期，而且第一轮还要 AI 端判得出对方在求职才会真发话（`first_contact` 标记）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wxclient.gateway import GatewayRunner, GatewaySettings, LocalFileTicketProvider
from wxclient.gateway.sessions import GatewayStore
from wxclient.gateway.strangers import MAX_STRANGERS, StrangerWatcher, session_names
from wxclient.gateway.watchlist import Watchlist
from wxclient.queue import TaskType

ACCOUNT = "acct-1"
NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)


def _contact(number: str, session_name: str, status: str = "accepted") -> dict:
    return {
        "number": number,
        "session_name": session_name,
        "profile_name": "",
        "contact_status": status,
        "worker_id": "w-1",
        "updated_at": "2026-08-26T09:00:00.000Z",
    }


def _session(name: str, *, today: bool = True) -> dict:
    return {"name": name, "text": name, "preview": "", "timestamp_text": "10:02", "timestamp_is_today": today}


class _FakeRuntime:
    """只认 `read_chat_sessions`，记下每次的 payload。"""

    def __init__(self, sessions: list[dict] | None = None, *, boom: bool = False) -> None:
        self.sessions = sessions or []
        self.calls: list[dict] = []
        self.confirmations: list[str] = []
        self.boom = boom

    async def submit(self, task_type, payload, confirmation: str = ""):
        assert task_type is TaskType.READ_CHAT_SESSIONS
        self.calls.append(dict(payload))
        self.confirmations.append(confirmation)
        if self.boom:
            raise RuntimeError("微信没开")
        return {"status": "succeeded", "result": {"sessions": self.sessions}}


class SessionNameParsingTests(unittest.TestCase):
    def test_只取今天有动静的(self) -> None:
        """把整个会话列表倒进来，等于把 fail-closed 名单变成 fail-open。"""
        result = {"result": {"sessions": [_session("今天来的"), _session("三个月前的", today=False)]}}
        self.assertEqual(session_names(result), ["今天来的"])

    def test_去重且不编名字(self) -> None:
        result = {"result": {"sessions": [_session("张三"), _session("张三"), {"name": "  "}, "坏数据"]}}
        self.assertEqual(session_names(result), ["张三"])

    def test_读不到就是空_不替它编(self) -> None:
        self.assertEqual(session_names(None), [])
        self.assertEqual(session_names({"result": {"sessions": "不是数组"}}), [])
        # dry-run 下底座回的是空壳
        self.assertEqual(session_names({"dry_run": True, "sessions": []}), [])


class StrangerAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _watcher(self, runtime, **over) -> StrangerWatcher:
        kwargs = {
            "accounts": [ACCOUNT],
            "runtime": runtime,
            "store": self.store,
            "interval_seconds": 600.0,
            "enabled": True,
            "allow_real_actions": True,
            "clock": lambda: NOW,
        }
        kwargs.update(over)
        return StrangerWatcher(**kwargs)

    async def test_x_默认关_一趟都不扫(self) -> None:
        """放开 fail-closed 名单是专员的一次知情选择，不该是缺省行为。"""
        runtime = _FakeRuntime([_session("陌生人")])
        watcher = self._watcher(runtime, enabled=False)
        self.assertFalse(watcher.enabled)
        self.assertEqual(await watcher.tick(), [])
        self.assertEqual(runtime.calls, [])
        self.assertEqual(self.store.stranger_names(ACCOUNT), [])

    async def test_不认识的名字才准入(self) -> None:
        self.store.save_contacts(ACCOUNT, [_contact("138", "张三·普工")])
        runtime = _FakeRuntime([_session("张三·普工"), _session("陌生的王工")])
        self.assertEqual(await self._watcher(runtime).tick(), ["陌生的王工"])
        self.assertEqual(self.store.stranger_names(ACCOUNT), ["陌生的王工"])

    async def test_x_读类必须显式关掉_dry_run(self) -> None:
        """底座的 dry_run 默认是 True，`read_chat_sessions` 那时回的是空壳——扫了等于没扫。"""
        runtime = _FakeRuntime([_session("陌生的王工")])
        await self._watcher(runtime).tick()
        self.assertEqual(runtime.calls[0]["dry_run"], False)

    async def test_真机读列表要带确认短语(self) -> None:
        """dry_run=False 的读也要过 `WeChatRuntime._authorize_payload`：没带确认短语会被拒，
        扫描就一直「读会话列表失败」——此前真机上正是这样默默没跑（2026-09-23 发现）。"""
        runtime = _FakeRuntime([_session("陌生的王工")])
        await self._watcher(runtime, confirmation="口令").tick()
        self.assertEqual(runtime.confirmations, ["口令"])

    async def test_dry_run_机器如实按_dry_run_跑(self) -> None:
        runtime = _FakeRuntime([])
        await self._watcher(runtime, allow_real_actions=False).tick()
        self.assertEqual(runtime.calls[0]["dry_run"], True)

    async def test_读不到不拖垮循环(self) -> None:
        runtime = _FakeRuntime(boom=True)
        self.assertEqual(await self._watcher(runtime).tick(), [])

    async def test_x_有名额上限(self) -> None:
        """这不是第二份名单：满了就不再收新的，等老的过期。"""
        many = [_session(f"陌生人{i}") for i in range(MAX_STRANGERS + 5)]
        added = await self._watcher(_FakeRuntime(many)).tick()
        self.assertEqual(len(added), MAX_STRANGERS)
        self.assertEqual(len(self.store.stranger_names(ACCOUNT)), MAX_STRANGERS)

    async def test_x_过了保鲜期就清掉(self) -> None:
        """不清的话，跑上几个月就攒出一份谁也没审过的长名单——每一项都意味着「会被自动回复」。"""
        old = NOW - timedelta(days=30)
        self.store.note_strangers(ACCOUNT, ["很久以前来过的"], now=old)
        runtime = _FakeRuntime([])
        await self._watcher(runtime).tick()
        self.assertEqual(self.store.stranger_names(ACCOUNT), [])

    async def test_再次露面会续期(self) -> None:
        self.store.note_strangers(ACCOUNT, ["王工"], now=NOW - timedelta(days=6))
        await self._watcher(_FakeRuntime([_session("王工")])).tick()
        self.assertEqual(self.store.stranger_names(ACCOUNT), ["王工"])

    async def test_不在工作时间窗一趟都不扫(self) -> None:
        runtime = _FakeRuntime([_session("陌生人")])
        # NOW 是 UTC 10:00 = 本地 18:00；把窗口设在上午
        watcher = self._watcher(runtime, active_hours="00:00-05:00")
        self.assertEqual(await watcher.tick(), [])
        self.assertEqual(runtime.calls, [])


class StrangerWiringTests(unittest.IsolatedAsyncioTestCase):
    """准入之后怎么影响监听名单和那一轮的元信息。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt"}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, watchlist: Watchlist) -> GatewayRunner:
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
        )

    def test_x_临时准入并进名单_是并集不是替换(self) -> None:
        self.store.save_contacts(ACCOUNT, [_contact("138", "张三·普工")])
        self.store.note_strangers(ACCOUNT, ["陌生的王工"], now=NOW)
        wl = Watchlist()
        self.assertEqual(self._runner(wl)._apply_derived_watchlist(), 2)
        self.assertEqual(wl.watched(), ["张三·普工", "陌生的王工"])

    def test_x_陌生人这一轮带_first_contact_标记(self) -> None:
        """AI 端据此收紧第一轮。**必须由这一层说**——入站 refs 为空是常态，
        拿它当判据会让名单里的正常联系人一起哑掉。"""
        self.store.note_strangers(ACCOUNT, ["陌生的王工"], now=NOW)
        runner = self._runner(Watchlist())
        self.assertTrue(runner._turn_meta("陌生的王工", []).get("first_contact"))
        self.assertNotIn("first_contact", runner._turn_meta("张三·普工", []))

    def test_归位成候选人之后不再算陌生人(self) -> None:
        """后端建好档、会话名归位 → 他进正式名单，临时准入撤掉。

        不撤的话他同时挂在两处，而临时准入到期会被清掉，名单反而少一个正经候选人。
        """
        self.store.note_strangers(ACCOUNT, ["王工"], now=NOW)
        self.store.save_contacts(ACCOUNT, [_contact("138", "王工")])
        settled = [n for n in self.store.stranger_names(ACCOUNT) if n in self.store.known_session_names(ACCOUNT)]
        self.assertEqual(self.store.drop_strangers(ACCOUNT, settled), 1)
        self.assertEqual(self.store.stranger_names(ACCOUNT), [])


if __name__ == "__main__":
    unittest.main()
