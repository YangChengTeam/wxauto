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
from wxclient.gateway.capabilities import (
    CAP_PRIVATE_MESSAGE,
    CAP_READ,
    CAPABILITIES_OP,
    Capability,
)

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
