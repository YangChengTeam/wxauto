"""把会话移出监听名单——真 runner + 真 WS + 假 AI 端。

界面读的就是 `runner.watched_peers()`，所以断言都落在本机这份名单上。

★ 移出在本机（2026-09-24 起）：名单权威在业务后端、业务端没有移出接口，AI 端也不再持有名单。
落进本机「不管」，同步只换 ``only``，人不会被带回来。
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


class _WatchlistCase(unittest.IsolatedAsyncioTestCase):
    """两组用例共用的装配：真 runner + 真 WS + 假 AI 端。

    ★ 别让「同步」那组去继承「移除」那组——继承一个**具体的** TestCase 会把它那几条
    用例原样再跑一遍（跑得慢，且失败时同名用例出现两次，查起来分不清是哪一组）。
    """

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


class WatchlistRemovalTests(_WatchlistCase):
    """★ 移出发生在**本机**（用户 2026-09-24）：名单权威 0.1.83 起在业务后端，AI 端手上没有这份名单，
    原来请 AI 端删永远回「本来就不在」，界面那一行怎么点都还在。"""

    async def test_it_removes_locally_without_asking_the_ai_side(self) -> None:
        saved: list[dict] = []
        self.watchlist = Watchlist(persist=lambda scope: saved.append(
            {"only": sorted(scope.only), "never": sorted(scope.never)}))
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            self.watchlist.apply_backend({"only": ["候选人-王建国", "普工招聘群"]})

            result = await runner.remove_watched("候选人-王建国")

            self.assertEqual(result["removed"], ["候选人-王建国"])
            self.assertEqual(self.watchlist.watched(), ["普工招聘群"], "★ 界面读的就是这份")
            self.assertFalse([f for f in server.received if f.get("op") == "wechat.watchlist.remove"])
            self.assertEqual(saved[-1]["never"], ["候选人-王建国"], "★ 要落盘，重启后还在「不管」里")

            self.watchlist.apply_backend({"only": ["候选人-王建国", "普工招聘群"]})
            self.assertEqual(self.watchlist.watched(), ["普工招聘群"], "★ 下一次同步不许把他带回来")

    async def test_removing_someone_not_on_the_list_says_so(self) -> None:
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            runner = self._runner(server)
            self.watchlist.apply_backend({"only": ["普工招聘群"]})

            result = await runner.remove_watched("查无此人")

            self.assertEqual(result["removed"], [])
            self.assertEqual(self.watchlist.watched(), ["普工招聘群"])


class WatchlistSyncTests(_WatchlistCase):
    """配置中心的「同步名单」：整条路走一遍——发出去的是哪个 op、推回来的名单落没落地。

    ★ 这个按钮以前是个预留位（点了只改一行字）。改成真接口之后最容易走偏的一步是
    **把名单塞进这次请求的返回值里**：那会变成本机有两处解析同一份名单，早晚漂。
    名单只走 `wechat.watchlist` 推送落地，这里连带把这条钉住。
    """

    def _syncs(self, server: FakeAgentServer) -> list[dict[str, Any]]:
        return [f.get("data") or {}
                for f in server.received if f.get("op") == "wechat.watchlist.sync"]

    async def test_it_pulls_the_list_from_the_ai_side(self) -> None:
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            server.watchlist = {"候选人-王建国", "普工招聘群"}
            runner = self._runner(server)
            # 本机现在手里是**旧的**（少一个人）：这正是要按那个按钮的场景
            self.watchlist.apply_push({"only": ["普工招聘群"], "never": []})

            result = await runner.sync_watched()

            self.assertEqual(self._syncs(server), [{}], "★ 点了却没发请求 = 还是假按钮")
            self.assertEqual(result["watching"], 2)
            self.assertEqual(self.watchlist.watched(), ["候选人-王建国", "普工招聘群"],
                             "★ 推回来的那份要真落地——界面读的就是它")

    async def test_the_list_does_not_ride_along_in_the_reply(self) -> None:
        """★ 回执只报数。名单跟着推送走，本机只留 `apply_push` 一个落地口。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            server.watchlist = {"候选人-王建国"}
            runner = self._runner(server)

            result = await runner.sync_watched()

            self.assertNotIn("only", result)
            self.assertNotIn("names", result)

    async def test_syncing_an_empty_list_is_not_an_error(self) -> None:
        """AI 端那份就是空的：如实回 0，本机跟着变空——「空」和「没同步到」是两句话。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            server.watchlist = set()
            runner = self._runner(server)
            self.watchlist.apply_push({"only": ["早就不该盯的人"], "never": []})

            result = await runner.sync_watched()

            self.assertEqual(result["watching"], 0)
            self.assertEqual(self.watchlist.watched(), [])


if __name__ == "__main__":  # pragma: no cover - 手动跑单个文件时用
    unittest.main()
