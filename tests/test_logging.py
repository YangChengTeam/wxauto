from __future__ import annotations

import logging
import unittest
from pathlib import Path

from wxclient import logrole
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
            "2026-08-08 17:30:00 [信息] [本机] 微信任务执行成功：发送消息 #12345678",
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
            "2026-08-08 17:30:00 [信息] [本机] 好友申请已提交：13800138000（张三-仓库工）",
        )

    def test_friend_add_line_survives_a_run_that_read_no_name(self) -> None:
        output = self.render("friend.add.done", number="13800138000")
        self.assertEqual(
            output, "2026-08-08 17:30:00 [信息] [本机] 好友申请已提交：13800138000"
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

    def test_attachment_save_steps_read_like_progress(self) -> None:
        step = self.render("chat.attachment.save.step", friend="李先洋", step=3, total=6, stage="右键点「另存为」", detail="")
        self.assertIn("另存进度 3/6", step)
        self.assertIn("右键点「另存为」", step)
        failed = self.render("chat.attachment.save.failed", friend="李先洋", step=3, total=6,
                             stage="保存对话框弹出", error="点了「另存为」但保存对话框没出现")
        self.assertIn("卡在「保存对话框弹出」", failed)
        self.assertIn("已完成 3/6 步", failed)

    def test_a_message_stuck_in_the_input_box_says_so_plainly(self) -> None:
        output = self.render("message.send.stuck_in_input", friend="王建国", left=5)

        self.assertIn("卡在输入框里没发出去", output)
        self.assertIn("王建国", output)
        self.assertIn("对方没有收到", output)

    def test_a_lost_lease_says_which_lease_was_lost(self) -> None:
        output = self.render("worker.lease_lost", worker_id="w-1", task_id="12345678-abcd",
                             worker_lease=False, task_lease=True)

        self.assertIn("续租失败", output)
        self.assertIn("结果可能记不上", output)
        self.assertNotIn("已停手", output)
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

    def test_attachment_progress_says_step_file_and_reason(self) -> None:
        ok = self.render("附件进度", 会话="李先洋", 文件="简历 (2).docx", 步骤="2/4", 说明="已另存到本机（7.2 KB）")
        self.assertIn("附件进度 2/4", ok)
        self.assertIn("「简历 (2).docx」已另存到本机", ok)
        self.assertNotIn("✗", ok)
        bad = self.render("附件进度", 会话="李先洋", 文件="简历.docx", 步骤="2/4", 失败=True,
                          说明="另存失败", 原因="右键菜单里没有「另存为」")
        self.assertIn("✗", bad)
        self.assertIn("原因：右键菜单里没有「另存为」", bad)
        self.assertNotIn("失败=True", bad)

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
            "2026-08-08 17:30:00 [信息] [本机] 本轮不回复「张三」，原因：转人工：对方在问是不是自动回复 / 要求真人",
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

class TurnProgressLineTests(ChineseLogRendererTests):
    """★ 一轮进行中的实时日志（用户 2026-08-29）。

    一轮在 AI 端要跑几十秒，超时 180s 还会重连再试一次。原来这段时间里那台 Windows
    电脑上一个字都没有——「读到了消息、没有回复、屏幕上什么也没有」就是这么来的。
    """

    def test_it_says_what_was_fed_to_the_ai(self) -> None:
        first = self.render("喂给 AI 端", 会话="张三", 本轮=2, 历史行=12,
                            认得这个人=True, 第几次=1)
        again = self.render("喂给 AI 端", 会话="张三", 本轮=2, 历史行=0,
                            认得这个人=False, 第几次=2)

        self.assertIn("喂给 AI 端：「张三」，本轮 2 条 + 历史 12 行，对面已归位到候选人", first)
        self.assertNotIn("第 1 次", first)          # 正常那次不提「第几次」，免得每轮都刷
        self.assertIn("还不知道对面是哪个候选人", again)
        self.assertIn("第 2 次尝试", again)

    def test_fed_line_lists_the_attachments_that_went_along(self) -> None:
        """104：送了哪几份附件、多大，得在「喂给 AI 端」这一行就看见，不用翻另存那几行去对。"""
        output = self.render("喂给 AI 端", 会话="张三", 本轮=1, 历史行=0, 认得这个人=True, 第几次=1,
                             附件=2, 附件清单=[{"文件": "身份证.jpg", "类型": "image", "字节": 350 * 1024},
                                             {"文件": "简历.pdf", "类型": "file", "字节": 2 * 1024 * 1024}])
        self.assertIn("随行附件 2 份：身份证.jpg（图片，350 KB）、简历.pdf（文件，2.0 MB）", output)
        self.assertNotIn("随行附件", self.render("喂给 AI 端", 会话="张三", 本轮=1, 历史行=0,
                                                认得这个人=True, 第几次=1, 附件=0, 附件清单=[]))

    def test_left_behind_names_each_file_and_why(self) -> None:
        output = self.render("这一轮有附件没能带上", 会话="张三", 个数=2,
                             清单=[{"文件": "7.jpg", "原因": "超出一轮最多 6 份的上限"},
                                  {"文件": "旧图.jpg", "原因": "文件已不在本机（过了保鲜期或被清掉）"}])
        self.assertIn("「张三」这一轮有 2 份附件没能带上：7.jpg（超出一轮最多 6 份的上限）、"
                      "旧图.jpg（文件已不在本机（过了保鲜期或被清掉））", output)
        self.assertIn("不会上传", output)

    def test_claimed_materials_say_whether_they_will_be_uploaded(self) -> None:
        """上传在 AI 端做、这台电脑看不到结果，但「会不会传」从卡上就能判：有授权原话的才会。"""
        output = self.render("AI 端认领了材料", 会话="张三",
                             清单=[{"文件": "身份证.jpg", "类型": "身份证正面", "有授权原话": True, "字段数": 3},
                                  {"文件": "简历.pdf", "类型": "简历", "有授权原话": False, "字段数": 0}],
                             未认领=["合影.jpg"])
        self.assertIn("身份证.jpg → 身份证正面（有授权原话，会上传到后端，识别到 3 个字段）", output)
        self.assertIn("简历.pdf → 简历（没有授权原话，不会上传，要传得专员自己传）", output)
        self.assertIn("未认领：合影.jpg（这些不会上传）", output)

    def test_unclaimed_attachments_say_nothing_will_be_uploaded(self) -> None:
        output = self.render("AI 端没认领这一轮的附件", 会话="张三", 送去=["a.jpg", "b.pdf"])
        self.assertIn("AI 端没认领「张三」这一轮的附件（a.jpg、b.pdf）：不会上传到后端", output)

    def test_a_stage_reads_like_a_status_line(self) -> None:
        """AI 端本来就在流里发阶段（055），网关此前整个丢掉。"""
        self.assertIn("AI 端：正在读取资料",
                      self.render("AI 端阶段", 会话="张三", 阶段="正在读取资料", key="reading"))

    def test_the_heartbeat_says_how_long_and_what_happens_next(self) -> None:
        output = self.render("AI 端还在跑这一轮", 会话="张三", 已用秒=45, 上限秒=180)

        self.assertIn("已 45 秒", output)
        self.assertIn("超过 180 秒算超时，会重连再试一次", output)

    def test_a_finished_turn_says_whether_a_card_came_back(self) -> None:
        got = self.render("AI 端这一轮跑完了", 会话="张三", 已用秒=38, 有卡片=True)
        none = self.render("AI 端这一轮跑完了", 会话="张三", 已用秒=38, 有卡片=False)

        self.assertIn("用时 38 秒，拿到卡片", got)
        self.assertIn("但没有卡片", none)

    def test_an_ai_error_carries_the_message_not_just_the_code(self) -> None:
        """原来只记 code——现场看到一个错误码，猜不出发生了什么。"""
        output = self.render("AI 端返回错误", 会话="张三", code="upstream_error",
                             原因="上游模型繁忙", 已用秒=12)

        self.assertIn("错误码 upstream_error", output)
        self.assertIn("原因：上游模型繁忙", output)
        self.assertIn("消息留着下一轮重投", output)

    def test_a_dropped_connection_says_what_it_means_for_the_messages(self) -> None:
        """★ 「和 AI 端断了」是这条链路最要紧的一句：断开时在跑的那一轮当场作废。"""
        output = self.render("读循环结束", error="connection closed abnormally")

        self.assertIn("和 AI 端的连接断了", output)
        self.assertIn("消息留着重投", output)

    def test_being_unable_to_connect_says_messages_are_not_lost(self) -> None:
        output = self.render("AI 端连接不可用", account="wx_1", error="Connection refused")

        self.assertIn("连不上 AI 端", output)
        self.assertIn("攒着，通了再回", output)

    def test_the_two_attempts_have_different_consequences(self) -> None:
        """第 1 次还会重连再试，第 2 次就是这一轮到此为止——看的人必须分得出来。"""
        first = self.render("这一轮没跑通", 会话="张三", 原因="会话 s_1 一轮超时",
                            第几次=1, 已用秒=180)
        second = self.render("这一轮没跑通", 会话="张三", 原因="会话 s_1 一轮超时",
                             第几次=2, 已用秒=180)

        self.assertIn("重连后再试一次", first)
        self.assertIn("两次都没跑通，这批消息留到下一轮重投", second)



class LogRoleTests(unittest.TestCase):
    """角色列（对端）：这一行日志说的是跟谁打交道。"""

    def setUp(self) -> None:
        self.renderer = ChineseLogRenderer()

    def render(self, *, logger_name: str | None = None, **payload: object) -> str:
        event_dict: dict[str, object] = {
            "timestamp": "2026-08-30 09:00:00",
            "level": "info",
            "event": "随便一句",
            **payload,
        }
        if logger_name is not None:
            event_dict["logger"] = logger_name
        return self.renderer(logging.getLogger("t"), "info", event_dict)

    def test_模块默认角色(self) -> None:
        """一个模块只跟一方打交道的，登记一次就够，不必逐条标。"""
        self.assertEqual(logrole.module_role("wxclient.gateway.wsclient"), logrole.AI)
        self.assertEqual(logrole.module_role("wxclient.gateway.ticket"), logrole.BACKEND)
        self.assertEqual(logrole.module_role("wxclient.wechat_api.uia"), logrole.WECHAT)
        self.assertEqual(logrole.module_role("wxclient.ui.status_reporter"), logrole.BACKEND)

    def test_没登记的算本机(self) -> None:
        """宁可少认，不可乱认——猜错的角色比没有角色更误导人。"""
        self.assertEqual(logrole.module_role("wxclient.gateway.runner"), logrole.LOCAL)
        self.assertEqual(logrole.module_role("third_party.pywechat.whatever"), logrole.LOCAL)
        self.assertEqual(logrole.module_role(None), logrole.LOCAL)
        self.assertEqual(logrole.module_role(""), logrole.LOCAL)

    def test_子模块盖得住父包(self) -> None:
        """取最长的那条前缀：`queue.worker` 是微信，`queue` 别的东西不是。"""
        self.assertEqual(logrole.module_role("wxclient.queue.worker"), logrole.WECHAT)
        self.assertEqual(logrole.module_role("wxclient.queue.store"), logrole.LOCAL)

    def test_显式标注盖过模块默认(self) -> None:
        """混着跟两三方打交道的模块靠这条：调用处说了算。"""
        self.assertEqual(
            logrole.role_of("wxclient.gateway.wsclient", logrole.BACKEND), logrole.BACKEND
        )
        self.assertEqual(logrole.role_of("wxclient.gateway.runner", logrole.AI), logrole.AI)
        # 空串/非字符串不算「说了」，退回模块默认
        self.assertEqual(logrole.role_of("wxclient.gateway.ticket", "  "), logrole.BACKEND)
        self.assertEqual(logrole.role_of("wxclient.gateway.ticket", None), logrole.BACKEND)

    def test_角色进前缀(self) -> None:
        line = self.render(logger_name="wxclient.gateway.wsclient")
        self.assertEqual(line, "2026-08-30 09:00:00 [信息] [AI端] 随便一句")

    def test_显式对端进前缀(self) -> None:
        line = self.render(logger_name="wxclient.gateway.runner", **{logrole.ROLE_KEY: logrole.AI})
        self.assertEqual(line, "2026-08-30 09:00:00 [信息] [AI端] 随便一句")

    def test_对端和logger都不漏进消息尾巴(self) -> None:
        """这两个键是「这行日志的元信息」，不是业务参数，不该出现在 kv 串里。"""
        line = self.render(
            logger_name="wxclient.gateway.runner", 会话="张三", **{logrole.ROLE_KEY: logrole.AI}
        )
        self.assertNotIn("对端=", line)
        self.assertNotIn("logger=", line)
        self.assertIn("张三", line)

    def test_认不出模块也有角色(self) -> None:
        """第三方库直接写进来的行也得有一格，不能是空的方括号。"""
        line = self.render()
        self.assertEqual(line, "2026-08-30 09:00:00 [信息] [本机] 随便一句")


class BoundRoleLoggerTests(unittest.TestCase):
    """混着跟几方打交道的模块，绑出来的 logger 真的带上了角色吗。

    钉的是**接线**而不是措辞：`runner._ai` 这类绑定一旦写错（比如漏了 ROLE_KEY），
    渲染出来照样是一行正常日志，只是角色列悄悄退回「本机」——界面上看不出错。
    """

    def test_绑定的角色渲染得出来(self) -> None:
        from wxclient.gateway import commands, inbound, outbound, runner

        cases = [
            (runner._ai, logrole.AI),
            (runner._backend, logrole.BACKEND),
            (runner._wechat, logrole.WECHAT),
            (inbound._ai, logrole.AI),
            (inbound._backend, logrole.BACKEND),
            (inbound._local, logrole.LOCAL),
            (outbound._backend, logrole.BACKEND),
            (outbound._local, logrole.LOCAL),
            (commands._ai, logrole.AI),
            (commands._backend, logrole.BACKEND),
            (commands._local, logrole.LOCAL),
        ]
        for bound, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(bound._context.get(logrole.ROLE_KEY), expected)


class UiTracebackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test")

    def test_ui_renderer_hides_the_python_traceback(self) -> None:
        """界面上的实时日志不贴执行栈，只留最后一行；日志文件那份照样完整（2026-09-24）。"""
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "wxclient/queue/worker.py", line 121, in run_once\n'
            "    outcome = self.dispatch(task)\n"
            "RuntimeError: 窗口不见了"
        )
        event = {"timestamp": "2026-09-24 15:42:44", "level": "error", "event": "看不懂的事", "exception": traceback}
        ui = ChineseLogRenderer(show_traceback=False)(self.logger, "error", dict(event))
        self.assertNotIn("Traceback", ui)
        self.assertNotIn("worker.py", ui)
        self.assertIn("RuntimeError: 窗口不见了", ui)
        full = ChineseLogRenderer()(self.logger, "error", dict(event))
        self.assertIn("Traceback", full)
        self.assertIn("worker.py", full)
