"""语音消息：微信开了「聊天中的语音消息自动转成文字」之后，网关怎么把听写正文读进来。

vendor（pyweixin）自己不点「转文字」，只认微信已经转好的字：语音气泡的 UIA 名称形如
``语音12"秒<正文>``。这里守三件事：① 读取层认出语音并剥出正文；② 游标签名在转写前后
不变；③ 转写没出来时压住整段等几轮，等不到就按「没能转成文字」照常处理。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from wxclient.gateway import inbound as _inbound_mod
from wxclient.gateway.inbound import (
    InboundMessage,
    format_history,
    format_transcript,
    message_from_row,
    message_to_row,
)
from wxclient.gateway.wechat_io import (
    VOICE_TRANSCRIPT_STRIKES,
    VOICE_UNTRANSCRIBED_TEXT,
    hold_pending_voice,
    message_signature,
    split_new_messages,
    to_inbound,
)
from wxclient.wechat_api.uia import VOICE_ITEM_CLASS, _annotate_voice_row

# 夹具时刻是 UTC，行首时分的断言按 UTC 写；生产按本机时区打（见 inbound.local_clock_text）。
_inbound_mod.LOCAL_TZ = timezone.utc


class AnnotateVoiceRowTests(unittest.TestCase):
    def test_transcribed_bubble_yields_body_and_stable_label(self) -> None:
        row = _annotate_voice_row(VOICE_ITEM_CLASS, '语音12"秒你好，我想问下这个岗位还招吗')
        self.assertEqual(row["message_type"], "voice")
        self.assertEqual(row["voice_label"], '语音12"秒')
        self.assertEqual(row["text"], "你好，我想问下这个岗位还招吗")
        self.assertFalse(row["transcript_pending"])

    def test_untranscribed_bubble_is_pending_with_empty_text(self) -> None:
        for raw in ('语音12"秒', '语音 12"', "", "語音3秒", 'Audio 5"sec'):
            with self.subTest(raw=raw):
                row = _annotate_voice_row(VOICE_ITEM_CLASS, raw)
                self.assertEqual(row["text"], "")
                self.assertTrue(row["transcript_pending"])
                self.assertTrue(row["voice_label"])

    def test_unknown_shape_keeps_raw_as_label_and_stays_pending(self) -> None:
        """真机上名称长得跟 vendor 正则不一样时，别把整段名称当正文喂给模型。"""
        row = _annotate_voice_row(VOICE_ITEM_CLASS, "Voice message")
        self.assertEqual(row["voice_label"], "Voice message")
        self.assertTrue(row["transcript_pending"])

    def test_text_bubble_starting_with_voice_word_is_not_voice(self) -> None:
        self.assertIsNone(_annotate_voice_row("mmui::ChatTextItemView", '语音12"秒不是语音'))


class VoiceSignatureTests(unittest.TestCase):
    def test_signature_does_not_change_when_transcript_arrives(self) -> None:
        before = {"class_name": VOICE_ITEM_CLASS, "voice_label": '语音12"秒', "text": "", "transcript_pending": True}
        after = dict(before, text="你好", transcript_pending=False)
        self.assertEqual(message_signature(before), message_signature(after))
        self.assertNotEqual(message_signature(after), message_signature({"text": "你好"}))

    def test_transcribed_voice_after_cursor_is_new(self) -> None:
        history = [
            {"sender": "我", "text": "岗位在苏州"},
            {"class_name": VOICE_ITEM_CLASS, "voice_label": '语音5"秒', "text": "工资多少", "message_type": "voice"},
        ]
        fresh, cursor = split_new_messages(history, message_signature(history[0]))
        self.assertEqual([m["text"] for m in fresh], ["工资多少"])
        self.assertEqual(cursor, message_signature(history[1]))


class HoldPendingVoiceTests(unittest.TestCase):
    def _history(self, pending: bool) -> list[dict]:
        return [
            {"text": "老消息"},
            {"class_name": VOICE_ITEM_CLASS, "voice_label": '语音5"秒', "text": "" if pending else "工资多少",
             "transcript_pending": pending, "message_type": "voice"},
            {"text": "在吗"},
        ]

    def test_pending_voice_holds_tail_so_cursor_stays_before_it(self) -> None:
        waits: dict = {}
        kept, held = hold_pending_voice(self._history(pending=True), waits, "张三")
        self.assertEqual([m["text"] for m in kept], ["老消息"])
        self.assertEqual(held, 2, "语音和它后面的「在吗」一起压住")
        self.assertEqual(waits, {("张三", '语音5"秒'): 1})

    def test_ready_voice_is_not_held(self) -> None:
        kept, held = hold_pending_voice(self._history(pending=False), {}, "张三")
        self.assertEqual(held, 0)
        self.assertEqual(len(kept), 3)

    def test_gives_up_after_strikes_and_never_holds_again(self) -> None:
        waits: dict = {}
        for _ in range(VOICE_TRANSCRIPT_STRIKES - 1):
            _, held = hold_pending_voice(self._history(pending=True), waits, "张三")
            self.assertEqual(held, 2)
        kept, held = hold_pending_voice(self._history(pending=True), waits, "张三")
        self.assertEqual(held, 0)
        self.assertEqual(kept[1]["text"], VOICE_UNTRANSCRIBED_TEXT)
        self.assertFalse(kept[1]["transcript_pending"])
        # 微信永远转不出来的那条：以后每一轮都直接放行，不能每三轮又压一次后面的消息
        for _ in range(3):
            _, held = hold_pending_voice(self._history(pending=True), waits, "张三")
            self.assertEqual(held, 0)

    def test_given_up_row_keeps_cursor_signature(self) -> None:
        """放弃等待写了占位正文之后签名仍按标签算，下一轮同一条还能对回游标。"""
        waits = {("张三", '语音5"秒'): VOICE_TRANSCRIPT_STRIKES}
        kept, _ = hold_pending_voice(self._history(pending=True), waits, "张三")
        self.assertEqual(message_signature(kept[1]), message_signature(self._history(pending=True)[1]))


class VoiceInboundTests(unittest.TestCase):
    def test_to_inbound_carries_message_type(self) -> None:
        raw = {"text": "工资多少", "message_type": "voice", "is_self": False}
        message = to_inbound("acc", "张三", raw, seq=1)
        self.assertEqual(message.message_type, "voice")
        self.assertEqual(to_inbound("acc", "张三", {"text": "hi"}, seq=2).message_type, "text")

    def test_row_round_trip_keeps_message_type(self) -> None:
        message = InboundMessage("acc", "张三", "张三#1", "工资多少", datetime.now(timezone.utc), message_type="voice")
        self.assertEqual(message_from_row("acc", "张三", message_to_row(message)).message_type, "voice")

    def test_transcript_marks_voice_lines(self) -> None:
        at = datetime(2026, 9, 2, 10, 5, tzinfo=timezone.utc)
        messages = [
            InboundMessage("acc", "张三", "张三#1", "工资多少", at, message_type="voice"),
            InboundMessage("acc", "张三", "张三#2", "在吗", at),
        ]
        text = format_transcript(messages)
        self.assertIn("[对方 · 10:05] （语音转文字）工资多少", text)
        self.assertIn("[对方 · 10:05] 在吗", text)
        history = format_history([message_to_row(m) for m in messages])
        self.assertIn("（语音转文字）工资多少", history)


if __name__ == "__main__":
    unittest.main()
