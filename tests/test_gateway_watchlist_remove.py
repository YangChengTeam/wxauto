"""把会话移出监听名单——真 runner + 真 WS + 假 AI 端。

断言的是**整条路**：桌面端发出去的是哪个 op、AI 端删完推回来的新名单有没有真的落到
本机这份上。后者正是「界面会不会更新」的根：界面读的就是 `runner.watched_peers()`。

★ 为什么删除必须发生在 AI 端（069）：名单权威在那儿，推一次覆盖一次——本机把行抹掉
只能撑到下一次推送，删了立刻又回来，用户看到的是「删不掉」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fake_agent_ws import FakeAgentServer

from wxclient.gateway import (
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    LocalFileTicketProvider,
    Watchlist,
)

T0 = datetime(2026, 8, 17, 14, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"


class SilentSource:
    async def fetch(self, account_id: str) -> list:
        return []


class SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("改名单不该往微信发任何东西")


class WatchlistRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = GatewayStore(root / "gateway.db")
        self.tickets = root / "tickets.json"
        self.tickets.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.watchlist = Watchlist()

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
            watchlist=self.watchlist,
        )

    def _removes(self, server: FakeAgentServer) -> list[dict[str, Any]]:
        return [f.get("data") or {}
                for f in server.received if f.get("op") == "wechat.watchlist.remove"]

    async def test_it_asks_the_ai_side_and_takes_the_new_list_from_it(self) -> None:
        """★ 桌面端只发意图；名单以 AI 端推回来的那一份为准。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            server.watchlist = {"候选人-王建国", "普工招聘群"}
            runner = self._runner(server)
            # 先让 AI 端把当前名单推下来（真实链路里握手完就推）
            self.watchlist.apply_push({"only": sorted(server.watchlist), "never": []})

            result = await runner.remove_watched("候选人-王建国")

            self.assertEqual(self._removes(server), [{"names": ["候选人-王建国"]}])
            self.assertEqual(result["removed"], ["候选人-王建国"])
            self.assertEqual(self.watchlist.watched(), ["普工招聘群"],
                             "★ AI 端推回来的新名单要真的落到本机——界面读的就是这份")

    async def test_removing_someone_not_on_the_list_says_so(self) -> None:
        """AI 端说「他本来就不在名单里」时，如实返回空的 removed，本机名单一个不动。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            server.watchlist = {"普工招聘群"}
            runner = self._runner(server)
            self.watchlist.apply_push({"only": ["普工招聘群"], "never": []})

            result = await runner.remove_watched("查无此人")

            self.assertEqual(result["removed"], [])
            self.assertEqual(self.watchlist.watched(), ["普工招聘群"])


if __name__ == "__main__":  # pragma: no cover - 手动跑单个文件时用
    unittest.main()
