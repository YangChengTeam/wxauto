"""断线重连后必须重新自报能力表——真 runner + 真 WS + 假 AI 端。

★ 这条曾经是生产哑火的根：AI 端的「网关册子」是按**连接**记的（连接一关就注销），
而自报能力表是这台机器入册的唯一途径（业务端已不再签 `scope=wechat` 的票）。
`_report_capabilities` 原来只比内容「变了才报」，于是断线重连后内容一样 → 一帧都不报
→ 新连接永远不入册 → 之后每条微信指令都死在 `no gateway`，而两边日志都显示「已连接」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fake_agent_ws import FakeAgentServer

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    LocalFileTicketProvider,
)
from wxclient.gateway.agent_status import ReportOutcome
from wxclient.gateway.capabilities import (
    CAP_PRIVATE_MESSAGE,
    CAP_READ,
    CAPABILITIES_OP,
    Capability,
)
from wxclient.gateway.commands import CONTEXT_MODE_UPLINK_OP

T0 = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"


class SilentSource:
    async def fetch(self, account_id: str) -> list:
        return []


class SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("上报能力不该往微信发任何东西")


class CapabilityReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.caps = {
            CAP_PRIVATE_MESSAGE: Capability(key=CAP_PRIVATE_MESSAGE, available=True),
            CAP_READ: Capability(key=CAP_READ, available=True),
        }

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, server: FakeAgentServer) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(
                ai_ws_url=server.url,
                local_ticket_file=self.tickets,
                turn_timeout_seconds=10.0,
                active_hours="00:00-23:59",
            ),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=SilentSource(),
            sink=SilentSink(),
            accounts=[ACCOUNT],
            clock=lambda: T0,
            capabilities=lambda: self.caps,
        )

    @staticmethod
    def _reports(server: FakeAgentServer) -> list[dict]:
        return [f.get("data") or {} for f in server.received if f.get("op") == CAPABILITIES_OP]

    async def _sync(self, runner: GatewayRunner) -> None:
        """等假 AI 端把已发出的帧都读完：上报是不等回执的，ping 有回执、且 WS 保序。"""
        client = await runner._pool.acquire(ACCOUNT)
        await client.ping()

    async def test_it_reports_again_after_a_reconnect(self) -> None:
        """★ 内容一模一样也要报——新连接在 AI 端是个陌生人。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await self._sync(runner)
                self.assertEqual(len(self._reports(server)), 1, "建连后应报一次")

                await runner._pool.invalidate(ACCOUNT)  # = ping 不通时走的那条路
                await runner.ensure_connections()
                await self._sync(runner)

                reports = self._reports(server)
                self.assertEqual(len(reports), 2, "重连后必须重报，否则网关不入册")
                self.assertEqual(reports[0], reports[1])
            finally:
                await runner.stop()

    async def test_it_stays_quiet_on_the_same_connection(self) -> None:
        """同一条连接上内容没变就别刷屏——保活 30s 一跳，报一次就够。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await runner.ensure_connections()
                await runner.ensure_connections()
                await self._sync(runner)
                self.assertEqual(len(self._reports(server)), 1)
            finally:
                await runner.stop()

    async def test_it_reports_again_when_the_capabilities_change(self) -> None:
        """微信被关掉 → 能力表变了 → 同一条连接上也要立刻重报。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                self.caps[CAP_PRIVATE_MESSAGE] = Capability(
                    key=CAP_PRIVATE_MESSAGE, available=False, reason="微信没登录",
                )
                await runner.ensure_connections()
                await self._sync(runner)

                reports = self._reports(server)
                self.assertEqual(len(reports), 2)
                self.assertFalse(reports[1]["capabilities"][CAP_PRIVATE_MESSAGE]["available"])
            finally:
                await runner.stop()


