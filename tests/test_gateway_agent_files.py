"""附件直接传到 AI 端的会话、只带 file_ids（用户 2026-09-28；AI 服务仓 docs/21 §2.0）。

重点是**传不上去时这一轮照样送到**：换不到票、网络断、非 201、回的个数对不上，一律退回内联，
一份都不能因为换了送法而丢掉。以及**只送字节不送路径**。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway.agent_files import (
    TICKET_HEADER,
    AgentFileUploader,
    http_base_from_ws,
)
from wxclient.gateway.runner import GatewayRunner
from wxclient.gateway.ticket import TicketError

FILES = [
    {"file_name": "微信图片_1.jpg", "content_type": "image/jpeg", "kind": "image", "data": b"\xff\xd8jpg"},
    {"file_name": "简历.pdf", "content_type": "application/pdf", "kind": "file", "data": b"%PDF-1.4"},
]


class _Tickets:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.minted = 0

    async def mint(self, wechat_account_id: str) -> str:
        if self.fail:
            raise TicketError("这台机器还没登录，换不到票")
        self.minted += 1
        return f"ticket-{self.minted}"


def _uploader(handler, tickets: _Tickets | None = None) -> tuple[AgentFileUploader, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    uploader = AgentFileUploader(
        base_url="https://worker.example.cn/agent-ws", ticket_provider=tickets or _Tickets(), client=client
    )
    return uploader, seen


def _created(*ids: str) -> httpx.Response:
    return httpx.Response(201, json={"files": [{"file_id": i, "name": f"f{n}"} for n, i in enumerate(ids)]})


class HttpBaseTests(unittest.TestCase):
    def test_same_proxy_prefix_as_the_ws_door(self) -> None:
        self.assertEqual(http_base_from_ws("wss://worker.laoxiaozhi.cn/agent-ws/v1/ws"), "https://worker.laoxiaozhi.cn/agent-ws")
        self.assertEqual(http_base_from_ws("ws://127.0.0.1:8137/v1/ws"), "http://127.0.0.1:8137")
        self.assertEqual(http_base_from_ws("wss://h/v1/ws/"), "https://h")


class UploaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_bytes_to_this_session_with_a_fresh_ticket(self) -> None:
        uploader, seen = _uploader(lambda r: _created("id-1", "id-2"))
        ids = await uploader.upload("wxid", "张三", "sess/1", FILES)
        self.assertEqual(ids, ["id-1", "id-2"])
        request = seen[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://worker.example.cn/agent-ws/v1/sessions/sess%2F1/files")
        self.assertEqual(request.headers[TICKET_HEADER], "ticket-1")
        body = request.content
        self.assertIn(b'name="files"', body)
        self.assertIn(b"%PDF-1.4", body)
        self.assertNotIn(b"\\\\", body, "★ 只送字节和文件名，不送本机路径")

    async def test_every_failure_returns_none(self) -> None:
        cases = {
            "403": lambda r: httpx.Response(403, json={"error": {"code": "forbidden"}}),
            "500 非 JSON": lambda r: httpx.Response(500, text="bad gateway"),
            "个数对不上": lambda r: _created("id-1"),
            "编号空": lambda r: httpx.Response(201, json={"files": [{"file_id": ""}, {"file_id": "x"}]}),
            "形状不对": lambda r: httpx.Response(201, json={"ok": True}),
        }
        for why, handler in cases.items():
            uploader, _ = _uploader(handler)
            self.assertIsNone(await uploader.upload("wxid", "张三", "s1", FILES), why)

    async def test_network_error_returns_none(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        uploader, _ = _uploader(boom)
        self.assertIsNone(await uploader.upload("wxid", "张三", "s1", FILES))

    async def test_no_ticket_no_request(self) -> None:
        uploader, seen = _uploader(lambda r: _created("a", "b"), tickets=_Tickets(fail=True))
        self.assertIsNone(await uploader.upload("wxid", "张三", "s1", FILES))
        self.assertEqual(seen, [])


class _FakeUploader:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[str] = []

    async def upload(self, account_id, peer_id, session_id, files):
        self.calls.append(session_id)
        return self.result


def _turn_files(uploader, session_id: str, uploaded: dict) -> dict:
    runner = SimpleNamespace(_uploader=uploader)
    return asyncio.run(GatewayRunner._turn_files(runner, "wxid", "张三", session_id, FILES, uploaded))


class TurnFilesTests(unittest.TestCase):
    def test_uploaded_turn_carries_only_file_ids(self) -> None:
        extra = _turn_files(_FakeUploader(["id-1", "id-2"]), "s1", {})
        self.assertEqual(extra, {"file_ids": ["id-1", "id-2"]})

    def test_upload_failure_falls_back_to_inline_with_every_file(self) -> None:
        extra = _turn_files(_FakeUploader(None), "s1", {})
        self.assertEqual([a["file_name"] for a in extra["attachments"]], ["微信图片_1.jpg", "简历.pdf"])
        self.assertNotIn("file_ids", extra)

    def test_no_uploader_is_the_old_inline_path(self) -> None:
        self.assertIn("attachments", _turn_files(None, "s1", {}))

    def test_retry_on_the_same_session_does_not_upload_twice(self) -> None:
        uploader = _FakeUploader(["id-1", "id-2"])
        uploaded: dict = {}
        _turn_files(uploader, "s1", uploaded)
        _turn_files(uploader, "s1", uploaded)
        _turn_files(uploader, "s2", uploaded)  # 会话被清掉重建 → 新会话要重传
        self.assertEqual(uploader.calls, ["s1", "s2"])

    def test_no_files_no_extra(self) -> None:
        runner = SimpleNamespace(_uploader=_FakeUploader(["x"]))
        self.assertEqual(asyncio.run(GatewayRunner._turn_files(runner, "wxid", "张三", "s1", [], {})), {})


if __name__ == "__main__":
    unittest.main()
