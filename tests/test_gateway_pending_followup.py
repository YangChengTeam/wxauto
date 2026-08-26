"""「跑完一轮」≠「把话说出去了」——一条都没发出去时，这批消息该留还是该销（审计 P0-4/P0-8）。

背景：待处理表是候选人消息的唯一保险绳。原来的销账条件是**「handler 没抛异常」**，
于是两条常见的「跑完了但一条都没发」的路——AI 端返回错误、被守门拦下（额度用完 /
不在时间窗）——都被当成成功：批次删掉、状态栏的积压数归零，候选人的话就此消失，
现场看不出丢过东西。这里把该留的留住、该销的销掉，并且分清两种「留」：

- ``retry``（AI 端报错）计一次失败，有上限，最终会放弃并留痕；
- ``defer``（额度/时间窗）**不计**失败——它一定会自己过去，拿重试次数去消耗一段
  本来就该等的时间，等额度回来时批次已经被放弃了。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fake_agent_ws import FakeAgentServer, TurnScript
from test_gateway_business import (
    T0,
    FakeSink,
    FakeSource,
    StoreTestCase,
    msg,
    reply_card,
)

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    InboundLoop,
    LocalFileTicketProvider,
    OutboundGuard,
    OutboundSender,
)
from wxclient.gateway.inbound import (
    FOLLOW_UP_DEFER,
    FOLLOW_UP_RETRY,
    PENDING_MAX_ATTEMPTS,
    InboundMessage,
)
from wxclient.gateway.outbound import DeliveryResult

ACCOUNT = "acct-1"
PEER = "peer-1"


class FollowUpLoopTestCase(StoreTestCase):
    def _loop(self, source, handler, *, now: datetime, debounce: float = 3.0) -> InboundLoop:
        self._now = now
        return InboundLoop(
            accounts=[ACCOUNT],
            source=source,
            store=self.store,
            handler=handler,
            debounce_seconds=debounce,
            clock=lambda: self._now,
        )

    async def _dispatch_once(self, loop: InboundLoop) -> None:
        """读一趟 + 等去抖过 + 派发一趟。"""
        await loop.tick()
        self._now += timedelta(seconds=10)
        await loop.tick()
        await loop.drain()

    def _pending(self) -> list[dict]:
        return self.store.list_pending_inbound()


class GuardDenialKeepsTheBatchTests(FollowUpLoopTestCase):
    """守门拦下 = 一条都没发出去。额度会恢复、时间窗会到，**别把话丢了**。"""

    def _sender(self, **guard_kwargs: Any) -> tuple[OutboundSender, FakeSink]:
        sink = FakeSink()
        guard_kwargs.setdefault("active_hours", "00:00-23:59")
        return (
            OutboundSender(
                sink=sink,
                guard=OutboundGuard(self.store, **guard_kwargs),
                store=self.store,
                clock=lambda: self._now,
            ),
            sink,
        )

    async def test_quota_exhausted_defers_without_burning_the_retry_budget(self) -> None:
        """★ 额度用完 → 留着重投，且**不计失败**。

        计成失败的话，重试次数会在等额度的这一小时里被耗光，等额度回来批次已经被放弃——
        表现和「消息丢了」一模一样，只是多了一行日志。
        """
        self._now = T0
        sender, sink = self._sender(max_sends_per_hour=1)
        # 先把这一小时的额度用掉。
        await sender.deliver(ACCOUNT, "someone-else", reply_card(["先占位"]))

        results: list[DeliveryResult] = []

        async def handler(account_id: str, peer_id: str, _t: str, _m) -> DeliveryResult:
            result = await sender.deliver(account_id, peer_id, reply_card(["您好"]))
            results.append(result)
            return result

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")], [], []]), handler, now=T0)
        await self._dispatch_once(loop)

        self.assertEqual(len(sink.sent), 1, "只有占位那条，候选人这条被守门拦下了")
        self.assertEqual(results[-1].follow_up, FOLLOW_UP_DEFER, "额度会恢复，属于「等一等」")
        rows = self._pending()
        self.assertEqual(len(rows), 1, "被拦下 ≠ 处理完，这批必须留着")
        self.assertEqual(rows[0]["attempts"], 0, "等额度不该算失败")

    async def test_deferred_batch_lands_once_the_quota_frees_up(self) -> None:
        """留着不是目的——额度恢复的下一趟要真的把话说出去。"""
        self._now = T0
        sender, sink = self._sender(max_sends_per_hour=1)
        await sender.deliver(ACCOUNT, "someone-else", reply_card(["先占位"]))

        async def handler(account_id: str, peer_id: str, _t: str, _m) -> DeliveryResult:
            return await sender.deliver(account_id, peer_id, reply_card(["您好"]))

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")], [], [], []]), handler, now=T0)
        await self._dispatch_once(loop)
        self.assertEqual(len(self._pending()), 1)

        self._now = T0 + timedelta(hours=2)   # 上一小时的额度滑出窗口
        await loop.tick()
        await loop.drain()
        self.assertEqual([text for _a, _p, text in sink.sent][-1], "您好")
        self.assertEqual(self._pending(), [], "发出去了才销账")

    async def test_blacklisted_peer_is_settled_not_retried(self) -> None:
        """★ 拉黑是**结论**，不是暂时挡一下：留着只会每趟重投到过期，白占队列。"""
        self._now = T0
        sender, _sink = self._sender()
        self.store.blacklist(ACCOUNT, PEER, reason="明确拒绝")

        async def handler(account_id: str, peer_id: str, _t: str, _m) -> DeliveryResult:
            return await sender.deliver(account_id, peer_id, reply_card(["您好"]))

        loop = self._loop(FakeSource([[msg("别烦我", msg_id="m1")], []]), handler, now=T0)
        await self._dispatch_once(loop)
        self.assertEqual(self._pending(), [])

    async def test_retry_still_gives_up_eventually(self) -> None:
        """`retry` 那一档必须还受重试上限管着，否则就是无限原地打转。"""
        calls = 0

        async def handler(_a: str, _p: str, _t: str, _m) -> DeliveryResult:
            nonlocal calls
            calls += 1
            return DeliveryResult(skipped_reason="AI 端没结果", follow_up=FOLLOW_UP_RETRY)

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")]] + [[]] * 20), handler, now=T0)
        for _ in range(PENDING_MAX_ATTEMPTS + 2):
            await self._dispatch_once(loop)
        self.assertEqual(self._pending(), [], "重试到上限要放弃")
        self.assertLessEqual(calls, PENDING_MAX_ATTEMPTS + 1)


class TakeoverDefersInsteadOfSettlingTests(GuardDenialKeepsTheBatchTests):
    """091 · 接管两小时 ≠ 丢这个人（docs/19 v2 §1）。

    接管期间消息**照读**（US3），但一条都不自动回。这一批要跟额度用完时走同一条路：
    ``defer`` —— 留着、不计失败，恢复托管之后原样重投。

    ★ 如果接管走的是「销账」那一档，现场根本看不出丢过东西：状态栏积压数归零，
      候选人那几句话就此消失。这正是 [[settle-only-when-said]] 那条教训的直接应用。
    """

    async def test_a_taken_over_candidate_defers_the_batch(self) -> None:
        self._now = T0
        sender, sink = self._sender()
        self.store.set_candidate_mode(ACCOUNT, PEER, "human_takeover", reason="我自己来跟")

        results: list[DeliveryResult] = []

        async def handler(account_id: str, peer_id: str, _t: str, _m) -> DeliveryResult:
            result = await sender.deliver(account_id, peer_id, reply_card(["您好"]))
            results.append(result)
            return result

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")], [], []]), handler, now=T0)
        await self._dispatch_once(loop)

        self.assertEqual(sink.sent, [], "接管中一条都不许自动发")
        self.assertEqual(results[-1].follow_up, FOLLOW_UP_DEFER, "接管会结束，属于「等一等」")
        rows = self._pending()
        self.assertEqual(len(rows), 1, "被接管拦下 ≠ 处理完")
        self.assertEqual(rows[0]["attempts"], 0, "★ 等人干完活不该算失败：算了就会在接管期间把重试次数耗光")

    async def test_the_batch_lands_after_the_candidate_is_handed_back(self) -> None:
        """★ 留着不是目的：交回分身之后的下一趟要把**同一批**话真的说出去。"""
        self._now = T0
        sender, sink = self._sender()
        self.store.set_candidate_mode(ACCOUNT, PEER, "human_takeover", reason="我自己来跟")

        async def handler(account_id: str, peer_id: str, _t: str, _m) -> DeliveryResult:
            return await sender.deliver(account_id, peer_id, reply_card(["您好"]))

        loop = self._loop(FakeSource([[msg("在吗", msg_id="m1")], [], [], []]), handler, now=T0)
        await self._dispatch_once(loop)
        self.assertEqual(len(self._pending()), 1)

        self.store.set_candidate_mode(ACCOUNT, PEER, "ai_managed")
        self._now += timedelta(minutes=5)
        await loop.tick()
        await loop.drain()

        self.assertEqual([text for _a, _p, text in sink.sent], ["您好"])
        self.assertEqual(self._pending(), [], "发出去了才销账")

    async def test_a_long_takeover_never_burns_the_retry_budget(self) -> None:
        """接管一上午（远超重试上限的趟数）之后，这批人一个都不能少。"""
        self._now = T0
        sender, sink = self._sender()
        self.store.set_candidate_mode(ACCOUNT, PEER, "human_takeover", reason="上午都我来")

        async def handler(account_id: str, peer_id: str, _t: str, _m) -> DeliveryResult:
            return await sender.deliver(account_id, peer_id, reply_card(["您好"]))

        loop = self._loop(
            FakeSource([[msg("在吗", msg_id="m1")]] + [[]] * 40), handler, now=T0
        )
        for _ in range(PENDING_MAX_ATTEMPTS + 2):
            await self._dispatch_once(loop)
        self.assertEqual(len(self._pending()), 1, "★ 接管久一点就把人丢了 = 最贵的那种丢")

        self.store.set_candidate_mode(ACCOUNT, PEER, "ai_managed")
        await loop.tick()
        await loop.drain()
        self.assertEqual([text for _a, _p, text in sink.sent], ["您好"])


class InflightBatchIsNotReloadedTests(FollowUpLoopTestCase):
    """★ 正在跑的批次不能被重新装载（审计 P0-8）。

    在途的那一批**既不在缓冲里、也还没销账**——`_reload_pending` 只看这两处，就会把
    同一批又装回缓冲；等这一轮收口，缓冲里那份再跑一遍，候选人收到两条一样的回复。
    """

    async def test_a_running_batch_is_not_picked_up_again(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        transcripts: list[str] = []

        async def handler(_a: str, _p: str, transcript: str, _m) -> None:
            transcripts.append(transcript)
            started.set()
            await release.wait()

        loop = self._loop(FakeSource([[msg("我想应聘", msg_id="m1")], [], [], []]), handler, now=T0)
        await loop.tick()
        self._now = T0 + timedelta(seconds=10)
        await loop.tick()                      # 派发；handler 卡住不返回
        await asyncio.wait_for(started.wait(), timeout=2)

        self._now = T0 + timedelta(seconds=20)
        await loop.tick()                      # ★ 在途期间又来一趟
        release.set()
        await loop.drain()

        self._now = T0 + timedelta(seconds=40)
        await loop.tick()                      # 收口之后再来一趟
        await loop.drain()

        self.assertEqual(len(transcripts), 1, f"同一批只该跑一次：{transcripts}")
        self.assertEqual(self._pending(), [])


class AiErrorKeepsTheBatchTests(unittest.IsolatedAsyncioTestCase):
    """走真 runner + 真 WS + 假 AI 端：AI 端返回 error 时，这一轮**没有结论**。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets_file = root / "tickets.json"
        self.tickets_file.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.sink = FakeSink()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, server: FakeAgentServer) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(
                ai_ws_url=server.url,
                local_ticket_file=self.tickets_file,
                turn_timeout_seconds=10.0,
                active_hours="00:00-23:59",
            ),
            ticket_provider=LocalFileTicketProvider(self.tickets_file),
            store=self.store,
            source=type("Silent", (), {"fetch": staticmethod(lambda _a: _empty())})(),
            sink=self.sink,
            accounts=[ACCOUNT],
            clock=lambda: T0,
        )

    async def test_agent_error_asks_for_a_retry_instead_of_settling(self) -> None:
        script = TurnScript(error={"code": "AGENT_UNAVAILABLE", "message": "上游忙"})
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            runner = self._runner(server)
            try:
                result = await runner.handle_turn(
                    ACCOUNT, PEER, "在吗",
                    [InboundMessage(ACCOUNT, PEER, "m1", "在吗", T0)],
                )
            finally:
                await runner.stop()

        self.assertEqual(self.sink.sent, [], "AI 端报错时一条微信都不该发")
        self.assertEqual(
            result.follow_up, FOLLOW_UP_RETRY,
            "AI 端报错 = 这一轮没结论；当成成功就把候选人的消息永久丢了",
        )

    async def test_a_normal_turn_settles_the_batch(self) -> None:
        """对照组：正常发出去的那一轮不带 `follow_up`，照常销账。"""
        script = TurnScript(cards=[reply_card(["您好，这边在招普工"])])
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            runner = self._runner(server)
            try:
                result = await runner.handle_turn(
                    ACCOUNT, PEER, "在吗",
                    [InboundMessage(ACCOUNT, PEER, "m1", "在吗", T0)],
                )
            finally:
                await runner.stop()

        self.assertEqual([text for _a, _p, text in self.sink.sent], ["您好，这边在招普工"])
        self.assertEqual(result.follow_up, "")


async def _empty() -> list:
    return []


if __name__ == "__main__":
    unittest.main()
