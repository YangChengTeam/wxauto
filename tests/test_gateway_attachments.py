"""对方发来的图片 / 文件（104）：另存 → 随轮送 AI 端 → 卡上认领的证件/简历传业务后端。

**先证坏路被堵**：自己发的不另存、扩展名不在名单不送、另存失败只说不猜、候选人反查不到不上传、
后端 404 不上抛；再证好路能通：气泡分类、同文游标、附件随轮、multipart 形状与契约一致。
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from structlog.testing import capture_logs

from wxclient.gateway.attachments import (
    MAX_ATTACHMENTS_PER_TURN,
    AttachmentFetcher,
    attachment_bytes,
    inline_attachments,
)
from wxclient.gateway.candidates import CandidateDirectory, CandidateUpdateError
from wxclient.gateway.inbound import InboundMessage, message_from_row, message_to_row
from wxclient.gateway.sessions import GatewayStore
from wxclient.gateway.watchlist import Watchlist
from wxclient.gateway.wechat_io import (
    EMOJI_TEXT,
    IMAGE_UNSAVED_TEXT,
    QueueMessageSource,
    message_signature,
    split_new_messages,
)
from wxclient.queue import TaskType
from wxclient.wechat_api.uia import _annotate_attachment_row


class RowClassificationTests(unittest.TestCase):
    """UIA 树里的气泡名字 → message_type。只按名字/类名判，不碰控件。"""

    def test_image_and_file_and_emoji(self) -> None:
        self.assertEqual(_annotate_attachment_row("mmui::ChatImageItemView", "图片"), {"message_type": "image"})
        self.assertEqual(_annotate_attachment_row("", "[图片]"), {"message_type": "image"})
        self.assertEqual(_annotate_attachment_row("", "动画表情"), {"message_type": "emoji"})
        self.assertEqual(_annotate_attachment_row("mmui::ChatEmoticonItemView", ""), {"message_type": "emoji"})
        self.assertEqual(_annotate_attachment_row("", "视频"), {"message_type": "video"})

    def test_file_bubble_keeps_its_name(self) -> None:
        self.assertEqual(
            _annotate_attachment_row("", "文件 王建国简历.pdf"),
            {"message_type": "file", "file_name": "王建国简历.pdf"},
        )
        self.assertEqual(_annotate_attachment_row("", "[文件]"), {"message_type": "file", "file_name": ""})

    def test_plain_text_is_left_alone(self) -> None:
        self.assertIsNone(_annotate_attachment_row("mmui::ChatTextItemView", "还招人吗"))
        self.assertIsNone(_annotate_attachment_row("", "我发了图片给你"))


class DuplicateTextCursorTests(unittest.TestCase):
    """两张身份证正反面读出来都叫「图片」：游标停在第一张，下一轮第二张必须算新的。"""

    def test_second_identical_row_is_new(self) -> None:
        first = [{"text": "还招人吗"}, {"text": "图片", "message_type": "image"}]
        fresh, cursor = split_new_messages(first, None)
        self.assertEqual(len(fresh), 2)
        second = first + [{"text": "图片", "message_type": "image"}]
        fresh, cursor2 = split_new_messages(second, cursor)
        self.assertEqual(len(fresh), 1, "第二张图片得算新消息")
        self.assertNotEqual(cursor, cursor2)
        # 第三轮没新东西：游标精确停在第二张上。
        fresh, _ = split_new_messages(second, cursor2)
        self.assertEqual(fresh, [])

    def test_legacy_cursor_without_dup_index_still_matches(self) -> None:
        """库里 104 之前记的游标（纯文本哈希）照样认得回来，不白丢一条。"""
        rows = [{"text": "好的"}, {"text": "再发一次"}]
        legacy = message_signature({"text": "好的"})
        fresh, _ = split_new_messages(rows, legacy)
        self.assertEqual([r["text"] for r in fresh], ["再发一次"])

    def test_first_occurrence_signature_unchanged(self) -> None:
        self.assertEqual(message_signature({"text": "图片"}, 0), message_signature({"text": "图片"}))


class FakeRuntime:
    """回放聊天记录；另存任务往 target_dir 里真写一个文件（或按 fail 失败）。"""

    def __init__(self, history: dict[str, list[dict]], *, fail: bool = False, name: str = "微信图片_1.jpg") -> None:
        self.history = history
        self.fail = fail
        self.name = name
        self.saves: list[dict] = []

    async def submit(self, task_type, payload=None, **_kw):
        if task_type is TaskType.SAVE_MESSAGE_ATTACHMENT:
            self.saves.append(dict(payload or {}))
            if self.fail:
                return {"status": "failed", "last_error": "右键菜单里没有「另存为」"}
            target = Path(payload["target_dir"]) / self.name
            target.write_bytes(b"\xff\xd8fake-jpeg")
            return {"status": "succeeded", "result": {"path": str(target), "file_name": self.name, "bytes": 11}}
        friend = (payload or {}).get("friend")
        return {"result": {"messages": self.history.get(friend, [])}}


class SourceAttachmentTests(unittest.IsolatedAsyncioTestCase):
    PEER = "候选人-王建国"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.root = Path(self._tmp.name) / "attachments"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _source(self, runtime: FakeRuntime, *, fetcher: bool = True) -> QueueMessageSource:
        return QueueMessageSource(
            runtime=runtime,
            store=self.store,
            watchlist=Watchlist.from_local(only=[self.PEER]),
            attachment_fetcher=AttachmentFetcher(runtime=runtime, root=self.root) if fetcher else None,
        )

    async def test_peer_image_is_saved_and_attached(self) -> None:
        runtime = FakeRuntime({self.PEER: [{"text": "图片", "message_type": "image", "is_self": False,
                                             "runtime_id": "rid-1", "order": 3}]})
        messages = await self._source(runtime).fetch("acct-1")
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.message_type, "image")
        self.assertIn("微信图片_1.jpg", message.text)
        self.assertEqual(message.attachments[0]["file_name"], "微信图片_1.jpg")
        self.assertTrue(Path(message.attachments[0]["path"]).is_file())
        self.assertEqual(runtime.saves[0]["friend"], self.PEER)
        self.assertEqual(runtime.saves[0]["runtime_id"], "rid-1")
        self.assertTrue(runtime.saves[0]["target_dir"].startswith(str(self.root)))

    async def test_own_image_is_not_saved(self) -> None:
        """专员自己发出去的合同/海报不用回头再读一遍。"""
        runtime = FakeRuntime({self.PEER: [{"text": "图片", "message_type": "image", "is_self": True}]})
        messages = await self._source(runtime).fetch("acct-1")
        self.assertEqual(runtime.saves, [])
        self.assertEqual(messages, [], "尾部是自己发的 = 人在接着聊，首读一条都不接")

    async def test_save_failure_is_said_not_guessed(self) -> None:
        runtime = FakeRuntime({self.PEER: [{"text": "图片", "message_type": "image", "is_self": False}]}, fail=True)
        with capture_logs() as logs:
            messages = await self._source(runtime).fetch("acct-1")
        self.assertEqual(messages[0].text, IMAGE_UNSAVED_TEXT)
        self.assertEqual(messages[0].attachments, ())
        self.assertTrue(any("没能另存" in entry["event"] for entry in logs), logs)

    async def test_disallowed_extension_is_dropped(self) -> None:
        runtime = FakeRuntime({self.PEER: [{"text": "文件 setup.exe", "message_type": "file", "is_self": False}]},
                              name="setup.exe")
        messages = await self._source(runtime).fetch("acct-1")
        self.assertEqual(messages[0].attachments, ())
        self.assertFalse(list(self.root.rglob("*.exe")), "不在名单里的文件另存了也要删掉")

    async def test_emoji_becomes_filler_placeholder(self) -> None:
        """动画表情喂给 AI 端的是 `[动画表情]`——AI 端 FILLER_RE 认它，模型才知道不用回。"""
        runtime = FakeRuntime({self.PEER: [{"text": "动画表情", "message_type": "emoji", "is_self": False}]})
        messages = await self._source(runtime).fetch("acct-1")
        self.assertEqual(messages[0].text, EMOJI_TEXT)
        self.assertEqual(messages[0].message_type, "emoji")
        self.assertEqual(runtime.saves, [])

    async def test_fetcher_off_only_describes(self) -> None:
        runtime = FakeRuntime({self.PEER: [{"text": "图片", "message_type": "image", "is_self": False}]})
        messages = await self._source(runtime, fetcher=False).fetch("acct-1")
        self.assertEqual(messages[0].text, IMAGE_UNSAVED_TEXT)
        self.assertEqual(runtime.saves, [])

    async def test_row_roundtrip_keeps_attachments(self) -> None:
        message = InboundMessage(
            account_id="a", peer_id="p", msg_id="p#1", text="[图片：x.jpg（已附上）]",
            at=datetime(2026, 9, 9, tzinfo=timezone.utc), message_type="image",
            attachments=({"kind": "image", "file_name": "x.jpg", "path": "/tmp/x.jpg", "bytes": 3},),
        )
        back = message_from_row("a", "p", message_to_row(message))
        self.assertEqual(back.attachments, message.attachments)


class InlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _message(self, *names: str, size: int = 4) -> InboundMessage:
        entries = []
        for name in names:
            path = self.dir / name
            path.write_bytes(b"x" * size)
            entries.append({"kind": "image", "file_name": name, "path": str(path), "bytes": size})
        return InboundMessage(account_id="a", peer_id="p", msg_id="p#1", text="t",
                              at=datetime.now(timezone.utc), attachments=tuple(entries))

    def test_base64_shape(self) -> None:
        out, skipped = inline_attachments([self._message("a.jpg")])
        self.assertEqual(skipped, 0)
        self.assertEqual(out[0]["file_name"], "a.jpg")
        self.assertEqual(out[0]["content_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(out[0]["data_base64"]), b"xxxx")

    def test_caps_count_and_missing_files(self) -> None:
        names = [f"{i}.jpg" for i in range(MAX_ATTACHMENTS_PER_TURN + 2)]
        out, skipped = inline_attachments([self._message(*names)])
        self.assertEqual(len(out), MAX_ATTACHMENTS_PER_TURN)
        self.assertEqual(skipped, 2)
        message = self._message("gone.jpg")
        (self.dir / "gone.jpg").unlink()
        out, skipped = inline_attachments([message])
        self.assertEqual((out, skipped), ([], 1))

    def test_attachment_bytes_by_name(self) -> None:
        message = self._message("id.jpg")
        self.assertEqual(attachment_bytes([message], "id.jpg"), b"xxxx")
        self.assertIsNone(attachment_bytes([message], "other.jpg"))


def directory(handler, token: str = "tok") -> CandidateDirectory:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CandidateDirectory("https://api.example.com", lambda: token, client=client)


class UploadMaterialTests(unittest.IsolatedAsyncioTestCase):
    """契约 `docs/20` §3：multipart，字段按材料类型白名单，后端没实现（404）不上抛。"""

    async def test_multipart_shape(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["url"] = str(request.url)
            seen["content_type"] = request.headers.get("content-type", "")
            seen["body"] = request.content
            return httpx.Response(201, json={"materialId": "m-1"})

        got = await directory(handler).upload_material(
            "cand-1", material_type="id_card_front", file_name="正面.jpg", data=b"jpegbytes",
            fields={"name": "王建国", "id_number": "4201**********1234", "phone": "138"},
            consent_note="候选人在微信里主动发来",
        )
        self.assertEqual(got, {"uploaded": True, "material_id": "m-1"})
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["url"].endswith("/recruitment-agent/client/candidates/cand-1/onboarding-materials"))
        self.assertTrue(seen["content_type"].startswith("multipart/form-data"))
        body = seen["body"]
        self.assertIn(b'name="material_type"', body)
        self.assertIn(b"id_card_front", body)
        self.assertIn(b'filename="' + "正面.jpg".encode() + b'"', body)
        self.assertIn(b"jpegbytes", body)
        fields = json.loads(body.split(b'name="fields"')[1].split(b"\r\n\r\n")[1].split(b"\r\n")[0])
        self.assertEqual(fields, {"name": "王建国", "id_number": "4201**********1234"}, "phone 不在正面白名单里")
        consent = json.loads(body.split(b'name="consent"')[1].split(b"\r\n\r\n")[1].split(b"\r\n")[0])
        self.assertEqual(consent["source"], "wechat_chat")

    async def test_backend_not_ready_404_is_reported_not_raised(self) -> None:
        got = await directory(lambda _r: httpx.Response(404, json={})).upload_material(
            "cand-1", material_type="resume", file_name="简历.pdf", data=b"%PDF",
        )
        self.assertEqual(got["uploaded"], False)
        self.assertIn("后端", got["reason"])

    async def test_bad_shape_is_rejected_locally(self) -> None:
        called: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(str(request.url))
            return httpx.Response(200)

        with self.assertRaises(CandidateUpdateError):
            await directory(handler).upload_material("cand-1", material_type="passport", file_name="a.jpg", data=b"x")
        with self.assertRaises(CandidateUpdateError):
            await directory(handler).upload_material("", material_type="resume", file_name="a.pdf", data=b"x")
        self.assertEqual(called, [])


class RunnerDocumentUploadTests(unittest.IsolatedAsyncioTestCase):
    """卡上 `documents[]` → 本机反查候选人 → 上传；反查不到不上传，找不到附件跳过。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gw.db")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, candidates):
        from wxclient.gateway.runner import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._store = self.store
        runner._candidates = candidates
        return runner

    def _messages(self, name: str) -> list[InboundMessage]:
        path = self.dir / name
        path.write_bytes(b"jpeg")
        return [InboundMessage(account_id="acc", peer_id="张三·普工", msg_id="m#1", text="t",
                               at=datetime.now(timezone.utc), message_type="image",
                               attachments=({"kind": "image", "file_name": name, "path": str(path), "bytes": 4},))]

    def _card(self, documents) -> dict:
        return {"type": "info", "body": {"kind": "wechat_reply", "reply_segments": ["收到"],
                                         "intent": "consult", "funnel_stage": "qa", "documents": documents}}

    def _seed(self, worker_id: str | None) -> None:
        self.store.save_contacts("acc", [{
            "number": "13800138000", "session_name": "张三·普工",
            "contact_status": "accepted", "worker_id": worker_id, "updated_at": "2026-08-19T00:00:00Z",
        }])

    async def test_uploads_claimed_document(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(201, json={"materialId": "m-1"})

        self._seed("w-77")
        await self._runner(directory(handler))._upload_documents(
            "acc", "张三·普工",
            self._card([{"material_type": "id_card_front", "file_name": "正面.jpg", "fields": {"name": "张三"}}]),
            self._messages("正面.jpg"),
        )
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].endswith("/candidates/w-77/onboarding-materials"))

    async def test_unknown_candidate_or_missing_file_uploads_nothing(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(201, json={})

        runner = self._runner(directory(handler))
        card = self._card([{"material_type": "resume", "file_name": "简历.pdf"}])
        self._seed(None)
        await runner._upload_documents("acc", "张三·普工", card, self._messages("简历.pdf"))
        self._seed("w-77")
        await runner._upload_documents("acc", "张三·普工", card, self._messages("别的.pdf"))
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
