from __future__ import annotations

import logging
import unittest
from pathlib import Path

from wxclient.logging_config import ChineseLogRenderer


class ChineseLogRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = ChineseLogRenderer()
        self.logger = logging.getLogger("test")

    def render(self, event: str, **payload: object) -> str:
        return self.renderer(
            self.logger,
            "info",
            {
                "timestamp": "2026-08-08 17:30:00",
                "level": "info",
                "event": event,
                **payload,
            },
        )

    def test_renders_task_lifecycle_in_chinese(self) -> None:
        output = self.render(
            "task.succeeded",
            task_type="send_message",
            task_id="12345678-abcd",
        )
        self.assertEqual(
            output,
            "2026-08-08 17:30:00 [信息] 微信任务执行成功：发送消息 #12345678",
        )

    def test_renders_uia_probe_in_chinese(self) -> None:
        output = self.render(
            "uia.operation_probe",
            uia_exposed=True,
            reason="UIA 已暴露",
        )
        self.assertIn("微信 UIA 探测完成：可用", output)

    def test_renders_contact_search_in_chinese(self) -> None:
        task_output = self.render(
            "task.succeeded",
            task_type="search_contacts",
            task_id="12345678-abcd",
        )
        search_output = self.render(
            "contact.search.done",
            match_count=1,
            exact_count=1,
        )
        self.assertIn("搜索联系人", task_output)
        self.assertIn("联系人搜索完成：匹配 1 条，精确匹配 1 条", search_output)

    def test_renders_new_capability_logs_in_chinese(self) -> None:
        unread = self.render("messages.unread.read", session_count=2, total_unread=3)
        files = self.render("files.send.done", friend="文件传输助手", file_count=1)
        history = self.render("task.succeeded", task_type="read_chat_history", task_id="abcd1234")
        self.assertIn("未读消息汇总完成：2 个会话，共 3 条", unread)
        self.assertIn("文件发送完成", files)
        self.assertIn("读取聊天记录", history)

    def test_friend_add_line_shows_the_name_we_will_reply_with(self) -> None:
        output = self.render(
            "friend.add.done",
            number="13800138000",
            name="张三",
            session_name="张三-仓库工",
        )
        self.assertEqual(
            output,
            "2026-08-08 17:30:00 [信息] 好友申请已提交：13800138000（张三-仓库工）",
        )

    def test_friend_add_line_survives_a_run_that_read_no_name(self) -> None:
        output = self.render("friend.add.done", number="13800138000")
        self.assertEqual(
            output, "2026-08-08 17:30:00 [信息] 好友申请已提交：13800138000"
        )
class DailyLogFileTests(unittest.TestCase):
    """日志按天分文件（用户 2026-08-15：「里面应该是按日期天的日志」）。"""

    def setUp(self) -> None:
        import tempfile

        self.dir = Path(tempfile.mkdtemp()) / "logs"

    def _handler(self, keep_days: int = 30):
        from wxclient.logging_config import DailyFileHandler

        handler = DailyFileHandler(self.dir, keep_days=keep_days)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.addCleanup(handler.close)
        return handler

    def _record(self, message: str, created: float) -> logging.LogRecord:
        record = logging.LogRecord("wx", logging.INFO, __file__, 1, message, None, None)
        record.created = created
        return record

    def test_each_day_gets_its_own_file_named_after_that_day(self) -> None:
        import time

        handler = self._handler()
        day_one = time.mktime((2026, 8, 14, 23, 59, 0, 0, 0, -1))
        day_two = time.mktime((2026, 8, 15, 0, 1, 0, 0, 0, -1))

        handler.emit(self._record("昨天的事", day_one))
        handler.emit(self._record("今天的事", day_two))

        self.assertEqual(sorted(path.name for path in self.dir.glob("*.log")),
                         ["gateway-2026-08-14.log", "gateway-2026-08-15.log"])
        # ★ 当天那份也得带日期：TimedRotatingFileHandler 会把今天写进不带日期的
        # gateway.log，按日期找日志时最想要的那份反而没名字，所以没用它。
        self.assertFalse((self.dir / "gateway.log").exists())
        self.assertIn("昨天的事", (self.dir / "gateway-2026-08-14.log").read_text("utf-8"))
        self.assertIn("今天的事", (self.dir / "gateway-2026-08-15.log").read_text("utf-8"))

    def test_old_days_are_dropped_so_the_folder_does_not_grow_forever(self) -> None:
        import time

        handler = self._handler(keep_days=2)
        for day in (12, 13, 14, 15):
            handler.emit(self._record(f"第 {day} 天", time.mktime((2026, 8, day, 10, 0, 0, 0, 0, -1))))

        self.assertEqual(sorted(path.name for path in self.dir.glob("*.log")),
                         ["gateway-2026-08-14.log", "gateway-2026-08-15.log"])

    def test_a_write_that_fails_does_not_take_the_caller_down(self) -> None:
        """★ 日志写不出去是小事，把正在发消息的那条线程带崩是大事。"""
        handler = self._handler()
        handler.emit(self._record("先开一份", 0))
        handler._stream.close()  # 模拟句柄没了

        handler.emit(self._record("再写一条", 0))  # 不许抛


if __name__ == "__main__":
    unittest.main()
