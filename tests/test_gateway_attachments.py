"""对方发来的图片 / 文件（104）：另存 → 随轮送 AI 端。

**先证坏路被堵**：自己发的不另存、扩展名不在名单不送、另存失败只说不猜；再证好路能通：气泡分类、
同文游标、附件随轮。证件/简历的上报已改道 AI 端统一回执（agent_backend `docs/22`），客户端不再上传。
"""

from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from structlog.testing import capture_logs

from wxclient.gateway.attachments import (
    LEFT_BEHIND_TEXT,
    MAX_ATTACHMENTS_PER_TURN,
    MAX_TOTAL_BYTES,
    AttachmentFetcher,
    inline_attachments,
)
from wxclient.gateway.inbound import InboundMessage, message_from_row, message_to_row
from wxclient.gateway.runner import GatewayRunner
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

    def test_file_name_with_spaces_is_kept_whole(self) -> None:
        """★ 2026-09-29 真机：「李先洋-分拣员个人简历 (2).docx」原来只认出「(2).docx」。"""
        for body in (
            "文件\n李先洋-分拣员个人简历 (2).docx\n7.2K",
            "文件李先洋-分拣员个人简历 (2).docx 7.2K",
        ):
            with self.subTest(body=body):
                self.assertEqual(_annotate_attachment_row("", body)["file_name"], "李先洋-分拣员个人简历 (2).docx")

    def test_size_line_is_not_a_file_name(self) -> None:
        """大小那行（「7.2K」）扩展名不以字母打头，不当文件名。"""
        self.assertEqual(_annotate_attachment_row("", "文件\n7.2K\n简历.pdf")["file_name"], "简历.pdf")
        self.assertEqual(_annotate_attachment_row("", "文件\n7.2K")["file_name"], "")

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
        failed = [e for e in logs if e["event"] == "附件进度" and e.get("失败")]
        self.assertEqual([e["步骤"] for e in failed], ["2/4"], logs)
        self.assertIn("另存为", failed[0]["原因"], "失败那行要带上卡住的原因")

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


class ReuseSavedFileTests(unittest.IsolatedAsyncioTestCase):
    """2026-09-29 用户：先看自己的存储目录里有没有，没有再另存为；不碰微信自己的目录。"""

    PEER = "候选人-王建国"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "attachments"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fetcher(self, runtime: FakeRuntime) -> AttachmentFetcher:
        return AttachmentFetcher(runtime=runtime, root=self.root)

    def _put(self, fetcher: AttachmentFetcher, name: str, size: int) -> Path:
        target = fetcher.dir_for("acct-1", self.PEER)
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        path.write_bytes(b"x" * size)
        return path

    async def test_same_name_and_size_is_reused_without_save_as(self) -> None:
        runtime = FakeRuntime({}, name="简历.pdf")
        fetcher = self._fetcher(runtime)
        path = self._put(fetcher, "简历.pdf", 7373)  # 气泡上显示 7.2K
        raw = {"message_type": "file", "file_name": "简历.pdf"}
        with capture_logs() as logs:
            got = await fetcher.fetch("acct-1", self.PEER, raw)
        self.assertEqual(runtime.saves, [], "本机已有就不再另存")
        self.assertEqual(got["path"], str(path))
        self.assertEqual(got["bytes"], 7373)
        notes = [e["说明"] for e in logs if e["event"] == "附件进度"]
        self.assertTrue(any("已经存过" in n for n in notes), notes)

    async def test_same_name_is_the_same_file_whatever_the_size(self) -> None:
        """用户 2026-09-29：名字相同就算同一份，不比大小。"""
        runtime = FakeRuntime({}, name="简历(1).pdf")
        fetcher = self._fetcher(runtime)
        path = self._put(fetcher, "简历.pdf", 7373)
        raw = {"message_type": "file", "file_name": "简历.pdf"}
        got = await fetcher.fetch("acct-1", self.PEER, raw)
        self.assertEqual(runtime.saves, [])
        self.assertEqual(got["path"], str(path))

    async def test_images_are_always_saved(self) -> None:
        """图片气泡没有名字，认不出是不是同一张。"""
        runtime = FakeRuntime({})
        fetcher = self._fetcher(runtime)
        self._put(fetcher, "微信图片_1.jpg", 11)
        await fetcher.fetch("acct-1", self.PEER, {"message_type": "image"})
        self.assertEqual(len(runtime.saves), 1)

    async def test_nothing_local_goes_to_save_as(self) -> None:
        runtime = FakeRuntime({}, name="简历.pdf")
        got = await self._fetcher(runtime).fetch("acct-1", self.PEER, {"message_type": "file", "file_name": "简历.pdf"})
        self.assertEqual(len(runtime.saves), 1)
        self.assertEqual(got["file_name"], "简历.pdf")


class InlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _message(self, *names: str | tuple[str, int], size: int = 4) -> InboundMessage:
        entries = []
        for spec in names:
            name, size = spec if isinstance(spec, tuple) else (spec, size)
            path = self.dir / name
            path.write_bytes(b"x" * size)
            entries.append({"kind": "image", "file_name": name, "path": str(path), "bytes": size})
        return InboundMessage(account_id="a", peer_id="p", msg_id="p#1", text="t",
                              at=datetime.now(timezone.utc), attachments=tuple(entries))

    def test_base64_shape(self) -> None:
        out, left_behind = inline_attachments([self._message("a.jpg")])
        self.assertEqual(left_behind, [])
        self.assertEqual(out[0]["file_name"], "a.jpg")
        self.assertEqual(out[0]["content_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(out[0]["data_base64"]), b"xxxx")

    def test_caps_count_and_missing_files(self) -> None:
        names = [f"{i}.jpg" for i in range(MAX_ATTACHMENTS_PER_TURN + 2)]
        out, left_behind = inline_attachments([self._message(*names)])
        self.assertEqual(len(out), MAX_ATTACHMENTS_PER_TURN)
        # 没带上的要说得出是哪一份、为什么——日志页上「有 2 份没带上」对方追问时答不上来。
        self.assertEqual(
            left_behind,
            [{"file_name": names[-2], "reason": "too_many"}, {"file_name": names[-1], "reason": "too_many"}],
        )
        message = self._message("gone.jpg")
        (self.dir / "gone.jpg").unlink()
        out, left_behind = inline_attachments([message])
        self.assertEqual((out, left_behind), ([], [{"file_name": "gone.jpg", "reason": "missing"}]))
        self.assertTrue(all(reason in LEFT_BEHIND_TEXT for reason in ("missing", "too_many", "over_budget")))

    def test_total_budget_is_reported_as_its_own_reason(self) -> None:
        big = MAX_TOTAL_BYTES // 2 + 1
        out, left_behind = inline_attachments([self._message(("x.jpg", big), ("y.jpg", big))])
        self.assertEqual([o["file_name"] for o in out], ["x.jpg"])
        self.assertEqual(left_behind, [{"file_name": "y.jpg", "reason": "over_budget"}])



class ClaimedDocumentsLogTests(unittest.TestCase):
    """104/106：送过去的附件 AI 端认没认成材料、会不会上传，日志里得说清楚——
    上传在 AI 端做，这台电脑看不到结果，但从卡上 `documents[]` 就能判「会不会传」。"""

    SENT: ClassVar[list[dict]] = [{"file_name": "身份证.jpg", "kind": "image", "data_base64": "AAAA"},
                                  {"file_name": "简历.pdf", "kind": "file", "data_base64": "AAAA"}]

    def _card(self, documents: object) -> dict:
        return {"body": {"kind": "wechat_reply", "documents": documents}}

    def test_claimed_with_and_without_consent(self) -> None:
        card = self._card([
            {"material_type": "id_card_front", "file_name": "身份证.jpg",
             "fields": {"name": "王建国", "id_number": "110101199001011234"}, "consent_note": "身份证发你了"},
            {"material_type": "resume", "file_name": "简历.pdf"},
        ])
        with capture_logs() as logs:
            GatewayRunner._log_claimed_documents("候选人-王建国", self.SENT, card)
        entry = next(e for e in logs if e["event"] == "AI 端认领了材料")
        self.assertEqual(entry["清单"], [
            {"文件": "身份证.jpg", "类型": "身份证正面", "有授权原话": True, "字段数": 2},
            {"文件": "简历.pdf", "类型": "简历", "有授权原话": False, "字段数": 0},
        ])
        self.assertEqual(entry["未认领"], [])
        # 字段值是证件号一类的 PII：只记个数，日志里一个数字都不能出现。
        self.assertNotIn("110101199001011234", repr(logs))
        self.assertNotIn("王建国", repr(entry["清单"]))

    def test_unclaimed_files_are_named(self) -> None:
        card = self._card([{"material_type": "resume", "file_name": "简历.pdf", "consent_note": "简历给你"}])
        with capture_logs() as logs:
            GatewayRunner._log_claimed_documents("p", self.SENT, card)
        self.assertEqual(logs[0]["未认领"], ["身份证.jpg"])

    def test_nothing_claimed_says_so(self) -> None:
        with capture_logs() as logs:
            GatewayRunner._log_claimed_documents("p", self.SENT, self._card(None))
            GatewayRunner._log_claimed_documents("p", self.SENT, None)
            GatewayRunner._log_claimed_documents("p", [], self._card(None))
        self.assertEqual([e["event"] for e in logs], ["AI 端没认领这一轮的附件"] * 2)
        self.assertEqual(logs[0]["送去"], ["身份证.jpg", "简历.pdf"])


if __name__ == "__main__":
    unittest.main()
