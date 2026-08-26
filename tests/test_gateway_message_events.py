"""微信消息事件上报（`docs/招聘分身_客户端微信消息事件上报_v1.md`）。

这条链决定业务后端排不排「未回复跟进」，所以**先证坏路被堵**：归不了位的不瞎报、
dry-run 不报、任务没确认成功不报、后端拒收不无限重试、断网不丢事件；再证好路能通：
字段与契约逐字一致、重试用同一个 eventId、候选人一开口就报得出去。

★ 最要紧的一条是 `InboundReportingTests` 里的拒绝那条：「别再联系我了」也是一次回复。
   它触发本机拉黑并当场 return，事件要是排在那行 return 后面就永远发不出去——
   而那正是最不该再追一条的时刻。
"""

from __future__ import annotations

import json
import unittest
from datetime import timedelta
from typing import Any

import httpx
from structlog.testing import capture_logs
from test_gateway_business import T0, FakeSink, FakeSource, StoreTestCase, msg

from wxclient.gateway import GatewayStore, InboundLoop, OutboundGuard, OutboundSender
from wxclient.gateway.commands import CommandExecutor
from wxclient.gateway.message_events import (
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
    MAX_AGE_HOURS,
    MAX_ATTEMPTS,
    MessageEventError,
    MessageEventReporter,
    build_event,
    event_id_for,
    local_message_id,
)

ACCOUNT = "acct-1"
PEER = "peer-1"
NUMBER = "13800138000"
CANDIDATE = "6052b11b-d0a6-4ebb-a32d-e077552f041c"


def reporter(store: GatewayStore, handler, *, token: str = "tok",
             clock=None) -> MessageEventReporter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MessageEventReporter(
        "https://api.example.com",
        lambda: token,
        store=store,
        client=client,
        clock=clock or (lambda: T0),
    )


