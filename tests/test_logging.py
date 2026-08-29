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


class NoEnglishLeaksTests(ChineseLogRendererTests):
    """★ 实时日志页是给招聘专员看的：任何一条落到默认分支的英文事件名都会原样摆到他面前。

    2026-08-16 盘点时有 13 条是这样漏出去的（真机上见过
    `message.send.stuck_in_input，friend="王建国"，left=5`）。
    """

    def test_every_emitted_event_has_a_chinese_sentence(self) -> None:
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "wxclient"
        emitted: set[str] = set()
        call = re.compile(
            r'\b\w*log(?:ger)?\w*\.(?:debug|info|warning|error|critical|exception)'
            r'\(\s*"([a-z0-9_.]+)"'
        )
        for path in root.rglob("*.py"):
            if "__pycache__" in str(path) or path.name == "logging_config.py":
                continue
            emitted |= set(call.findall(path.read_text(encoding="utf-8")))

        config = (root / "logging_config.py").read_text(encoding="utf-8")
        # 只比英文事件名这一档。网关业务层的事件名本身就是整句中文，归
        # `GatewayLineTests` 管——混在一起的话，那边每加一条渲染都会把这里判成
        # 「分支没人发了」。
        rendered = set(re.findall(r'event == "([a-z0-9_.]+)"', config))

        self.assertFalse(emitted - rendered,
                         f"这些事件没有中文句子，会原样印英文：{sorted(emitted - rendered)}")
        self.assertFalse(rendered - emitted,
                         f"这些分支已经没人发了，删掉免得误导：{sorted(rendered - emitted)}")

    def test_a_message_stuck_in_the_input_box_says_so_plainly(self) -> None:
        output = self.render("message.send.stuck_in_input", friend="王建国", left=5)

        self.assertIn("卡在输入框里没发出去", output)
        self.assertIn("王建国", output)
        self.assertIn("对方没有收到", output)

    def test_a_lost_lease_says_which_lease_was_lost(self) -> None:
        output = self.render("worker.lease_lost", worker_id="w-1", task_id="12345678-abcd",
                             worker_lease=False, task_lease=True)

        self.assertIn("租约丢失，已停手", output)
        self.assertIn("#12345678", output)
        self.assertIn("执行器租约 丢", output)
        self.assertIn("任务租约 在", output)


class GatewayLineTests(ChineseLogRendererTests):
    """★ 网关业务层：「为什么没回这个人」整条链路的日志。

    这一层的事件名已经是整句中文，所以躲过了 `NoEnglishLeaksTests`——但它落在通用兜底上，
    实时日志里显示成 `本轮不回复，account="wx_1"，会话="张三"，reason="转人工：…"`。
    人要的是「为什么没回」，看到的是 JSON 键值对（用户 2026-08-29）。
    """

    def _gateway_keys(self) -> list[str]:
        import re
        from pathlib import Path

        config = (Path(__file__).resolve().parent.parent / "wxclient" / "logging_config.py").read_text("utf-8")
        block = config.split("def _render_gateway_line")[1].split("def _render_event")[0]
        return re.findall(r'event == "([^"]+)"', block)

    def test_every_branch_matches_a_real_log_call(self) -> None:
        """★ 分支的键就是 `_log.info(...)` 的第一个参数——那边改措辞，这里必须跟着改。

        对不上不会报错，只会静静地落回 kv 串（正是这次要修的那个样子），所以拿测试钉住。
        """
        from pathlib import Path

        gateway = Path(__file__).resolve().parent.parent / "wxclient" / "gateway"
        source = "\n".join(path.read_text("utf-8") for path in gateway.glob("*.py"))
        keys = self._gateway_keys()

        self.assertTrue(keys, "一条都没解析出来，多半是 `_render_gateway_line` 改名了")
        missing = [key for key in keys if f'"{key}"' not in source]
        self.assertFalse(missing, f"这些分支在网关里找不到对应的日志调用：{missing}")

    def test_no_branch_leaves_a_key_value_tail(self) -> None:
        """渲染过的行不许再出现 `key="value"` 那截尾巴，也不许把 account 摆给人看。"""
        rendered = [
            self.render("读完这个会话", 会话="张三", 读到=6, 新消息=0, 最近一条="嗯嗯"),
            self.render("本轮不回复", account="wx_1", 会话="张三", reason="转人工：对方在问是不是自动回复"),
            self.render("出站被守门拦下", account="wx_1", 会话="张三", reason="不在允许发送的时间窗 09:00-20:00"),
            self.render("已投递回复", account="wx_1", 会话="张三", segments=2),
        ]
        for line in rendered:
            self.assertNotIn('="', line)
            self.assertNotIn("account", line)

    def test_read_six_but_none_new_says_so(self) -> None:
        """真机上最迷惑的一条：读到 6 条、一条都不新，于是没有下文（2026-08-14 用户就是这么问的）。"""
        output = self.render("读完这个会话", 会话="张三", 读到=6, 新消息=0, 最近一条="好的")

        self.assertIn("读完会话「张三」：读到 6 条", output)
        self.assertIn("没有新消息", output)
        self.assertIn("这一轮不会回", output)

    def test_not_replying_says_why(self) -> None:
        output = self.render("本轮不回复", account="wx_1", 会话="张三",
                             reason="转人工：对方在问是不是自动回复 / 要求真人")

        self.assertEqual(
            output,
            "2026-08-08 17:30:00 [信息] 本轮不回复「张三」，原因：转人工：对方在问是不是自动回复 / 要求真人",
        )

    def test_blocked_by_the_guard_says_which_gate(self) -> None:
        output = self.render("出站被守门拦下", account="wx_1", 会话="张三",
                             reason="「发消息」在过去一小时内已做 20 次，达到上限 20")

        self.assertIn("出站被守门拦下：「张三」", output)
        self.assertIn("达到上限 20", output)

    def test_all_from_self_explains_it_is_not_an_error(self) -> None:
        output = self.render("这几条全是自己说的，不触发回复，等对方开口", 会话="张三", 条数=2)

        self.assertIn("「张三」这 2 条全是自己说的", output)
        self.assertIn("等对方开口", output)

    def test_a_batch_left_pending_says_whether_it_counts_as_a_failure(self) -> None:
        """`retry` 和 `defer` 的区别决定它会不会被放弃，看的人必须分得出来。"""
        retry = self.render("这一轮没把话说出去，本批留在待处理队列等重投", account="wx_1",
                            原因="AI 端这一轮没有给出结果", 计入失败=True, 已重试=3)
        defer = self.render("这一轮没把话说出去，本批留在待处理队列等重投", account="wx_1",
                            原因="不在允许发送的时间窗 09:00-20:00", 计入失败=False, 已重试=0)

        self.assertIn("计一次失败，已重试 3 次", retry)
        self.assertIn("不计失败（这类拦截会自己过期）", defer)

    def test_an_unmatched_gateway_event_still_falls_back(self) -> None:
        """★ 没登记的照旧走通用兜底——加这段不该让别的日志消失。"""
        output = self.render("清掉过期聊天记录", 条数=5, 保留天数=14)

        self.assertIn("清掉过期聊天记录", output)
        self.assertIn("条数=5", output)
