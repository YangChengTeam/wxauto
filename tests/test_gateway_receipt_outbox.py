"""指令回执发件箱：**终态回执一条都不许丢**。

回执是 AI 端「这条计划项到底发出去没有」的唯一凭据（`docs/17 §5`：不要拿 `done` 当凭据，
等 `wechat_command_result`）。原来 `_report` 发不出去只写一行日志、没人再补，后果是一条
完整的链断在最后一米：

  指令投到网关 → 微信真发了 → 回执发不出去（连接抖一下） → AI 端那条指令一直挂在在途表
  → 业务后端等到 180s 超时，拿**同一个** trigger_id 重投 → 被幂等挡掉「这一条已经做过了」
  → 这条计划项**永远发不出去，且两边都以为做过了**。

所以先证坏路被堵：断网不丢、进程被杀不丢、补发不重复、过期丢弃要留痕、没执行器/读不懂
的帧也得回话；再证好路照旧：正常连着时发完就不留底。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fake_agent_ws import FakeAgentServer
from structlog.testing import capture_logs

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    LocalFileTicketProvider,
)
from wxclient.gateway.commands import RESULT_OP

T0 = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"


class SilentSource:
    async def fetch(self, account_id: str) -> list:
        return []


class SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("补发回执不该往微信发任何东西")


async def _settle(runner: GatewayRunner) -> None:
    """等 `_dispatch_commands` 派出去的那几个任务跑完（它是同步方法，任务是后台起的）。"""
    while runner._command_tasks:
        await asyncio.gather(*list(runner._command_tasks), return_exceptions=True)


class ReceiptOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.now = T0

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, url: str) -> GatewayRunner:
        return GatewayRunner(
            settings=GatewaySettings(
                ai_ws_url=url, local_ticket_file=self.tickets,
                turn_timeout_seconds=5.0, active_hours="00:00-23:59",
            ),
            ticket_provider=LocalFileTicketProvider(self.tickets),
            store=self.store,
            source=SilentSource(),
            sink=SilentSink(),
            accounts=[ACCOUNT],
            clock=lambda: self.now,
        )

    @staticmethod
    def _receipts(server: FakeAgentServer) -> list[dict]:
        return [f.get("data") or {} for f in server.received if f.get("op") == RESULT_OP]

    async def _sync(self, runner: GatewayRunner) -> None:
        """等假 AI 端把已发出的帧读完：回执不等回应，ping 等，而 WS 保序。"""
        client = await runner._pool.acquire(ACCOUNT)
        await client.ping()

    # ── 好路 ───────────────────────────────────────────────

    async def test_a_delivered_receipt_leaves_nothing_behind(self) -> None:
        """连着的时候发完就不留底——发件箱不是流水账，只压没送到的。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server.url)
            try:
                await runner._report(ACCOUNT, "cmd-1", "done", result={"sent": True})
                await self._sync(runner)
                self.assertEqual(len(self._receipts(server)), 1)
                self.assertEqual(self.store.count_receipts(), 0, "送到了就不该留底")
            finally:
                await runner._pool.close_all()

    async def test_intermediate_stages_are_not_queued(self) -> None:
        """★ 中间态发不掉就算了：它只刷卡片，隔几分钟才补上去反而是噪音。

        真正不能丢的是终态——那是销账凭据。
        """
        runner = self._runner("ws://127.0.0.1:1/v1/ws")   # 连不上
        try:
            await runner._report(ACCOUNT, "cmd-1", "received")
            await runner._report(ACCOUNT, "cmd-1", "started")
            self.assertEqual(self.store.count_receipts(), 0)

            await runner._report(ACCOUNT, "cmd-1", "done")
            self.assertEqual(self.store.count_receipts(), 1, "终态必须留底")
        finally:
            await runner._pool.close_all()

    # ── 坏路 ───────────────────────────────────────────────

    async def test_a_receipt_that_cannot_be_sent_is_kept_and_resent_later(self) -> None:
        """★ 这条就是整块东西的理由：发的那一刻连不上，恢复之后必须补上去。"""
        runner = self._runner("ws://127.0.0.1:1/v1/ws")
        await runner._report(ACCOUNT, "cmd-1", "done", result={"sent": True})
        self.assertEqual(self.store.count_receipts(), 1, "发不出去要留着")
        await runner._pool.close_all()

        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server.url)
            try:
                await runner._flush_receipts_once()
                await self._sync(runner)

                receipts = self._receipts(server)
                self.assertEqual(len(receipts), 1, "恢复后要补一条")
                self.assertEqual(receipts[0]["command_id"], "cmd-1")
                self.assertEqual(receipts[0]["status"], "done")
                self.assertEqual(receipts[0]["result"], {"sent": True}, "原样补，不是补个空壳")
                self.assertEqual(self.store.count_receipts(), 0, "补到了就销掉")
            finally:
                await runner._pool.close_all()

    async def test_a_queued_receipt_survives_a_restart(self) -> None:
        """★ 落盘早于发送：发到一半进程被杀，重启后这条还在。

        （这台电脑是专员自己的 Windows，关机、杀进程、装更新都是家常便饭。）
        """
        runner = self._runner("ws://127.0.0.1:1/v1/ws")
        await runner._report(ACCOUNT, "cmd-1", "failed", reason="微信没登录")
        await runner._pool.close_all()

        # 换一个 GatewayStore 实例 = 重新开库，模拟进程重启。
        reopened = GatewayStore(Path(self._tmp.name) / "gateway.db")
        rows = reopened.list_receipts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["reason"], "微信没登录")

    async def test_it_does_not_resend_forever_and_says_so_when_it_gives_up(self) -> None:
        """★ 压过 24h 就不再补——AI 端在途表也是 24h，那边已经忘了这条指令。

        但丢必须留痕：静悄悄丢回执和静悄悄丢消息是同一种病。
        """
        runner = self._runner("ws://127.0.0.1:1/v1/ws")
        await runner._report(ACCOUNT, "cmd-old", "done")
        await runner._pool.close_all()

        self.now = T0 + timedelta(hours=25)
        runner = self._runner("ws://127.0.0.1:1/v1/ws")
        try:
            with capture_logs() as logs:
                await runner._flush_receipts_once()
            self.assertEqual(self.store.count_receipts(), 0, "过期的要清掉")
            dropped = [entry for entry in logs if entry.get("event") == "回执过期未送达，已丢弃"]
            self.assertEqual(len(dropped), 1, "★ 丢了必须有一行日志说清楚丢的是哪条")
            self.assertEqual(dropped[0]["command_id"], "cmd-old")
        finally:
            await runner._pool.close_all()

    async def test_a_still_broken_connection_keeps_the_receipt_and_counts_the_try(self) -> None:
        """补发又没成 → 留着，并记一次失败次数（排查时看得出它试过几回）。"""
        runner = self._runner("ws://127.0.0.1:1/v1/ws")
        try:
            await runner._report(ACCOUNT, "cmd-1", "done")
            await runner._flush_receipts_once()

            rows = self.store.list_receipts()
            self.assertEqual(len(rows), 1, "没送到就不许销")
            self.assertEqual(rows[0]["attempts"], 1)
        finally:
            await runner._pool.close_all()

    async def test_one_command_keeps_one_row(self) -> None:
        """同一条指令的终态只有一个：重报覆盖，不攒成两行。"""
        runner = self._runner("ws://127.0.0.1:1/v1/ws")
        try:
            await runner._report(ACCOUNT, "cmd-1", "done")
            await runner._report(ACCOUNT, "cmd-1", "failed", reason="其实没发出去")

            rows = self.store.list_receipts()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["payload"]["status"], "failed")
        finally:
            await runner._pool.close_all()

    # ── 静默丢弃的两条路 ────────────────────────────────────

    async def test_a_command_with_no_executor_is_rejected_not_swallowed(self) -> None:
        """★ 没配执行器时原来一声不吭：AI 端会一直挂在在途表里等一个永远不来的回执。

        做不了就明说做不了（074：「做不了」和「不让做」给用户的话完全两样）。
        """
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server.url)          # command_executor=None
            try:
                runner._dispatch_commands(ACCOUNT, {
                    "command_id": "cmd-9", "op": "send_message",
                    "args": {"to": "张三", "text": "在吗"},
                })
                await _settle(runner)
                await self._sync(runner)

                receipts = self._receipts(server)
                self.assertEqual([r["status"] for r in receipts], ["rejected"])
                self.assertEqual(receipts[0]["command_id"], "cmd-9")
                self.assertIn("没有配指令执行器", receipts[0]["reason"])
            finally:
                await runner._pool.close_all()

    async def test_an_unreadable_frame_still_gets_an_answer(self) -> None:
        """帧里没有 op → 也回一条 rejected，别让 AI 端等到超时。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server.url)
            try:
                runner._dispatch_commands(ACCOUNT, {"command_id": "cmd-8", "args": {}})
                await _settle(runner)
                await self._sync(runner)

                receipts = self._receipts(server)
                self.assertEqual([r["status"] for r in receipts], ["rejected"])
                self.assertIn("缺少 op", receipts[0]["reason"])
            finally:
                await runner._pool.close_all()

    async def test_a_frame_without_a_command_id_is_not_answered(self) -> None:
        """连 command_id 都没有 → 无从回起（AI 端也挂不上在途表）。只留日志，不编一个 ID。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server.url)
            try:
                runner._dispatch_commands(ACCOUNT, {"args": {}})
                await _settle(runner)
                await self._sync(runner)

                self.assertEqual(self._receipts(server), [])
                self.assertEqual(self.store.count_receipts(), 0)
            finally:
                await runner._pool.close_all()


if __name__ == "__main__":
    unittest.main()