def accepting(seen: list[dict[str, Any]], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(status, json={"accepted": True, "duplicate": False,
                                            "linked": True, "action": "schedule_follow_up"})

    return handler


def known_candidate(store: GatewayStore) -> None:
    """让本机认得「这个会话名 = 这个候选人」（平时由 `/client/contacts` 同步落下来）。"""
    store.save_contacts(
        ACCOUNT,
        [{"number": NUMBER, "session_name": PEER, "worker_id": CANDIDATE,
          "updated_at": "2026-08-24T00:00:00Z"}],
    )


# ────────────────────────── 事件形状（契约字段表）──────────────────────────


class BuildEventTests(unittest.TestCase):
    def test_contract_fields(self) -> None:
        event = build_event(
            candidate_id=CANDIDATE,
            direction=DIRECTION_OUTBOUND,
            message_id="wxmsg-1",
            occurred_at=T0,
            command_id="cmd-1",
            content="你好，在吗",
            source="command",
        )
        self.assertEqual(event["candidateId"], CANDIDATE)
        self.assertEqual(event["direction"], "outbound")
        self.assertEqual(event["messageType"], "text")
        self.assertEqual(event["messageId"], "wxmsg-1")
        self.assertEqual(event["status"], "sent")          # outbound 默认 sent
        self.assertEqual(event["commandId"], "cmd-1")
        self.assertTrue(event["occurredAt"].endswith("Z"))  # 契约要求带时区
        self.assertTrue(event["contentDigest"].startswith("sha256:"))
        self.assertNotIn("你好", json.dumps(event, ensure_ascii=False))  # 正文不出门

    def test_inbound_defaults_to_received_and_drops_command_id(self) -> None:
        """契约：候选人入站消息不要传 commandId。"""
        event = build_event(
            candidate_id=CANDIDATE, direction=DIRECTION_INBOUND,
            message_id="m1", occurred_at=T0, command_id="cmd-1",
        )
        self.assertEqual(event["status"], "received")
        self.assertNotIn("commandId", event)

    def test_event_id_is_derived_not_random(self) -> None:
        """★ 断网重投必须还是同一个 eventId，否则后端的幂等去重形同虚设。"""
        first = build_event(candidate_id=CANDIDATE, direction=DIRECTION_INBOUND,
                            message_id="m1", occurred_at=T0)
        later = build_event(candidate_id=CANDIDATE, direction=DIRECTION_INBOUND,
                            message_id="m1", occurred_at=T0 + timedelta(hours=3))
        self.assertEqual(first["eventId"], later["eventId"])
        self.assertEqual(first["eventId"], event_id_for(CANDIDATE, DIRECTION_INBOUND, "m1"))
        # 方向不同 = 不同事件（我方发的和对方回的绝不能互相顶掉）
        self.assertNotEqual(
            first["eventId"], event_id_for(CANDIDATE, DIRECTION_OUTBOUND, "m1")
        )

    def test_bad_shapes_are_refused(self) -> None:
        for kwargs in (
            {"candidate_id": "", "direction": DIRECTION_INBOUND, "message_id": "m1"},
            {"candidate_id": CANDIDATE, "direction": "sideways", "message_id": "m1"},
            {"candidate_id": CANDIDATE, "direction": DIRECTION_INBOUND, "message_id": ""},
        ):
            with self.assertRaises(MessageEventError):
                build_event(occurred_at=T0, **kwargs)
        with self.assertRaises(MessageEventError):
            build_event(candidate_id=CANDIDATE, direction=DIRECTION_INBOUND,
                        message_id="m1", occurred_at=T0, message_type="hologram")

    def test_over_long_command_id_is_dropped_not_truncated(self) -> None:
        """★ 后端 `commandId` 是 varchar(120)，超了整条事件被 400 打回。

        截断只会得到一个对不上任何指令的假 ID；事件本身（「这一刻发出去了一条消息」）
        才是后端排跟进要的东西，为一个对账字段把它整条丢掉不划算。
        """
        event = build_event(
            candidate_id=CANDIDATE, direction=DIRECTION_OUTBOUND,
            message_id="m1", occurred_at=T0, command_id="c" * 121,
        )
        self.assertNotIn("commandId", event)
        # 刚好卡在上限上要留着
        ok = build_event(
            candidate_id=CANDIDATE, direction=DIRECTION_OUTBOUND,
            message_id="m1", occurred_at=T0, command_id="c" * 120,
        )
        self.assertEqual(ok["commandId"], "c" * 120)

    def test_stays_inside_the_backend_column_widths(self) -> None:
        """契约字段表 = 后端建表宽度（messageId 180 / contentDigest 128 / commandId 120）。"""
        event = build_event(
            candidate_id=CANDIDATE, direction=DIRECTION_OUTBOUND,
            message_id="m" * 500, occurred_at=T0, content="x" * 10_000,
            command_id="cmd-1",
        )
        self.assertEqual(len(event["messageId"]), 180)
        self.assertLessEqual(len(event["contentDigest"]), 128)
        self.assertLessEqual(len(event["commandId"]), 120)

    def test_local_message_id_is_stable_and_hides_the_session_name(self) -> None:
        """本机 msg_id 形如「{备注名}#{序号}」——备注名没必要跟着每条事件出门。"""
        made = local_message_id(ACCOUNT, "张三·仓库工#7")
        self.assertEqual(made, local_message_id(ACCOUNT, "张三·仓库工#7"))
        self.assertNotIn("张三", made)


# ────────────────────────── 发件箱与上报 ──────────────────────────


class OutboxTests(StoreTestCase):
    async def test_records_then_posts_contract_body(self) -> None:
        seen: list[dict[str, Any]] = []
        rep = reporter(self.store, accepting(seen))
        rep.record(ACCOUNT, candidate_id=CANDIDATE, direction=DIRECTION_INBOUND,
                   message_id="m1", content="我想问下工资")

        self.assertEqual(self.store.count_message_events(), 1)
        self.assertEqual(await rep.flush(), 1)
        self.assertEqual(self.store.count_message_events(), 0)

        self.assertTrue(seen[0]["url"].endswith("/recruitment-agent/client/message-events"))
        self.assertEqual(seen[0]["auth"], "Bearer tok")
        self.assertEqual(seen[0]["body"]["direction"], "inbound")
        self.assertEqual(seen[0]["body"]["candidateId"], CANDIDATE)

    async def test_same_message_is_only_queued_once(self) -> None:
        """同一条消息重复读到（重投、重启补课）不能变成两条事件。"""
        seen: list[dict[str, Any]] = []
        rep = reporter(self.store, accepting(seen))
        first = rep.record(ACCOUNT, candidate_id=CANDIDATE,
                           direction=DIRECTION_INBOUND, message_id="m1")
        again = rep.record(ACCOUNT, candidate_id=CANDIDATE,
                           direction=DIRECTION_INBOUND, message_id="m1")
        self.assertEqual(first, again)
        self.assertEqual(self.store.count_message_events(), 1)
        await rep.flush()
        self.assertEqual(len(seen), 1)

    async def test_network_failure_keeps_the_event_and_retries_with_same_id(self) -> None:
        """★ 抖一下就丢，代价是候选人回了话还被追一条。留着，下一趟原样重发。"""
        attempts: list[dict[str, Any]] = []

        def flaky(request: httpx.Request) -> httpx.Response:
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                raise httpx.ConnectError("网断了")
            return httpx.Response(200, json={"accepted": True})

        rep = reporter(self.store, flaky)
        rep.record(ACCOUNT, candidate_id=CANDIDATE,
                   direction=DIRECTION_INBOUND, message_id="m1")

        self.assertEqual(await rep.flush(), 0)
        self.assertEqual(self.store.count_message_events(), 1)
        self.assertEqual(await rep.flush(), 1)
        self.assertEqual(self.store.count_message_events(), 0)
        self.assertEqual(attempts[0]["eventId"], attempts[1]["eventId"])

    async def test_not_logged_in_sends_nothing_and_keeps_the_event(self) -> None:
        """没登录 = 这台机器还没准备好，不是「这条事件不成立」。"""
        called: list[int] = []
        rep = reporter(self.store, lambda _r: called.append(1) or httpx.Response(200),
                       token="")
        rep.record(ACCOUNT, candidate_id=CANDIDATE,
                   direction=DIRECTION_INBOUND, message_id="m1")
        self.assertEqual(await rep.flush(), 0)
        self.assertEqual(called, [])
        self.assertEqual(self.store.count_message_events(), 1)

    async def test_permanent_rejection_is_dropped_not_retried(self) -> None:
        """400 = 我们发错了，重试一万次也一样。丢掉并留痕，别把发件箱堵死。"""
        called: list[int] = []
        rep = reporter(self.store,
                       lambda _r: called.append(1) or httpx.Response(400, json={}))
        rep.record(ACCOUNT, candidate_id=CANDIDATE,
                   direction=DIRECTION_INBOUND, message_id="m1")
        self.assertEqual(await rep.flush(), 0)
        self.assertEqual(self.store.count_message_events(), 0)
        self.assertEqual(len(called), 1)

    async def test_server_error_stops_the_batch_at_the_failure(self) -> None:
        """后端不通时别把剩下几十条一条条撞过去——停在那条，下趟从它继续。"""
        called: list[int] = []
        rep = reporter(self.store,
                       lambda _r: called.append(1) or httpx.Response(503, json={}))
        for index in range(3):
            rep.record(ACCOUNT, candidate_id=CANDIDATE,
                       direction=DIRECTION_INBOUND, message_id=f"m{index}")
        self.assertEqual(await rep.flush(), 0)
        self.assertEqual(len(called), 1)
        self.assertEqual(self.store.count_message_events(), 3)

    async def test_gives_up_after_too_many_attempts(self) -> None:
        rep = reporter(self.store, lambda _r: httpx.Response(503, json={}))
        rep.record(ACCOUNT, candidate_id=CANDIDATE,
                   direction=DIRECTION_INBOUND, message_id="m1")
        for _ in range(MAX_ATTEMPTS):
            await rep.flush()
        self.assertEqual(self.store.count_message_events(), 0)

    async def test_stale_events_are_purged_with_a_trace(self) -> None:
        """隔了一天才报上去，后端算出来的跟进时点早过了。清掉——但不是静悄悄地清。"""
        now = T0
        rep = reporter(self.store, lambda _r: httpx.Response(200, json={}),
                       clock=lambda: now)
        rep.record(ACCOUNT, candidate_id=CANDIDATE,
                   direction=DIRECTION_INBOUND, message_id="m1")
        now = T0 + timedelta(hours=MAX_AGE_HOURS + 1)
        with capture_logs() as logs:
            self.assertEqual(await rep.flush(), 0)
        self.assertEqual(self.store.count_message_events(), 0)
        # 清掉必须留痕：静悄悄丢事件和静悄悄丢消息是同一种病。
        self.assertTrue(any(line["log_level"] == "warning" for line in logs), logs)


# ────────────────────────── 入站：候选人回话了 ──────────────────────────


class InboundReportingTests(StoreTestCase):
    """入站事件由 `InboundLoop` 在读到消息那一刻回调登记（`GatewayRunner._note_inbound_messages`）。"""

    def _loop(self, batches, on_peer_messages) -> InboundLoop:
        self._now = T0

        async def handler(*_args: Any) -> None:
            return None

        return InboundLoop(
            accounts=[ACCOUNT],
            source=FakeSource(batches),
            store=self.store,
            handler=handler,
            debounce_seconds=3.0,
            clock=lambda: self._now,
            on_peer_messages=on_peer_messages,
        )

    async def test_peer_messages_are_handed_over_once(self) -> None:
        seen: list[tuple[str, str, int]] = []
        loop = self._loop(
            [[msg("在吗", msg_id="m1"), msg("我发出去了", msg_id="m2", from_self=True)]],
            lambda account, peer, messages: seen.append((account, peer, len(messages))),
        )
        await loop.tick()
        # 只交对方说的那几条：专员自己手打的不算「候选人回复」。
        self.assertEqual(seen, [(ACCOUNT, PEER, 1)])

        await loop.tick()   # 同一条再读到一次（去重表兜住）
        self.assertEqual(len(seen), 1)

    async def test_rejection_is_still_reported_before_the_blacklist_return(self) -> None:
        """★ 「别再联系我了」也是一次回复，而那正是最不该再追一条的时刻。"""
        seen: list[str] = []
        loop = self._loop(
            [[msg("别再联系我了", msg_id="m1")]],
            lambda _a, _p, messages: seen.extend(m.text for m in messages),
        )
        await loop.tick()
        self.assertEqual(seen, ["别再联系我了"])
        self.assertTrue(self.store.is_blacklisted(ACCOUNT, PEER))

    async def test_listener_failure_never_stops_the_read_loop(self) -> None:
        def boom(*_args: Any) -> None:
            raise RuntimeError("发件箱坏了")

        loop = self._loop([[msg("在吗", msg_id="m1")]], boom)
        with capture_logs() as logs:
            await loop.tick()
        self.assertTrue(any(line["log_level"] == "warning" for line in logs), logs)
        # 消息照旧进了待处理表——旁听者出错不该让这批消息丢掉。
        self.assertEqual(len(self.store.list_pending_inbound()), 1)


class TakeoverLeavesTheReadSideAloneTests(InboundReportingTests):
    """091 · 接管期间**读侧零误伤**（docs/19 v2 §1.1）。

    接管的意思是「分身别自动发」，不是「分身别看了」。读侧一旦跟着停：
    - 入站事件不报 → 业务后端排不了「未回复跟进」，人交回来之后没人知道谁在等着；
    - 会话进不了待处理表 → 接管这段时间的话直接消失（连补投的机会都没有）。

    ★ 所以这一组全是「照常」断言。看着乏味，但它盯的正是最容易被顺手一起关掉的一层。

    ★ **故意继承** `InboundReportingTests`：父类那几条（入站事件交出去、拒绝也照报、
      旁听者出错不带停读循环）在接管状态下**原样再跑一遍**——「照常」这件事，
      与其重写断言，不如让同一份断言在接管中也成立。
    """

    def _loop(self, batches, on_peer_messages) -> InboundLoop:
        loop = super()._loop(batches, on_peer_messages)
        self.store.set_candidate_mode(ACCOUNT, PEER, "human_takeover", reason="我自己来跟")
        return loop

    async def test_the_batch_still_lands_in_the_pending_table(self) -> None:
        """★ 接管期间的消息照样进队列——恢复托管之后要能原样补投。"""
        loop = self._loop([[msg("在吗", msg_id="m1")]], lambda *_a: None)
        await loop.tick()
        self.assertEqual(len(self.store.list_pending_inbound()), 1)

    async def test_the_watchlist_loses_nobody(self) -> None:
        """★ 接管 ≠ 把人移出监听名单。名单退不回去，这一步做错就是不可逆的。"""
        loop = self._loop([[msg("在吗", msg_id="m1")]], lambda *_a: None)
        before = self.store.get_watchlist()
        await loop.tick()
        self.assertEqual(self.store.get_watchlist(), before)


# ────────────────────────── 出站：我方发出去了 ──────────────────────────


class OutboundReportingTests(StoreTestCase):
    def _sender(self, rep: MessageEventReporter | None) -> tuple[OutboundSender, FakeSink]:
        sink = FakeSink()
        return (
            OutboundSender(
                sink=sink,
                guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
                store=self.store,
                clock=lambda: T0,
                message_events=rep,
            ),
            sink,
        )

    def _card(self, segments: list[str]) -> dict[str, Any]:
        return {"type": "info", "body": {"kind": "wechat_reply", "reply_segments": segments}}

    async def test_每段回复各报一条(self) -> None:
        known_candidate(self.store)
        seen: list[dict[str, Any]] = []
        rep = reporter(self.store, accepting(seen))
        sender, _sink = self._sender(rep)

        result = await sender.deliver(ACCOUNT, PEER, self._card(["你好", "方便聊两句吗"]))
        self.assertEqual(len(result.sent), 2)
        self.assertEqual(await rep.flush(), 2)
        self.assertEqual([body["body"]["direction"] for body in seen], ["outbound"] * 2)
        self.assertEqual({body["body"]["candidateId"] for body in seen}, {CANDIDATE})
        self.assertEqual({body["body"]["status"] for body in seen}, {"sent"})

    async def test_归不了位就不报(self) -> None:
        """本机不知道这个会话是哪个候选人 → 如实跳过，绝不编一个 ID 顶上。"""
        rep = reporter(self.store, lambda _r: httpx.Response(200, json={}))
        sender, _sink = self._sender(rep)
        await sender.deliver(ACCOUNT, PEER, self._card(["你好"]))
        self.assertEqual(self.store.count_message_events(), 0)

    async def test_一条都没发出去就不报(self) -> None:
        known_candidate(self.store)
        rep = reporter(self.store, lambda _r: httpx.Response(200, json={}))
        sender, _sink = self._sender(rep)
        await sender.deliver(ACCOUNT, PEER, self._card([]))
        self.assertEqual(self.store.count_message_events(), 0)


# ────────────────────────── 指令：计划驱动那条路 ──────────────────────────


class FakeRuntime:
    """`submit(wait=True)` 会一直等到任务进终态——终态里既有 succeeded 也有 failed。"""

    def __init__(self, status: str = "succeeded") -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self._status = status

    async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
        self.calls.append((task_type, dict(payload or {})))
        return {"task_id": f"t{len(self.calls)}", "status": self._status, "result": {"ok": True}}


class CommandReportingTests(StoreTestCase):
    def _executor(
        self, rep: MessageEventReporter | None, *, allow_real: bool = True,
        status: str = "succeeded",
    ) -> tuple[CommandExecutor, FakeRuntime]:
        runtime = FakeRuntime(status)
        return (
            CommandExecutor(
                runtime=runtime,
                store=self.store,
                guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
                account_id=ACCOUNT,
                allow_real_actions=allow_real,
                confirmation="EXECUTE_REAL_ACTION",
                clock=lambda: T0,
                message_events=rep,
            ),
            runtime,
        )

    def _command(self) -> dict[str, Any]:
        return {"command_id": "cmd-1", "op": "send_message",
                "args": {"to": PEER, "text": "在吗，方便聊两句"}}

    async def test_带上原指令ID(self) -> None:
        known_candidate(self.store)
        seen: list[dict[str, Any]] = []
        rep = reporter(self.store, accepting(seen))
        executor, _runtime = self._executor(rep)

        result = await executor.execute(self._command())
        self.assertEqual(result.status, "done")
        self.assertEqual(await rep.flush(), 1)
        self.assertEqual(seen[0]["body"]["commandId"], "cmd-1")
        self.assertEqual(seen[0]["body"]["direction"], "outbound")
        self.assertEqual(seen[0]["body"]["messageType"], "text")

    async def test_任务没确认成功就不报(self) -> None:
        """★ 「没抛异常」≠「话发出去了」。报一条假的已发送，跟进就被推后一整个周期。"""
        known_candidate(self.store)
        rep = reporter(self.store, lambda _r: httpx.Response(200, json={}))
        executor, _runtime = self._executor(rep, status="failed")
        await executor.execute(self._command())
        self.assertEqual(self.store.count_message_events(), 0)

    async def test_只走流程不真发就不报(self) -> None:
        known_candidate(self.store)
        rep = reporter(self.store, lambda _r: httpx.Response(200, json={}))
        executor, _runtime = self._executor(rep, allow_real=False)
        await executor.execute(self._command())
        self.assertEqual(self.store.count_message_events(), 0)

    async def test_读类指令什么都不报(self) -> None:
        known_candidate(self.store)
        rep = reporter(self.store, lambda _r: httpx.Response(200, json={}))
        executor, _runtime = self._executor(rep)
        await executor.execute(
            {"command_id": "cmd-2", "op": "read_chat_history", "args": {"friend": PEER}}
        )
        self.assertEqual(self.store.count_message_events(), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
