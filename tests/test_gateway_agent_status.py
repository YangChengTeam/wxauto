"""089 · 分身级接管状态上报（docs/19 §2.1）。

★ 这份测试盯的是**一条**规则：上报失败绝不回滚本机状态，唯独 409 例外。
  两个方向都错得很贵——
  网络失败也回滚 = 「我明明点了接管，分身还在发消息」；
  409 不回滚 = 企业端看到的和真机上跑的长期不是一回事。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import httpx

from wxclient.gateway.agent_status import AgentStatusReporter

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
BASE = "https://biz.example.com"


def reporter(handler) -> AgentStatusReporter:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return AgentStatusReporter(BASE, lambda: "client-token", client=client, clock=lambda: NOW)


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"accepted": True, "agentStatus": "human_takeover", "version": 5})


class ReportShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_sends_the_contract_shape(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["body"] = __import__("json").loads(request.content)
            return ok(request)

        outcome = await reporter(handler).report("take_over", reason="我来跟这几个人聊", expected_version=4)
        self.assertTrue(outcome.reported)
        self.assertEqual(seen["method"], "PUT")
        self.assertTrue(seen["path"].endswith("/recruitment-agent/client/agent-status"))
        self.assertEqual(seen["body"]["status"], "human_takeover")
        self.assertEqual(seen["body"]["expectedVersion"], 4)
        self.assertTrue(seen["body"]["eventId"])
        self.assertEqual(seen["body"]["occurredAt"], NOW.isoformat())

    async def test_handing_back_maps_to_ai_managed(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = __import__("json").loads(request.content)
            return ok(request)

        await reporter(handler).report("delegated")
        self.assertEqual(seen["body"]["status"], "ai_managed")
        self.assertNotIn("reason", seen["body"])

    async def test_the_same_switch_reported_twice_keeps_one_event_id(self) -> None:
        """★ 幂等键由调用方给：补报的是**同一件事**，不该在后端多记一条审计。"""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(__import__("json").loads(request.content)["eventId"])
            return ok(request)

        r = reporter(handler)
        await r.report("take_over", reason="x", event_id="evt-1")
        await r.report("take_over", reason="x", event_id="evt-1")
        self.assertEqual(seen, ["evt-1", "evt-1"])

    async def test_takeover_without_a_reason_is_refused_before_it_leaves(self) -> None:
        """§2.1：`human_takeover` 必填 reason。少了会被 400 打回，本机先说清楚。"""
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return ok(request)

        outcome = await reporter(handler).report("take_over")
        self.assertFalse(outcome.reported)
        self.assertFalse(called)
        self.assertIn("原因", outcome.reason)


class NeverRollbackTests(unittest.IsolatedAsyncioTestCase):
    """★★ 除 409 外一律不回滚，而且要**欠着**下次接着报。"""

    async def _report(self, handler):
        return await reporter(handler).report("take_over", reason="我来")

    async def test_a_network_failure_changes_nothing(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        outcome = await self._report(handler)
        self.assertFalse(outcome.reported)
        self.assertIsNone(outcome.rollback_to)
        self.assertTrue(outcome.should_retry)

    async def test_a_500_changes_nothing(self) -> None:
        outcome = await self._report(lambda _r: httpx.Response(500, json={}))
        self.assertIsNone(outcome.rollback_to)
        self.assertTrue(outcome.should_retry)

    async def test_a_429_changes_nothing(self) -> None:
        """连点被限流，回滚就等于「点了接管却没接管」。"""
        outcome = await self._report(lambda _r: httpx.Response(429, json={}))
        self.assertIsNone(outcome.rollback_to)
        self.assertTrue(outcome.should_retry)

    async def test_a_revoked_session_leaves_the_local_gate_alone(self) -> None:
        """§4.3 明写：401 清 Token 回登录页，但**本机那道闸不动**。"""
        outcome = await self._report(lambda _r: httpx.Response(401, json={}))
        self.assertIsNone(outcome.rollback_to)


class ConflictRollbackTests(unittest.IsolatedAsyncioTestCase):
    """★ 唯一该回滚的一档。"""

    async def test_409_rolls_back_to_the_value_the_backend_gives(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, json={
                "error": {
                    "code": "AGENT_STATUS_VERSION_CONFLICT",
                    "message": "已被更新",
                    "details": {"agentStatus": "ai_managed", "version": 6},
                }
            })

        outcome = await reporter(handler).report("take_over", reason="我来")
        self.assertFalse(outcome.reported)
        self.assertEqual(outcome.rollback_to, "delegated")
        self.assertEqual(outcome.rollback_version, 6)
        self.assertFalse(outcome.should_retry)

    async def test_409_without_a_real_value_does_not_roll_back(self) -> None:
        """★ 真值取不出来就不回滚：宁可两边先各说各话，也不能凭解析失败撤掉刚按下的接管。"""
        outcome = await reporter(lambda _r: httpx.Response(409, text="not json")).report(
            "take_over", reason="我来"
        )
        self.assertIsNone(outcome.rollback_to)


if __name__ == "__main__":
    unittest.main()
