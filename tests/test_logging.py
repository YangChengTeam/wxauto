from __future__ import annotations

import logging
import unittest

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
if __name__ == "__main__":
    unittest.main()