class ContextModeReportTests(CapabilityReportTests):
    """089 任务 13：重连之后把本机的接管状态也补报一次——同一次事故的第二条路。

    能力表补上了，接管状态没补，结果一样荒唐：机器上明明接管着（本机那道闸关得死死的），
    AI 端手里那份停在断线之前，照常调业务接口、照常算匹配推卡。
    """

    @staticmethod
    def _modes(server: FakeAgentServer) -> list[dict]:
        return [
            f.get("data") or {}
            for f in server.received
            if f.get("op") == CONTEXT_MODE_UPLINK_OP
        ]

    async def test_a_fresh_machine_reports_nothing(self) -> None:
        """★ 从没设过就一条都不报：默认是分身在管，平白报一条可能盖掉企业端刚下的接管。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await self._sync(runner)
                self.assertEqual(self._modes(server), [])
            finally:
                await runner.stop()

    async def test_a_local_takeover_is_reported_again_after_a_reconnect(self) -> None:
        """★ 这一条丢了最贵：本机闸关着、AI 端那半边却是开的。"""
        self.store.set_context_mode(
            ACCOUNT, "take_over", reason="我自己来跟这批人", version=7,
            reported_at=T0.isoformat(),
        )
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await self._sync(runner)
                self.assertEqual(len(self._modes(server)), 1)

                await runner._pool.invalidate(ACCOUNT)
                await runner.ensure_connections()
                await self._sync(runner)

                modes = self._modes(server)
                self.assertEqual(len(modes), 2, "重连后必须重报")
                self.assertEqual(modes[-1]["context_mode"], "take_over")
                self.assertEqual(modes[-1]["reason"], "我自己来跟这批人")
                self.assertEqual(modes[-1]["version"], 7, "带上本机手里那个版本号，好让 AI 端认出过期的一份")
            finally:
                await runner.stop()

    async def test_an_offline_resume_is_reported_too(self) -> None:
        """离线时按下的「交回分身」也要补报——它欠着一次上报（`reported_at` 为空）。

        不报的话，AI 端会一直停在接管里跑静默轮：人已经把机器交回来了，分身却还哑着。
        """
        self.store.set_context_mode(ACCOUNT, "delegated", version=7)
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await self._sync(runner)
                modes = self._modes(server)
                self.assertEqual(len(modes), 1)
                self.assertEqual(modes[0]["context_mode"], "delegated")
            finally:
                await runner.stop()

    async def test_a_settled_delegated_state_is_not_reported(self) -> None:
        """已经报过的 `delegated` 不再重复报：默认值一条都不该在重连时抢话。"""
        self.store.set_context_mode(
            ACCOUNT, "delegated", version=7, reported_at=T0.isoformat(),
        )
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await self._sync(runner)
                self.assertEqual(self._modes(server), [])
            finally:
                await runner.stop()

    async def test_the_same_connection_is_not_spammed(self) -> None:
        """同一条连接上保活一跳一跳地过，接管状态没变就别刷屏。"""
        self.store.set_context_mode(ACCOUNT, "take_over", reason="我来", version=7)
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await runner.ensure_connections()
                await runner.ensure_connections()
                await self._sync(runner)
                self.assertEqual(len(self._modes(server)), 1)
            finally:
                await runner.stop()


class FakeReporter:
    """假的业务端上报口。默认报成功；`outcome` 换成别的就走那一档。"""

    def __init__(self, outcome: ReportOutcome | None = None) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.outcome = outcome or ReportOutcome(True)

    async def report(self, mode: str, *, reason: str = "", expected_version: int = 0,
                     **_kw: object) -> ReportOutcome:
        self.calls.append((mode, reason, expected_version))
        return self.outcome


class SwitchAndAlignTests(CapabilityReportTests):
    """089 Phase 2/5 · 界面按下开关（`set_context_mode`）与启动对齐（`align_context_mode`）。

    ★ 全组盯同一条：**先落本机、上报失败不回滚**，唯独 409 例外。
    """

    def _runner(self, server: FakeAgentServer, **kwargs) -> GatewayRunner:  # type: ignore[override]
        runner = super()._runner(server)
        runner._agent_status = kwargs.get("agent_status", FakeReporter())
        runner._login_context_mode = kwargs.get("login_context_mode")
        return runner

    async def test_pressing_it_lands_locally_before_anything_is_reported(self) -> None:
        reporter = FakeReporter()
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, agent_status=reporter)
            try:
                result = await runner.set_context_mode("take_over", reason="我本人跟进")
            finally:
                await runner.stop()
        row = self.store.context_mode(ACCOUNT)
        self.assertTrue(row.taken_over)
        self.assertEqual(result["context_mode"], "take_over")
        self.assertTrue(result["synced"])
        self.assertIsNotNone(row.reported_at, "报成了就把欠账销掉")
        self.assertEqual(reporter.calls, [("take_over", "我本人跟进", 0)])

    async def test_a_failed_report_leaves_the_local_switch_on(self) -> None:
        """★★ 最贵的一条：「我明明点了接管，分身还在发消息」。"""
        reporter = FakeReporter(ReportOutcome(False, reason="业务接口没回应"))
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, agent_status=reporter)
            try:
                result = await runner.set_context_mode("take_over", reason="我来")
            finally:
                await runner.stop()
        self.assertTrue(self.store.context_mode(ACCOUNT).taken_over, "报不出去 ≠ 没接管")
        self.assertFalse(result["synced"])
        self.assertIn("业务接口", result["note"])
        self.assertIsNone(self.store.context_mode(ACCOUNT).reported_at, "欠着，下次补报")

    async def test_a_conflict_rolls_back_to_what_the_backend_says(self) -> None:
        """★ 唯一该回滚的一档：409 带着当前真值。"""
        reporter = FakeReporter(ReportOutcome(False, rollback_to="delegated",
                                              rollback_version=9, reason="已被企业端改过"))
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, agent_status=reporter)
            try:
                result = await runner.set_context_mode("take_over", reason="我来")
            finally:
                await runner.stop()
        row = self.store.context_mode(ACCOUNT)
        self.assertEqual(row.context_mode, "delegated")
        self.assertEqual(row.version, 9)
        self.assertFalse(result["synced"])
        self.assertEqual(result["context_mode"], "delegated", "界面要显示业务端那份真值")

    async def test_the_switch_is_reported_to_the_ai_side_too(self) -> None:
        """AI 端那半边的闸也得跟着关——本机闸拦不住「AI 直接去调业务接口」那条路。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                await runner.ensure_connections()
                await runner.set_context_mode("take_over", reason="我来")
                await self._sync(runner)
                modes = [
                    f.get("data") or {}
                    for f in server.received
                    if f.get("op") == CONTEXT_MODE_UPLINK_OP
                ]
            finally:
                await runner.stop()
        self.assertEqual(modes[-1]["context_mode"], "take_over")

    async def test_startup_reports_what_is_still_owed(self) -> None:
        """★ 无心跳契约：没报上去的那一次，重启之后要**补报一次**——服务端不会自己来问。"""
        self.store.set_context_mode(ACCOUNT, "take_over", reason="昨天下午接的")
        reporter = FakeReporter()
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, agent_status=reporter)
            try:
                await runner.align_context_mode()
            finally:
                await runner.stop()
        self.assertEqual(reporter.calls, [("take_over", "昨天下午接的", 0)])
        self.assertIsNotNone(self.store.context_mode(ACCOUNT).reported_at)

    async def test_startup_aligns_with_the_login_response(self) -> None:
        """没有欠账时按登录响应对齐（零新接口，docs/19 §2.3）。"""
        self.store.set_context_mode(ACCOUNT, "delegated", version=3,
                                    reported_at=T0.isoformat())
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, login_context_mode=lambda: ("take_over", 4))
            try:
                await runner.align_context_mode()
            finally:
                await runner.stop()
        self.assertTrue(self.store.context_mode(ACCOUNT).taken_over)

    async def test_an_older_login_value_does_not_undo_a_local_switch(self) -> None:
        """★ 登录响应可能比本机旧（刚按完就重启）。同版/更旧一律不动——更晚发生的是本机那次。"""
        self.store.set_context_mode(ACCOUNT, "take_over", reason="我来", version=4,
                                    reported_at=T0.isoformat())
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, login_context_mode=lambda: ("delegated", 4))
            try:
                await runner.align_context_mode()
            finally:
                await runner.stop()
        self.assertTrue(self.store.context_mode(ACCOUNT).taken_over)

    async def test_owing_a_report_wins_over_the_login_value(self) -> None:
        """两件事有先后：欠着的那次是**本机的意图**，先把它送出去，别被登录那份顺手盖掉。"""
        self.store.set_context_mode(ACCOUNT, "take_over", reason="刚按的")
        reporter = FakeReporter()
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, agent_status=reporter,
                                  login_context_mode=lambda: ("delegated", 99))
            try:
                await runner.align_context_mode()
            finally:
                await runner.stop()
        self.assertTrue(self.store.context_mode(ACCOUNT).taken_over)
        self.assertEqual(len(reporter.calls), 1)

    async def test_aligning_twice_only_does_it_once(self) -> None:
        self.store.set_context_mode(ACCOUNT, "take_over", reason="我来")
        reporter = FakeReporter()
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server, agent_status=reporter)
            try:
                await runner.align_context_mode()
                await runner.align_context_mode()
            finally:
                await runner.stop()
        self.assertEqual(len(reporter.calls), 1)

    async def test_the_status_reader_tells_both_halves_apart(self) -> None:
        """`(归谁用, 报出去了没有)`：界面靠后一半决定要不要标「未同步」。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            try:
                self.assertEqual(runner.context_mode(), ("delegated", True))
                self.store.set_context_mode(ACCOUNT, "take_over", reason="我来")
                self.assertEqual(runner.context_mode(), ("take_over", False))
                self.store.mark_context_mode_reported(ACCOUNT, T0.isoformat())
                self.assertEqual(runner.context_mode(), ("take_over", True))
            finally:
                await runner.stop()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
