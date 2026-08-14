"""网关协议层测试（Phase A）——全部在 Linux 上跑，不碰微信、不连真 AI 端。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fake_agent_ws import FakeAgentServer, TurnScript

from wxclient.gateway import (
    PUSH_EVENT,
    AgentWsClient,
    AgentWsError,
    GatewaySettings,
    LocalFileTicketProvider,
    TicketError,
    TurnTimeout,
    Unauthorized,
)

WECHAT_CARD = {
    "type": "info",
    "body": {
        "kind": "wechat_reply",
        "reply_segments": ["你好，我们这边还在招普工", "方便说下你在哪个区吗"],
        "intent": "consult",
        "funnel_stage": "intent_check",
        "actions": [],
    },
}


class ServerPushTests(unittest.IsolatedAsyncioTestCase):
    """AI 端主动推来的、不属于任何一轮的事件（监听列表就走这条）。"""

    async def test_unsolicited_event_reaches_on_push(self) -> None:
        received: list[tuple[str, dict]] = []
        payload = {"only": ["候选人-王建国", "招聘交流群"], "never": []}
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, push_on_auth=(PUSH_EVENT, payload)
        ) as server:
            client = AgentWsClient(server.url, on_push=lambda ev, data: received.append((ev, data)))
            await client.connect("tkt-ok")
            try:
                for _ in range(50):          # 推是异步到的，给读循环几拍
                    if received:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(received, [(PUSH_EVENT, payload)])
            finally:
                await client.close()

    async def test_bad_push_handler_does_not_kill_the_connection(self) -> None:
        """一个坏推送不该把整条连接的读循环掀翻——后面还要靠它跑对话。"""
        def boom(_ev: str, _data: dict) -> None:
            raise RuntimeError("处理推送时炸了")

        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, push_on_auth=(PUSH_EVENT, {"only": []})
        ) as server:
            client = AgentWsClient(server.url, on_push=boom)
            await client.connect("tkt-ok")
            try:
                await asyncio.sleep(0.1)
                self.assertTrue(client.connected)
                self.assertTrue(await client.start_session("wechat_reply"))
            finally:
                await client.close()


class WsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_turn_sequence(self) -> None:
        """换票 → 建连 → 建会话 → 发一轮 → 收到卡片，全序列打通。"""
        script = TurnScript(deltas=["你好", "，还在招"], cards=[WECHAT_CARD], elapsed_ms=2500)
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            client = AgentWsClient(server.url)
            await client.connect("tkt-ok")
            try:
                session_id = await client.start_session("wechat_reply")
                self.assertTrue(session_id)

                deltas: list[str] = []
                result = await client.send_message(
                    session_id, "[对方 · 14:03] 还招人吗", on_delta=deltas.append
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.session_id, session_id)
                self.assertEqual(result.text, "你好，还在招")
                self.assertEqual(deltas, ["你好", "，还在招"])
                self.assertEqual(result.elapsed_ms, 2500)
                self.assertIsNotNone(result.card)
                self.assertEqual(result.card["body"]["kind"], "wechat_reply")
                self.assertEqual(
                    result.card["body"]["reply_segments"],
                    ["你好，我们这边还在招普工", "方便说下你在哪个区吗"],
                )
            finally:
                await client.close()

    async def test_auth_failure_raises_unauthorized(self) -> None:
        """票无效 → Unauthorized，且连接关闭（调用方需重新换票）。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            client = AgentWsClient(server.url)
            with self.assertRaises(Unauthorized):
                await client.connect("tkt-bad")
            self.assertFalse(client.connected)

    async def test_resume_continues_same_session(self) -> None:
        """断线重连：新连接上 resume 回同一 session，继续发轮。"""
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[WECHAT_CARD])
        ) as server:
            first = AgentWsClient(server.url)
            await first.connect("tkt-ok")
            session_id = await first.start_session("wechat_reply")
            await first.close()

            # 重连必须现换一张票（这里同一张仅为测试便利）。
            second = AgentWsClient(server.url)
            await second.connect("tkt-ok")
            try:
                await second.resume_session(session_id)
                result = await second.send_message(session_id, "[对方 · 14:10] 在吗")
                self.assertTrue(result.ok)
                self.assertIsNotNone(result.card)
            finally:
                await second.close()

    async def test_resume_unknown_session_raises(self) -> None:
        """接回不属于自己的 session → 报错（AI 端归属鉴权红线）。"""
        async with FakeAgentServer(valid_tickets={"tkt-ok"}) as server:
            client = AgentWsClient(server.url)
            await client.connect("tkt-ok")
            try:
                with self.assertRaises(Unauthorized):
                    await client.resume_session("someone-elses-session")
            finally:
                await client.close()

    async def test_turn_error_returns_fallback_card(self) -> None:
        """AI 端报错 → 结果带 error 与 fallback_card，不抛异常、不白屏。"""
        script = TurnScript(
            error={
                "code": "upstream_error",
                "message": "AI 服务繁忙",
                "fallback_card": {"type": "info", "body": {"text": "稍后重试"}},
            }
        )
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            client = AgentWsClient(server.url)
            await client.connect("tkt-ok")
            try:
                session_id = await client.start_session("wechat_reply")
                result = await client.send_message(session_id, "你好")
                self.assertFalse(result.ok)
                self.assertEqual(result.error["code"], "upstream_error")
                self.assertIsNotNone(result.error["fallback_card"])
                self.assertEqual(result.cards, [])
            finally:
                await client.close()

    async def test_turn_timeout(self) -> None:
        """一轮超时 → TurnTimeout，且该 session 能继续发下一轮（状态已清理）。"""
        script = TurnScript(cards=[WECHAT_CARD], delay_seconds=5.0)
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            client = AgentWsClient(server.url, turn_timeout_seconds=0.3)
            await client.connect("tkt-ok")
            try:
                session_id = await client.start_session("wechat_reply")
                with self.assertRaises(TurnTimeout):
                    await client.send_message(session_id, "你好")
                # 超时后不能留下僵尸状态，否则该候选人从此再也回不了话。
                self.assertNotIn(session_id, client._turns)  # noqa: SLF001
            finally:
                await client.close()

    async def test_concurrent_turn_on_same_session_rejected(self) -> None:
        """同一 session 并发两轮 → 直接拒绝（串行护栏）。"""
        script = TurnScript(cards=[WECHAT_CARD], delay_seconds=0.4)
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            client = AgentWsClient(server.url)
            await client.connect("tkt-ok")
            try:
                session_id = await client.start_session("wechat_reply")
                first = asyncio.create_task(client.send_message(session_id, "第一条"))
                await asyncio.sleep(0.05)
                with self.assertRaises(AgentWsError):
                    await client.send_message(session_id, "第二条")
                self.assertTrue((await first).ok)
            finally:
                await client.close()

    async def test_multiple_sessions_do_not_cross_talk(self) -> None:
        """一条连接承载多个候选人会话，产出不能串台。"""

        def script_for(_session_id: str, text: str) -> TurnScript:
            return TurnScript(
                deltas=[f"回复<{text}>"],
                cards=[{"type": "info", "body": {"kind": "wechat_reply", "echo": text}}],
                delay_seconds=0.2 if text == "甲" else 0.0,
            )

        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script_for) as server:
            client = AgentWsClient(server.url)
            await client.connect("tkt-ok")
            try:
                a = await client.start_session("wechat_reply")
                b = await client.start_session("wechat_reply")
                self.assertNotEqual(a, b)

                results = await asyncio.gather(
                    client.send_message(a, "甲"),
                    client.send_message(b, "乙"),
                )
                by_session = {r.session_id: r for r in results}
                self.assertEqual(by_session[a].card["body"]["echo"], "甲")
                self.assertEqual(by_session[b].card["body"]["echo"], "乙")
                self.assertEqual(by_session[a].text, "回复<甲>")
                self.assertEqual(by_session[b].text, "回复<乙>")
            finally:
                await client.close()

    async def test_disconnect_fails_pending_turn(self) -> None:
        """连接断了，在途的一轮必须立刻失败，不能永远挂着。"""
        script = TurnScript(cards=[WECHAT_CARD], delay_seconds=5.0)
        async with FakeAgentServer(valid_tickets={"tkt-ok"}, turn_script=script) as server:
            client = AgentWsClient(server.url, turn_timeout_seconds=10.0)
            await client.connect("tkt-ok")
            session_id = await client.start_session("wechat_reply")
            pending = asyncio.create_task(client.send_message(session_id, "你好"))
            await asyncio.sleep(0.05)
            await client.close()
            with self.assertRaises(AgentWsError):
                await pending

    async def test_sent_frames_match_contract(self) -> None:
        """发出去的帧必须符合契约：{v,id,op,data}，且 refs 原样带上。"""
        async with FakeAgentServer(
            valid_tickets={"tkt-ok"}, turn_script=TurnScript(cards=[WECHAT_CARD])
        ) as server:
            client = AgentWsClient(server.url)
            await client.connect("tkt-ok")
            try:
                session_id = await client.start_session("wechat_reply")
                await client.send_message(
                    session_id,
                    "[对方 · 14:03] 还招人吗",
                    refs=[{"type": "worker", "id": "w_1", "label": "张三"}],
                    extra={"wechat": {"funnel": "inbound", "is_first_turn": False}},
                )
            finally:
                await client.close()

            ops = [f.get("op") for f in server.received]
            self.assertEqual(ops, ["auth", "session.start", "message.send"])
            for frame in server.received:
                self.assertEqual(frame.get("v"), 1)
                self.assertTrue(frame.get("id"), "每帧都要带 id 供 ack 对号入座")

            send_frame = server.received[-1]["data"]
            self.assertEqual(send_frame["session_id"], session_id)
            self.assertEqual(send_frame["refs"], [{"type": "worker", "id": "w_1", "label": "张三"}])
            self.assertEqual(send_frame["wechat"]["funnel"], "inbound")


class LocalTicketProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tickets.json"
            path.write_text(json.dumps({"wxid_a": "tkt-a"}), encoding="utf-8")
            provider = LocalFileTicketProvider(path)
            self.assertEqual(await provider.mint("wxid_a"), "tkt-a")

    async def test_missing_file_fails_closed(self) -> None:
        provider = LocalFileTicketProvider("/nonexistent/tickets.json")
        with self.assertRaises(TicketError):
            await provider.mint("wxid_a")

    async def test_unknown_account_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tickets.json"
            path.write_text(json.dumps({"wxid_a": "tkt-a"}), encoding="utf-8")
            provider = LocalFileTicketProvider(path)
            with self.assertRaises(TicketError):
                await provider.mint("wxid_b")


class SettingsTests(unittest.TestCase):
    def _settings(self, **kwargs: object) -> GatewaySettings:
        base: dict[str, object] = {
            "ai_ws_url": "wss://ai.example/v1/ws",
            "business_api_url": "https://business.example",
            "internal_key": "k",
        }
        base.update(kwargs)
        return GatewaySettings(**base)  # type: ignore[arg-type]

    def test_accepts_valid(self) -> None:
        self._settings().validate_for_start()

    def test_requires_ws_url(self) -> None:
        with self.assertRaises(ValueError):
            self._settings(ai_ws_url="").validate_for_start()

    def test_rejects_plaintext_ws_to_remote_host(self) -> None:
        """凭证不得明文过网——非回环的 ws:// 直接拒绝启动。"""
        with self.assertRaises(ValueError):
            self._settings(ai_ws_url="ws://ai.example/v1/ws").validate_for_start()

    def test_allows_plaintext_ws_on_loopback(self) -> None:
        self._settings(ai_ws_url="ws://127.0.0.1:8137/v1/ws").validate_for_start()

    def test_requires_internal_key_with_business_url(self) -> None:
        with self.assertRaises(ValueError):
            self._settings(internal_key="").validate_for_start()

    def test_rejects_both_ticket_sources(self) -> None:
        with self.assertRaises(ValueError):
            self._settings(local_ticket_file=Path("/tmp/t.json")).validate_for_start()

    def test_requires_one_ticket_source(self) -> None:
        with self.assertRaises(ValueError):
            self._settings(business_api_url="", internal_key="").validate_for_start()


if __name__ == "__main__":
    unittest.main()
