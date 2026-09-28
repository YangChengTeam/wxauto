"""AI 端下发微信能力指令（066 Phase 1）——白名单 / 守门 / 幂等 / 回执。

全部在 Linux 跑，不碰真微信：runtime 用假实现，只断言「投了什么任务、拒了什么、拒的理由」。
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from structlog.testing import capture_logs

from wxclient.gateway import CommandExecutor, GatewayStore, OutboundGuard
from wxclient.gateway.capabilities import (
    CAP_DELIVERY_RECEIPT,
    CAP_GROUP_MESSAGE,
    CAP_HOLIDAY_GREETING,
    CAP_MOMENTS,
    CAP_PRIVATE_MESSAGE,
    CAP_READ,
    CAP_SEND_FILES,
    capability_of,
    capability_status,
    probe_capabilities,
)
from wxclient.gateway.download import DownloadError, FileFetcher, FileRef
from wxclient.gateway.guard import (
    ACTION_FRIEND,
    ACTION_GROUP,
    ACTION_MOMENTS,
    FRIEND_WRITE_ACTION,
    RateLimit,
)
from wxclient.gateway.schedule import MAX_LATE_HOURS, ScheduleLoop, parse_schedule_at
from wxclient.queue import TaskType

BEIJING = timezone(timedelta(hours=8))
WORKDAY = datetime(2026, 8, 13, 14, 0, tzinfo=BEIJING)      # 工作日下午两点
MIDNIGHT = datetime(2026, 8, 13, 3, 0, tzinfo=BEIJING)      # 凌晨三点
ACCOUNT = "zk_weixin"


class FakeRuntime:
    """★ 回执里**必须带 `status`**：真的 `WeChatRuntime.submit` 返回的是任务终态
    （`succeeded` / `failed` / `blocked`，见 `runtime.task_to_dict`），失败时**一个字都不抛**。
    Fake 里漏掉这个字段，「任务失败」这条路在测试里就根本走不到——
    2026-08-27 真机上「没搜到这个号」照样被算成 `friend_state=requested`，
    正是因为这里少了一个字段，没有任何一条用例能红。
    """

    def __init__(
        self,
        *,
        boom: bool = False,
        result: dict[str, Any] | None = None,
        status: str = "succeeded",
    ) -> None:
        self.calls: list[tuple[TaskType, dict[str, Any], str]] = []
        self._boom = boom
        self._result = result if result is not None else {"ok": True}
        self._status = status

    async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
        if self._boom:
            raise RuntimeError("微信没登录")
        self.calls.append((task_type, dict(payload or {}), confirmation))
        return {"status": self._status, "result": dict(self._result),
                "task_id": f"t{len(self.calls)}"}


class CommandTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.now = WORKDAY

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, runtime: FakeRuntime, **kwargs: Any) -> CommandExecutor:
        kwargs.setdefault("allow_real_actions", True)
        kwargs.setdefault("confirmation", "确认")
        return CommandExecutor(
            runtime=runtime,
            store=self.store,
            guard=OutboundGuard(
                self.store,
                max_sends_per_hour=kwargs.pop("max_sends_per_hour", 20),
                active_hours=kwargs.pop("active_hours", "09:00-21:00"),
                write_intervals=kwargs.pop("write_intervals", None),
                action_limits=kwargs.pop("action_limits", None),
            ),
            account_id=ACCOUNT,
            clock=lambda: self.now,
            **kwargs,
        )


class TaskFailureTests(CommandTestCase):
    """微信任务没成，回执就不能说 done。

    2026-08-29 线上：`发布朋友圈#b390a5ee` 死在「WeChat profile pane did not appear」，
    队列里明明是 `failed`，AI 端收到的却是 `done` —— 因为执行器只在 `submit` **抛异常**
    时才算失败，而队列的终态是**正常返回**的一个 dict。同 2026-08-27 加好友那条
    （`5a85171`）是一个毛病：那次只在 `_friend_state` 里补了 status 判断，没往上收。
    """

    async def test_失败的任务回执是_failed_不是_done(self) -> None:
        runtime = FakeRuntime(status="failed")
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "post_moments",
            "args": {"text": "今天招工"},
        })
        self.assertEqual(result.status, "failed")

    async def test_失败原因原样带回去(self) -> None:
        class _Rt(FakeRuntime):
            async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
                await super().submit(task_type, payload, confirmation=confirmation)
                return {"status": "failed", "last_error": "WeChat profile pane did not appear"}

        result = await self._executor(_Rt()).execute({
            "command_id": "c1", "op": "post_moments", "args": {"text": "今天招工"},
        })
        self.assertIn("profile pane", result.reason)

    async def test_查无此人带稳定码报上去(self) -> None:
        """队列判成「微信里搜不到这个人」→ 回执原因以 CONTACT_NOT_FOUND 打头，业务端靠码归类（2026-09-24）。"""
        class _Rt(FakeRuntime):
            async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
                await super().submit(task_type, payload, confirmation=confirmation)
                return {
                    "status": "failed",
                    "last_error": "微信里搜不到「张三」这个会话（备注名对不上，或还不是好友），消息没有发出去",
                    "result": {"outcome": "not_sent", "reason_code": "CONTACT_NOT_FOUND", "retry_suppressed": True},
                }

        result = await self._executor(_Rt()).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.reason.startswith("CONTACT_NOT_FOUND："), result.reason)
        self.assertIn("没有发出去", result.reason)
        self.assertNotIn("结果未知", result.reason)
        self.assertEqual(result.result, {"reason_code": "CONTACT_NOT_FOUND", "outcome": "not_sent"})

    async def test_没做成就把额度还回去(self) -> None:
        """额度是动微信**之前**占的。一件没做成还扣着，等于一次失败白烧一个名额。"""
        ex = self._executor(FakeRuntime(status="failed"), max_sends_per_hour=1)
        first = await ex.execute({
            "command_id": "c1", "op": "send_message",
            "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(first.status, "failed")

        ok = await self._executor(FakeRuntime(), max_sends_per_hour=1).execute({
            "command_id": "c2", "op": "send_message",
            "args": {"to": "李四", "text": "在吗"},
        })
        self.assertEqual(ok.status, "done", ok.reason)

    async def test_等超时了不还额度_因为微信可能已经动过(self) -> None:
        """★ 「没结论」和「没做成」是两件事。超时那一轮微信**可能已经发出去了**，
        额度还回去 = 下一条指令拿它把同样的动作再做一遍。只把「不知道」如实报上去。"""
        class _Rt(FakeRuntime):
            async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
                await super().submit(task_type, payload, confirmation=confirmation)
                return {"status": "running", "timed_out": True}

        ex = self._executor(_Rt(), max_sends_per_hour=1)
        first = await ex.execute({
            "command_id": "c1", "op": "send_message",
            "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(first.status, "failed")

        again = await self._executor(FakeRuntime(), max_sends_per_hour=1).execute({
            "command_id": "c2", "op": "send_message",
            "args": {"to": "李四", "text": "在吗"},
        })
        self.assertEqual(again.status, "rejected", "名额没还，这条该被配额挡住")

    async def test_加好友这档_超时没确认加上就不算用掉配额(self) -> None:
        """★ 用户 2026-09-22：「没有加上好友不算用掉配额」。自动通过每 15 分钟一趟、每趟预留 5，
        原来超时按预留数全额扣——一趟没等到结论就把整小时「加好友」额度烧光，一个人没加，
        计划里当天第一次 add_friend 就撞上「过去一小时内已做 5 次」。"""
        class _Rt(FakeRuntime):
            async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
                await super().submit(task_type, payload, confirmation=confirmation)
                return {"status": "running", "timed_out": True}

        limits = {ACTION_FRIEND: RateLimit(5, 3600)}
        first = await self._executor(_Rt(), action_limits=limits).execute({
            "command_id": "c1", "op": "accept_new_friends", "args": {"limit": 5},
        })
        self.assertEqual(first.status, "failed")
        self.assertEqual(self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action=ACTION_FRIEND), 0)

        again = await self._executor(FakeRuntime(), action_limits=limits,
                                     write_intervals={FRIEND_WRITE_ACTION: 0}).execute({
            "command_id": "c2", "op": "add_friend", "args": {"number": "13800000001"},
        })
        self.assertEqual(again.status, "done", again.reason)

    async def test_加好友超时但驱动层已确认通过的那几个照记(self) -> None:
        """超时不等于一个没做：result 里驱动层已经记下的 `accepted` 是确认过的，按它结算。"""
        class _Rt(FakeRuntime):
            async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
                await super().submit(task_type, payload, confirmation=confirmation)
                return {"status": "running", "timed_out": True,
                        "result": {"items": ["张三", "李四", "王五"], "accepted": ["张三", "李四"]}}

        limits = {ACTION_FRIEND: RateLimit(5, 3600)}
        result = await self._executor(_Rt(), action_limits=limits).execute({
            "command_id": "c1", "op": "accept_new_friends", "args": {"limit": 5},
        })
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action=ACTION_FRIEND), 2)

    async def test_早就是好友的号不占加好友配额(self) -> None:
        """`already_friend` = 微信一下没动、没有申请发出去，不算用掉「加好友」名额。"""
        limits = {ACTION_FRIEND: RateLimit(1, 3600)}
        first = await self._executor(FakeRuntime(result={"already_friend": True, "profile_name": "张三"}),
                                     action_limits=limits, write_intervals={FRIEND_WRITE_ACTION: 0}).execute({
            "command_id": "c1", "op": "add_friend", "args": {"number": "13800000001"},
        })
        self.assertEqual(first.status, "done", first.reason)
        self.assertEqual(self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action=ACTION_FRIEND), 0)

        again = await self._executor(FakeRuntime(result={"already_friend": False}),
                                     action_limits=limits, write_intervals={FRIEND_WRITE_ACTION: 0}).execute({
            "command_id": "c2", "op": "add_friend", "args": {"number": "13800000002"},
        })
        self.assertEqual(again.status, "done", again.reason)
        self.assertEqual(self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action=ACTION_FRIEND), 1)

    async def test_私聊超时仍不还额度_口径只改加好友这档(self) -> None:
        """发消息那档维持原样：超时可能已经发出去，还了名额就是重复发。"""
        class _Rt(FakeRuntime):
            async def submit(self, task_type, payload=None, *, confirmation="", **_kw):
                await super().submit(task_type, payload, confirmation=confirmation)
                return {"status": "running", "timed_out": True}

        await self._executor(_Rt(), max_sends_per_hour=1).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(self.store.sends_in_last_hour(ACCOUNT, self.now), 1)


class SentEchoRecordTests(CommandTestCase):
    """计划发出去的那句话，下一轮读回来也要认得出是自己说的（2026-09-09 自问自答事故）。"""

    async def test_send_message_records_its_text(self) -> None:
        await self._executor(FakeRuntime()).execute({
            "command_id": "c1", "op": "send_message",
            "args": {"to": "张三", "text": "您好，看到您在找普工"},
        })
        self.assertTrue(self.store.claim_sent_echo(ACCOUNT, "张三", "您好，看到您在找普工"))

    async def test_failed_send_records_nothing(self) -> None:
        await self._executor(FakeRuntime(status="failed")).execute({
            "command_id": "c1", "op": "send_message",
            "args": {"to": "张三", "text": "在吗"},
        })
        self.assertFalse(self.store.claim_sent_echo(ACCOUNT, "张三", "在吗"))

    async def test_add_friend_greeting_is_recorded_under_the_session_name(self) -> None:
        """验证语在对方通过后会以我们的气泡出现在会话里，第一次读那个会话时要认得出是自己说的。"""
        runtime = FakeRuntime(result={"already_friend": False, "session_name": "候选人-张三"})
        await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "13800000001", "greetings": "你好，我是劳小智的招聘老师"},
        })
        self.assertTrue(self.store.claim_sent_echo(ACCOUNT, "候选人-张三", "你好，我是劳小智的招聘老师"))

    async def test_the_label_goes_into_the_request_box_instead_of_the_greeting(self) -> None:
        """★ 用户 2026-09-24：「发送添加朋友申请」下面那格填 label（备注名），不填验证语。"""
        runtime = FakeRuntime(result={"already_friend": False, "session_name": "张三-仓库工"})
        await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "13800000001", "greetings": "你好", "remark": "张三-仓库工"},
        })
        _, payload, _ = runtime.calls[-1]
        self.assertEqual(payload["greetings"], "张三-仓库工")
        self.assertTrue(self.store.claim_sent_echo(ACCOUNT, "张三-仓库工", "张三-仓库工"),
                        "记账跟着填进去的那句走")

    async def test_the_known_session_name_is_what_goes_into_the_request_box(self) -> None:
        self.store.save_contacts(ACCOUNT, [
            {"number": "13800000001", "session_name": "候选人-张建国", "updated_at": "2026-08-18T09:00:00.000Z"},
        ])
        runtime = FakeRuntime()
        await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "13800000001", "greetings": "你好", "remark": "张先生"},
        })
        _, payload, _ = runtime.calls[-1]
        self.assertEqual(payload["greetings"], "候选人-张建国")

    async def test_without_a_label_the_greeting_stays(self) -> None:
        runtime = FakeRuntime()
        await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend", "args": {"number": "13800000001", "greetings": "你好"},
        })
        _, payload, _ = runtime.calls[-1]
        self.assertEqual(payload["greetings"], "你好")

    async def test_already_friend_records_no_greeting(self) -> None:
        runtime = FakeRuntime(result={"already_friend": True, "session_name": "老朋友"})
        await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "13800000001", "greetings": "你好", "remark": "老朋友"},
        })
        self.assertFalse(self.store.claim_sent_echo(ACCOUNT, "老朋友", "你好"), "没发申请就没有这句气泡")


class DeliveryReceiptTests(CommandTestCase):
    """真发出去一条微信 → 回执带**发送凭据**（面试天气提醒契约：`done` 不算发成，
    只认 `result.delivery_status=sent` + 非空 `result.message_id`）。凭据只在真动了微信且
    队列确认 succeeded 时给；dry-run / 失败 / 不是发消息的 op 一律没有——不编 ID 顶上。"""

    async def test_real_send_carries_a_stable_message_id(self) -> None:
        result = await self._executor(FakeRuntime()).execute({
            "command_id": "weather:r1", "op": "send_message", "args": {"to": "张三", "text": "预报有雨，记得带伞"},
        })
        self.assertEqual(result.status, "done")
        self.assertEqual((result.result or {}).get("delivery_status"), "sent")
        message_id = (result.result or {}).get("message_id")
        self.assertTrue(message_id and message_id.startswith("wxlocal_"), result.result)
        # 同一账号 + 同一指令 → 同一个 ID（重推幂等层已经挡住；万一漏过去，后端按它去重）。
        from wxclient.gateway.message_events import local_message_id
        self.assertEqual(message_id, local_message_id(ACCOUNT, "weather:r1"))

    async def test_failed_send_has_no_receipt(self) -> None:
        result = await self._executor(FakeRuntime(status="failed")).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(result.status, "failed")
        self.assertNotIn("message_id", result.result or {})

    async def test_dry_run_has_no_receipt(self) -> None:
        """只走流程不真发：私聊本来就被能力表拦成 rejected；就算放过去也不能给凭据。"""
        executor = self._executor(FakeRuntime(), allow_real_actions=False)
        self.assertEqual(executor._delivery_receipt("c1", "send_message", {"status": "succeeded"}), {})

    async def test_add_friend_has_no_receipt(self) -> None:
        result = await self._executor(FakeRuntime(result={"requested": True})).execute({
            "command_id": "c1", "op": "add_friend", "args": {"number": "13800000000"},
        })
        self.assertEqual(result.status, "done")
        self.assertNotIn("message_id", result.result or {})
        self.assertNotIn("delivery_status", result.result or {})


class PinnedAccountTests(CommandTestCase):
    """指令点名了微信账号（`args.wechat_account_id`）：本机登录的不是那个号就拒，微信一下不动。
    换号之后旧的面试天气提醒不许自动转投新号（契约 §3.5）。"""

    async def test_other_account_is_rejected_before_touching_wechat(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "weather:r1", "op": "send_message",
            "args": {"to": "张三", "text": "预报有雨", "wechat_account_id": "someone_else"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("不是本机登录的这个号", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_matching_account_goes_through_and_is_not_forwarded_to_wechat(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "weather:r1", "op": "send_message",
            "args": {"to": "张三", "text": "预报有雨", "wechat_account_id": ACCOUNT},
        })
        self.assertEqual(result.status, "done")
        _task, payload, _conf = runtime.calls[-1]
        self.assertNotIn("wechat_account_id", payload)


class WhitelistTests(CommandTestCase):
    async def test_known_op_is_translated_to_a_queue_task(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message",
            "args": {"to": "张三", "text": "面试改到明天下午三点"},
        })
        self.assertEqual(result.status, "done")
        task_type, payload, confirmation = runtime.calls[-1]
        self.assertIs(task_type, TaskType.SEND_MESSAGE)
        self.assertEqual(payload["friend"], "张三")
        self.assertEqual(payload["text"], "面试改到明天下午三点")
        self.assertFalse(payload["dry_run"])
        self.assertEqual(confirmation, "确认", "真实写操作必须带确认短语")

    async def test_unknown_op_is_rejected_without_touching_wechat(self) -> None:
        """地基不信任上游：AI 端推来没见过的 op，一个微信动作都不许做。"""
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "delete_everything", "args": {},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("不支持的能力", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_watch_chats_is_deliberately_not_exposed(self) -> None:
        """常驻扫描属于入站循环，被一条指令占住会把正常收消息饿死。"""
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "watch_chats", "args": {"friend": "张三"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertEqual(runtime.calls, [])

    async def test_missing_required_arg_is_rejected_not_guessed(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("text", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_read_op_runs_for_real_not_as_a_stub(self) -> None:
        """读类也要显式关 dry_run。

        底座的 dry_run 默认是 True，`check_new_friends` / `read_recent_moments` 在
        dry-run 下压根不碰微信、直接回一个空壳（`wechat_api/uia.py`）——AI 端拿到的
        会是「已完成、什么都没有」的假回执，比报错还难查。
        """
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "check_new_friends", "args": {},
        })
        self.assertEqual(result.status, "done")
        task_type, payload, _confirmation = runtime.calls[-1]
        self.assertIs(task_type, TaskType.CHECK_NEW_FRIENDS)
        self.assertFalse(payload["dry_run"])

    async def test_dry_run_when_real_actions_are_off(self) -> None:
        runtime = FakeRuntime()
        executor = self._executor(runtime, allow_real_actions=False)
        await executor.execute({"command_id": "c1", "op": "post_moments",
                                "args": {"text": "招 5 个普工，日结 200"}})
        _task_type, payload, confirmation = runtime.calls[-1]
        self.assertTrue(payload["dry_run"])
        self.assertEqual(confirmation, "", "没开真实动作就不该带确认短语")


class CommandGuardTests(CommandTestCase):
    async def test_write_blocked_outside_active_hours(self) -> None:
        runtime = FakeRuntime()
        self.now = MIDNIGHT
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("时间窗", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_read_also_blocked_outside_active_hours(self) -> None:
        """半夜翻联系人一样是「机器在跑」，读操作同样不许在窗外动。"""
        runtime = FakeRuntime()
        self.now = MIDNIGHT
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "search_contacts", "args": {"query": "张"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertEqual(runtime.calls, [])

    async def test_read_ignores_blacklist_and_rate_limit(self) -> None:
        """读不打扰任何人：对方在黑名单里、发送额度用光了，都不该挡住「查一下资料」。"""
        runtime = FakeRuntime()
        self.store.blacklist(ACCOUNT, "张三", "说了别联系")
        for _ in range(30):
            self.store.record_send(ACCOUNT, "张三", self.now - timedelta(minutes=1))
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "get_contact_profile", "args": {"friend": "张三"},
        })
        self.assertEqual(result.status, "done", result.reason)

    async def test_write_to_blacklisted_peer_is_rejected(self) -> None:
        runtime = FakeRuntime()
        self.store.blacklist(ACCOUNT, "张三", "说了别联系")
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("黑名单", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_active_send_counts_toward_the_hourly_limit(self) -> None:
        """主动触达要和被动回复共用同一个额度，否则限频等于没限。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime, max_sends_per_hour=2)
        for i in range(2):
            got = await executor.execute({
                "command_id": f"c{i}", "op": "send_message",
                "args": {"to": f"候选人{i}", "text": "你好"},
            })
            self.assertEqual(got.status, "done")
        blocked = await executor.execute({
            "command_id": "c9", "op": "send_message", "args": {"to": "候选人9", "text": "你好"},
        })
        self.assertEqual(blocked.status, "rejected")
        self.assertIn("上限", blocked.reason)


class GroupLaneTests(CommandTestCase):
    """群消息必须走群那一档配额（072 · docs/18 §3.4）。

    本机只在**收到过**群消息时才知道某个名字是群（`wechat_io` 里 `mark_peer_kind`）。
    计划驱动的主动触达（081）是往 `wechatGroupIds` 里的群发第一条——那时本机一无所知，
    于是整批群发都被算进私聊档，群那档形同虚设。

    出厂默认群消息已不限（用户 2026-09-18），这里显式配 6/时，测的是「分到哪一档」。
    """

    GROUP = "劳务派遣交流群"

    def _executor(self, runtime: FakeRuntime, **kwargs: Any) -> CommandExecutor:
        kwargs.setdefault("action_limits", {ACTION_GROUP: RateLimit(6, 3600.0)})
        return super()._executor(runtime, **kwargs)

    async def _send(self, executor, i: int, **args: Any):
        return await executor.execute({
            "command_id": f"g{i}", "op": "send_message",
            "args": {"to": self.GROUP, "text": "招人", **args},
        })

    async def test_a_never_seen_group_still_uses_the_group_quota(self) -> None:
        """AI 端说了这是群，就按群算——哪怕本机从没收到过这个群的消息。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        for i in range(6):
            got = await self._send(executor, i, to_group=True)
            self.assertEqual(got.status, "done", got.reason)
        blocked = await self._send(executor, 6, to_group=True)
        self.assertEqual(blocked.status, "rejected")
        self.assertIn("群消息", blocked.reason)
        self.assertEqual(len(runtime.calls), 6)

    async def test_the_hint_is_remembered(self) -> None:
        """认过一次就记下来：后面不带提示的指令照样按群算。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        await self._send(executor, 0, to_group=True)
        self.assertTrue(self.store.is_known_group(ACCOUNT, self.GROUP))
        for i in range(1, 6):
            self.assertEqual((await self._send(executor, i)).status, "done")
        self.assertEqual((await self._send(executor, 6)).status, "rejected")

    async def test_the_hint_can_only_tighten(self) -> None:
        """本机已经知道是群，上游说「不是群」也不放宽——提示只准收紧。"""
        runtime = FakeRuntime()
        self.store.mark_peer_kind(ACCOUNT, self.GROUP, is_group=True)
        executor = self._executor(runtime)
        for i in range(6):
            self.assertEqual((await self._send(executor, i, to_group=False)).status, "done")
        self.assertEqual((await self._send(executor, 6, to_group=False)).status, "rejected")

    async def test_private_chat_is_untouched(self) -> None:
        """没有提示、也没收过群消息的，仍是私聊那一档，别误伤（群档配了 6/时也拦不到它）。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        for i in range(7):
            got = await executor.execute({
                "command_id": f"p{i}", "op": "send_message",
                "args": {"to": "张三", "text": "在吗"},
            })
            self.assertEqual(got.status, "done", got.reason)
        self.assertFalse(self.store.is_known_group(ACCOUNT, "张三"))


class FriendStateTests(CommandTestCase):
    """加好友回执要说清「只是申请发出去了」还是「真加上了」（docs/18 §3.2）。

    AI 端拿这个字段写候选人的 `wechatAdded`。原来两个 op 的回执长得一模一样，
    「申请发出」会被当成「已经是好友」——后面所有发消息的动作都建在一个假前提上。
    """

    async def test_add_friend_only_claims_the_request_was_sent(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "wxid_zhang", "greetings": "你好"},
        })
        self.assertEqual(result.status, "done", result.reason)
        self.assertEqual((result.result or {}).get("friend_state"), "requested")

    async def test_a_failed_add_friend_claims_nothing(self) -> None:
        """★ 「没搜到这个号」= 微信里什么都没发生，**不许**报成「申请已发出」。

        2026-08-27 真机实况：`submit` 对失败的任务一个字都不抛，只在 `status` 里写着 failed，
        而这里从前不看 status —— 于是后端把人记成「等对方通过」、AI 端跟着写 `ai_managed`，
        这个人从此卡在那儿等一个永远不会来的通过。
        """
        runtime = FakeRuntime(status="failed", result={"number": "wxid_zhang"})
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "wxid_zhang", "greetings": "你好", "remark": "张三"},
        })
        self.assertIsNone((result.result or {}).get("friend_state"))

    async def test_a_known_session_name_wins_over_the_one_the_ai_made_up(self) -> None:
        """★ 换机后不给同一个人起第二个备注名（api2 §4.2）：后端记着的那个说了算。

        名字一变，按会话名存的关系记忆（078）就断了，而且现场看不出来——
        微信里会多出一个「新人」，聊过的东西全不认。
        """
        self.store.save_contacts(ACCOUNT, [
            {"number": "wxid_zhang", "session_name": "候选人-张建国", "updated_at": "2026-08-18T09:00:00.000Z"},
        ])
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "wxid_zhang", "greetings": "你好", "remark": "张先生"},
        })
        self.assertEqual(result.status, "done", result.reason)
        _, payload, _ = runtime.calls[-1]
        self.assertEqual(payload["remark"], "候选人-张建国")

    async def test_the_ai_remark_stands_when_this_machine_knows_nothing(self) -> None:
        """本机没有这个号的记录 —— 那 AI 端给的备注名就是唯一一份，照用。"""
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "wxid_new", "greetings": "你好", "remark": "候选人-李四"},
        })
        self.assertEqual(result.status, "done", result.reason)
        _, payload, _ = runtime.calls[-1]
        self.assertEqual(payload["remark"], "候选人-李四")

    async def test_a_number_that_was_already_a_friend_is_accepted_not_requested(self) -> None:
        """★ 这个号早就是好友（2026-08-26 用户场景）：好友关系是**既成事实**，不是待通过。

        报成 `requested`，这个人在计划里就永远停在「等对方通过」——而根本没有人要通过。
        """
        runtime = FakeRuntime(result={"already_friend": True, "session_name": "张三·仓库工"})
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "add_friend",
            "args": {"number": "13800138000", "greetings": "你好"},
        })
        self.assertEqual(result.status, "done", result.reason)
        self.assertEqual((result.result or {}).get("friend_state"), "accepted")

    async def test_accepting_an_application_really_is_accepted(self) -> None:
        runtime = FakeRuntime(result={"items": ["张三"], "verify": True})
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "accept_new_friends", "args": {"limit": 1},
        })
        self.assertEqual(result.status, "done", result.reason)
        self.assertEqual((result.result or {}).get("friend_state"), "accepted")

    async def test_accepting_nothing_claims_nothing(self) -> None:
        """一个待处理申请都没有 —— 这一趟什么也没发生，别报 accepted。"""
        runtime = FakeRuntime(result={"items": [], "verify": True})
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "accept_new_friends", "args": {"limit": 1},
        })
        self.assertEqual(result.status, "done", result.reason)
        self.assertNotIn("friend_state", result.result or {})

    async def test_dry_run_claims_nothing(self) -> None:
        """只走流程没真动微信，就别报状态——报了 AI 端就会去写 wechatAdded。"""
        runtime = FakeRuntime()
        result = await self._executor(runtime, allow_real_actions=False).execute({
            "command_id": "c1", "op": "add_friend", "args": {"number": "wxid_zhang"},
        })
        self.assertEqual(result.status, "done", result.reason)
        self.assertNotIn("friend_state", result.result or {})

    async def test_other_ops_get_no_such_field(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "在吗"},
        })
        self.assertNotIn("friend_state", result.result or {})


class IdempotencyTests(CommandTestCase):
    async def test_same_command_id_executes_once(self) -> None:
        """重推（网络重试/网关重连补推）绝不能再发一遍消息。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        frame = {"command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "你好"}}
        first = await executor.execute(frame)
        second = await executor.execute(dict(frame))
        self.assertEqual(first.status, "done")
        self.assertEqual(second.status, "done")
        self.assertEqual(len(runtime.calls), 1, "同一条指令被执行了两次")

    async def test_rejection_is_also_remembered(self) -> None:
        runtime = FakeRuntime()
        executor = self._executor(runtime)
        frame = {"command_id": "c1", "op": "send_message", "args": {"to": "张三"}}
        await executor.execute(frame)
        again = await executor.execute(dict(frame))
        self.assertEqual(again.status, "rejected")
        self.assertEqual(runtime.calls, [])

    async def test_missing_command_id_is_rejected(self) -> None:
        runtime = FakeRuntime()
        result = await self._executor(runtime).execute({"op": "send_message", "args": {}})
        self.assertEqual(result.status, "rejected")
        self.assertIn("command_id", result.reason)


class FailureTests(CommandTestCase):
    async def test_execution_failure_is_reported_not_swallowed(self) -> None:
        runtime = FakeRuntime(boom=True)
        result = await self._executor(runtime).execute({
            "command_id": "c1", "op": "send_message", "args": {"to": "张三", "text": "你好"},
        })
        self.assertEqual(result.status, "failed")
        self.assertIn("微信没登录", result.reason)
        frame = result.to_frame_data()
        self.assertEqual(frame["command_id"], "c1")
        self.assertEqual(frame["status"], "failed")
        self.assertTrue(frame["reason"])


if __name__ == "__main__":
    unittest.main()


def fake_fetcher(root: Path, *, boom: str = "") -> Any:
    """假的取文件通道：记下被要过哪些 file_id，落盘到 ``root``，退出时标记已清理。"""

    class FakeFetcher:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.cleaned = False

        @asynccontextmanager
        async def materialize(self, account_id, refs):
            self.calls.append([r.file_id for r in refs])
            if boom:
                raise DownloadError(boom)
            paths = []
            for ref in refs:
                p = root / f"{ref.file_id}.png"
                p.write_bytes(b"png")
                paths.append(p)
            try:
                yield paths
            finally:
                self.cleaned = True

    return FakeFetcher()


class FileChannelTestCase(CommandTestCase):
    def _fetcher(self, *, boom: str = "") -> Any:
        return fake_fetcher(Path(self._tmp.name), boom=boom)


class SendFilesDownloadTestCase(FileChannelTestCase):
    """067：send_files 收的是**文件描述符**，不是路径。

    路径由网关自己下载后产生——AI 端给不了、也不许给（那是服务端派生的东西）。
    """

    async def test_file_ids_下载后交本机路径给底座(self) -> None:
        runtime, fetcher = FakeRuntime(), self._fetcher()
        ex = self._executor(runtime, fetcher=fetcher)
        result = await ex.execute({
            "command_id": "c1", "op": "send_files",
            "args": {"to": "张三", "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
        })
        self.assertEqual(result.status, "done", result.reason)
        task_type, payload, _conf = runtime.calls[0]
        self.assertEqual(task_type, TaskType.SEND_FILES)
        self.assertEqual(fetcher.calls, [["f1"]])
        self.assertTrue(payload["files"][0].endswith("f1.png"), payload["files"])
        self.assertTrue(fetcher.cleaned, "发完必须清掉临时文件")

    async def test_下载失败回执说人话且不投任务(self) -> None:
        runtime = FakeRuntime()
        ex = self._executor(runtime, fetcher=self._fetcher(boom="这个文件在 AI 服务上找不到了"))
        result = await ex.execute({
            "command_id": "c2", "op": "send_files",
            "args": {"to": "张三", "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
        })
        self.assertEqual(result.status, "failed")
        self.assertIn("找不到", result.reason)
        self.assertEqual(runtime.calls, [], "没取到文件就绝不能去发")

    async def test_没有下载器时明确拒绝而不是把描述符当路径发(self) -> None:
        runtime = FakeRuntime()
        ex = self._executor(runtime)   # 不带 fetcher
        result = await ex.execute({
            "command_id": "c3", "op": "send_files",
            "args": {"to": "张三", "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
        })
        self.assertEqual(result.status, "rejected")
        self.assertEqual(runtime.calls, [])


class MomentsMediaTestCase(FileChannelTestCase):
    """P0-5（2026-08-18 审计）：发朋友圈的配图也只收**文件描述符**，不收本机路径。

    原来 `post_moments` 的 build 是 `{"text": …, "medias": list(a.get("medias") or [])}`——
    AI 端推什么路径就往下传什么，最后 `_normalize_moments_media_paths` 会 expanduser +
    resolve，只要文件存在就发上朋友圈。**朋友圈是公开的**：HR 那台电脑上任何一个可读文件
    都在射程内。这个文件开头就写着「地基不信任上游」，`medias` 是它唯一信了上游的地方。
    """

    async def test_ai端塞本机路径一个字节都进不了payload(self) -> None:
        runtime = FakeRuntime()
        ex = self._executor(runtime, fetcher=self._fetcher())
        result = await ex.execute({
            "command_id": "m1", "op": "post_moments",
            "args": {"text": "招 3 个普工", "medias": ["C:/Users/ZK/Pictures/私人照片.jpg"]},
        })
        self.assertEqual(result.status, "done", result.reason)
        _task_type, payload, _conf = runtime.calls[0]
        self.assertNotIn("私人照片", json.dumps(payload, ensure_ascii=False),
                         "上游给的路径不许出现在 payload 的任何位置")
        self.assertFalse(payload.get("medias"), "没有描述符就没有图，别把路径当图发")

    async def test_描述符落盘后当配图交给底座(self) -> None:
        runtime, fetcher = FakeRuntime(), self._fetcher()
        ex = self._executor(runtime, fetcher=fetcher)
        result = await ex.execute({
            "command_id": "m2", "op": "post_moments",
            "args": {"text": "招 3 个普工",
                     "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
        })
        self.assertEqual(result.status, "done", result.reason)
        _task_type, payload, _conf = runtime.calls[0]
        self.assertEqual(fetcher.calls, [["f1"]])
        self.assertTrue(payload["medias"][0].endswith("f1.png"), payload["medias"])
        self.assertTrue(fetcher.cleaned, "发完必须清掉临时文件")

    async def test_纯文字朋友圈照发(self) -> None:
        runtime = FakeRuntime()
        ex = self._executor(runtime, fetcher=self._fetcher())
        result = await ex.execute({
            "command_id": "m3", "op": "post_moments", "args": {"text": "招 3 个普工"},
        })
        self.assertEqual(result.status, "done", result.reason)
        _task_type, payload, _conf = runtime.calls[0]
        self.assertFalse(payload.get("medias"))

    async def test_取图失败要把朋友圈那档额度还回去(self) -> None:
        """★ 忘了 release，配额被白吃掉——而配额是防风控红线的执行体。"""
        runtime = FakeRuntime()
        ex = self._executor(runtime, fetcher=self._fetcher(boom="这个文件在 AI 服务上找不到了"))
        before = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action=ACTION_MOMENTS)
        result = await ex.execute({
            "command_id": "m4", "op": "post_moments",
            "args": {"text": "招 3 个普工",
                     "files": [{"file_id": "f1", "file_name": "海报.jpg", "content_b64": "YQ=="}]},
        })
        self.assertEqual(result.status, "failed")
        self.assertEqual(runtime.calls, [], "没取到图就绝不能去发")
        after = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action=ACTION_MOMENTS)
        self.assertEqual(after, before, "一件没做成，额度要原样还回去")


class InlineFileTestCase(unittest.IsolatedAsyncioTestCase):
    """字节随指令下发：落盘 → 交本机路径 → 用完删。"""

    def _ref(self, name: str, data: bytes) -> FileRef:
        return FileRef.parse({"file_id": "f1", "file_name": name, "content_b64": base64.b64encode(data).decode()})

    async def test_落盘后交出本机路径且内容一致(self) -> None:
        ref = self._ref("招聘海报-1.jpg", b"\xff\xd8\xff\xe0")
        async with FileFetcher().materialize("acc", [ref]) as paths:
            self.assertEqual(paths[0].name, "招聘海报-1.jpg", "原名要带到微信上，不能退化成 .bin")
            self.assertEqual(paths[0].read_bytes(), b"\xff\xd8\xff\xe0")
            tmp_root = paths[0].parent.parent
        self.assertFalse(tmp_root.exists(), "发完必须连临时目录一起删")

    async def test_同名文件不互相覆盖(self) -> None:
        refs = [self._ref("海报.jpg", b"a"), self._ref("海报.jpg", b"bb")]
        async with FileFetcher().materialize("acc", refs) as paths:
            self.assertEqual([p.read_bytes() for p in paths], [b"a", b"bb"])

    async def test_超总量上限就拒绝而不是硬写盘(self) -> None:
        refs = [self._ref("大.bin", b"x" * 1024)]
        with self.assertRaises(DownloadError):
            async with FileFetcher(max_total_bytes=512).materialize("acc", refs):
                pass

    async def test_空文件与坏_base64_明确报错(self) -> None:
        # 空内容在 parse 阶段就不算文件；坏 base64 要在落盘前拦下。
        self.assertIsNone(FileRef.parse({"file_id": "f1", "file_name": "空.jpg", "content_b64": ""}))
        for ref in (FileRef("f1", "空.jpg", "  "), FileRef("f1", "坏.jpg", "!!!not-base64!!!")):
            with self.assertRaises(DownloadError):
                async with FileFetcher().materialize("acc", [ref]):
                    pass

    def test_文件名不许带路径(self) -> None:
        ref = FileRef.parse({"file_id": "f1", "file_name": "../../evil.png", "content_b64": "YQ=="})
        self.assertNotIn("/", ref.file_name)
        self.assertNotIn("..", ref.file_name.strip("._"))

    def test_没有字节的描述符不算文件(self) -> None:
        self.assertIsNone(FileRef.parse({"file_id": "f1", "url": "/v1/sessions/s/files/f1"}))


# ────────────────────────── 定时指令（077）──────────────────────────


class ScheduleParsingTests(unittest.TestCase):
    def test_parses_iso_with_zone(self) -> None:
        due = parse_schedule_at("2026-08-15T09:00:00+08:00")
        self.assertIsNotNone(due)
        self.assertEqual(due.utcoffset(), timedelta(hours=8))

    def test_naive_time_is_read_as_local_not_utc(self) -> None:
        """★ 用户说「明天上午九点」指的是他那台电脑上的九点。

        当成 UTC 的话在东八区差八小时——`within_active_hours` 踩过一模一样的坑。
        """
        due = parse_schedule_at("2026-08-15T09:00:00")
        self.assertIsNotNone(due)
        self.assertIsNotNone(due.tzinfo)

    def test_garbage_is_not_a_schedule(self) -> None:
        for raw in ("", "   ", "明天九点", None, 123, "2026-13-45"):
            self.assertIsNone(parse_schedule_at(raw))


class ScheduleLoopTests(unittest.IsolatedAsyncioTestCase):
    """★ 077：`schedule_at` 原来只是透传到网关就没人管了，「明天上午九点提醒他」
    会被**当场**发出去——比不做还糟。需求文档 §6.19 的定时触发就卡在这里。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.now = WORKDAY
        self.fired: list[dict] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _loop(self, *, active: bool = True) -> ScheduleLoop:
        async def run_command(_account: str, payload: dict) -> None:
            self.fired.append(payload)

        return ScheduleLoop(
            accounts=[ACCOUNT],
            store=self.store,
            run_command=run_command,
            is_active=lambda _now: active,
            clock=lambda: self.now,
        )

    def _schedule(self, due: datetime, command_id: str = "cmd-1") -> None:
        self.store.put_scheduled_command(
            ACCOUNT, command_id,
            {"command_id": command_id, "op": "send_message",
             "args": {"to": "王建国", "text": "提醒您明天面试"}},
            due, at=self.now,
        )

    async def test_not_due_yet_stays_put(self) -> None:
        self._schedule(WORKDAY + timedelta(hours=2))
        self.assertEqual(await self._loop().tick(), 0)
        self.assertEqual(self.fired, [])
        self.assertEqual(self.store.count_scheduled_commands(), 1)

    async def test_due_fires_once_and_is_cleared(self) -> None:
        self._schedule(WORKDAY - timedelta(minutes=5))
        self.assertEqual(await self._loop().tick(), 1)
        self.assertEqual(len(self.fired), 1)
        self.assertEqual(self.fired[0]["args"]["to"], "王建国")
        # 销账了：下一趟不会再放一遍。
        self.assertEqual(self.store.count_scheduled_commands(), 0)
        self.assertEqual(await self._loop().tick(), 0)

    async def test_nothing_fires_outside_working_hours(self) -> None:
        """★ 半夜三点准时发一条「提醒您明天面试」正是风控要找的那种账号。

        不是丢掉——在表里等着，第二天进时间窗第一趟放出去。
        """
        self._schedule(WORKDAY - timedelta(minutes=5))
        self.assertEqual(await self._loop(active=False).tick(), 0)
        self.assertEqual(self.fired, [])
        self.assertEqual(self.store.count_scheduled_commands(), 1)

        self.assertEqual(await self._loop(active=True).tick(), 1)
        self.assertEqual(len(self.fired), 1)

    async def test_way_too_late_is_dropped_with_a_trace(self) -> None:
        """机器关了一夜再开机，昨天的排期不该突然全放出来——面试都结束了。"""
        self._schedule(WORKDAY - timedelta(hours=MAX_LATE_HOURS + 1))
        with capture_logs() as logs:
            self.assertEqual(await self._loop().tick(), 0)
        self.assertEqual(self.fired, [])
        self.assertEqual(self.store.count_scheduled_commands(), 0)
        self.assertTrue(
            any(e["log_level"] == "warning" and "迟到" in e["event"] for e in logs), logs
        )



# ────────────────────────── 逐能力探测（074）──────────────────────────


class CapabilityProbeTests(unittest.TestCase):
    """★ 074：原来只有一个总开关「UIA 可用/不可用」，所有「做不了」长一个样，
    而且要**执行到一半**才发现（需求文档 §12.2 要逐能力上报 + 执行前校验）。
    """

    LIVE: ClassVar[dict[str, bool]] = {"logged_in": True, "uia_exposed": True}

    def _caps(self, **kw):
        base = {"uia": self.LIVE, "allow_real_actions": True, "has_file_channel": True}
        base.update(kw)
        return probe_capabilities(**base)

    def test_everything_works_when_wechat_is_live(self) -> None:
        caps = self._caps()
        self.assertTrue(all(c.available for c in caps.values()), caps)
        self.assertIn(CAP_DELIVERY_RECEIPT, caps)   # AI 端投面试天气提醒前看这一项

    def test_dry_run_cannot_promise_a_delivery_receipt(self) -> None:
        caps = self._caps(allow_real_actions=False)
        self.assertFalse(caps[CAP_DELIVERY_RECEIPT].available)

    def test_holiday_greeting_key_only_when_really_sending(self) -> None:
        """AI 端 123：见到这一项才向业务端占位、拼节前祝福；只走流程不真发时指令号永远对不上账。"""
        self.assertTrue(self._caps()[CAP_HOLIDAY_GREETING].available)
        self.assertFalse(self._caps(allow_real_actions=False)[CAP_HOLIDAY_GREETING].available)

    def test_login_screen_blocks_everything_with_a_human_reason(self) -> None:
        caps = self._caps(uia={"logged_in": False, "login_window": True})
        self.assertFalse(caps[CAP_PRIVATE_MESSAGE].available)
        self.assertIn("扫码登录", caps[CAP_PRIVATE_MESSAGE].reason)

    def test_no_probe_result_is_reported_as_unavailable_not_available(self) -> None:
        """★ 探不出来 = 不知道 = 按做不了报。猜出来的能力表比没有更糟——
        它会让 AI 端理直气壮地承诺做不到的事。"""
        caps = self._caps(uia=None)
        self.assertFalse(caps[CAP_PRIVATE_MESSAGE].available)

    def test_dry_run_mode_says_writes_do_not_work(self) -> None:
        """★ 只走流程时每条写指令都回 done，AI 端会照着告诉用户「已经发过去了」。"""
        caps = self._caps(allow_real_actions=False)
        self.assertFalse(caps[CAP_PRIVATE_MESSAGE].available)
        self.assertFalse(caps[CAP_MOMENTS].available)
        self.assertTrue(caps[CAP_READ].available, "读类不受影响")

    def test_missing_file_channel_only_blocks_files(self) -> None:
        caps = self._caps(has_file_channel=False)
        self.assertFalse(caps[CAP_SEND_FILES].available)
        self.assertIn("文件", caps[CAP_SEND_FILES].reason)
        self.assertTrue(caps[CAP_PRIVATE_MESSAGE].available)

    def test_unsupported_op_is_reported_as_such(self) -> None:
        caps = self._caps(supported_ops={"send_message"})
        self.assertTrue(caps[CAP_PRIVATE_MESSAGE].available)
        self.assertFalse(caps[CAP_MOMENTS].available)

    def test_capability_status_never_talks_up_a_dry_run_machine(self) -> None:
        """契约 §8：只有 ``passed`` 会让企业端显示「运行中」。

        ★ 只走流程不真发的机器必须报 ``limited``——报 passed 的话，企业端说运行中，
        而这台电脑一条消息都不会真的发出去。
        """
        self.assertEqual(capability_status(self._caps()), "passed")
        self.assertEqual(capability_status(self._caps(allow_real_actions=False)), "limited")
        self.assertEqual(capability_status(self._caps(has_file_channel=False)), "limited")
        self.assertEqual(capability_status(self._caps(uia=None)), "failed")

    def test_group_message_is_its_own_capability(self) -> None:
        self.assertEqual(capability_of("send_message"), CAP_PRIVATE_MESSAGE)
        self.assertEqual(capability_of("send_message", to_group=True), CAP_GROUP_MESSAGE)


class CapabilityGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _executor(self, caps, runtime) -> CommandExecutor:
        return CommandExecutor(
            runtime=runtime,
            store=self.store,
            guard=OutboundGuard(self.store, active_hours="00:00-23:59"),
            account_id=ACCOUNT,
            allow_real_actions=True,
            confirmation="确认",
            clock=lambda: WORKDAY,
            capabilities=lambda: caps,
        )

    async def test_unavailable_capability_is_refused_before_touching_wechat(self) -> None:
        """★ 做不了就当场说清楚，别一路走到取字节那步才拒。"""
        runtime = FakeRuntime()
        caps = probe_capabilities(
            uia={"logged_in": True, "uia_exposed": True},
            allow_real_actions=True,
            has_file_channel=False,          # 没配取文件通道
        )
        result = await self._executor(caps, runtime).execute({
            "command_id": "c1", "op": "send_files",
            "args": {"to": "王建国", "files": [{"file_id": "f1", "content_b64": "eA=="}]},
        })
        self.assertEqual(result.status, "rejected")
        self.assertIn("文件", result.reason)
        self.assertEqual(runtime.calls, [], "一条微信动作都不该发生")

    async def test_available_capability_still_goes_through(self) -> None:
        runtime = FakeRuntime()
        caps = probe_capabilities(
            uia={"logged_in": True, "uia_exposed": True},
            allow_real_actions=True,
            has_file_channel=True,
        )
        result = await self._executor(caps, runtime).execute({
            "command_id": "c2", "op": "send_message",
            "args": {"to": "王建国", "text": "您好"},
        })
        self.assertEqual(result.status, "done")
        self.assertEqual(len(runtime.calls), 1)


class AddFriendPacingTests(CommandTestCase):
    """加好友的**节奏**（2026-08-27 真机弹了安全验证）。

    那天整个日志里加好友只有两次——不是量大，是这两次的性质和间距：
    15:22:36 和 15:23:57，隔 81 秒，而且**两次都是「没搜到这个号」**。
    连续搜不到的陌生手机号是枚举的形状，比成功加一个好友敏感得多。

    81 秒能过闸，是因为写侧只有「5 次/小时」的配额、**没有最小间隔**——
    `pacing.py` 上写着「加好友相关的动作节拍必须慢得多」，可那道 300 秒的闸
    只挂在读侧（查好友申请），真正去搜陌生号的 `add_friend` 一直在闸外面。
    """

    def _add(self, runtime: FakeRuntime, cid: str = "c1", number: str = "13800000001"):
        return self._executor(runtime).execute({
            "command_id": cid, "op": "add_friend",
            "args": {"number": number, "greetings": "你好", "remark": "张三"},
        })

    async def test_two_add_friends_in_a_row_are_paced_not_just_quota_checked(self) -> None:
        """★ 第二次紧接着来 → 被节拍闸拦下（配额还剩 4 次，闸管的是间隔不是总量）。"""
        runtime = FakeRuntime()
        self.assertEqual((await self._add(runtime, "c1")).status, "done")
        self.now += timedelta(seconds=81)          # 真机上就是这个间距
        second = await self._add(runtime, "c2", "13800000002")
        self.assertEqual(second.status, "rejected")
        self.assertIn("300", second.reason)
        self.assertEqual(len(runtime.calls), 1, "被拦下的那次一下微信都不该动")

    async def test_after_the_floor_has_passed_it_goes_through(self) -> None:
        runtime = FakeRuntime()
        await self._add(runtime, "c1")
        self.now += timedelta(seconds=301)
        self.assertEqual((await self._add(runtime, "c2", "13800000002")).status, "done")
        self.assertEqual(len(runtime.calls), 2)

    async def test_accepting_requests_is_not_slowed_down_by_this(self) -> None:
        """★ 闸只管「主动去搜一个陌生号」，不管「通过别人发来的申请」。

        两者共用 `ACTION_FRIEND` 那本**配额**，但通过申请不用搜号——给它套 300 秒，
        攒着的申请就永远通不完。所以 `WRITE_PACING` 按**指令名**登记，不按 action。
        """
        runtime = FakeRuntime(result={"items": ["甲"]})
        await self._add(FakeRuntime(), "c1")
        self.now += timedelta(seconds=10)
        accepted = await self._executor(runtime).execute({
            "command_id": "c2", "op": "accept_new_friends", "args": {"limit": 1},
        })
        self.assertEqual(accepted.status, "done", accepted.reason)

    async def test_checking_does_not_block_adding(self) -> None:
        """★ 加好友**不与查好友合账**——合过一次，是错的。

        复查循环（`friendcheck.py`）只要还有人待复查就每 300 秒摸一次 `read_friends`，
        合账等于 AI 端下发的加好友永远排在它后面被拒；而被拒的指令是直接丢掉、不排队的。
        让路的必须是能重来的那一方，所以是复查躲加好友（见 friendcheck 的对应测试）。
        """
        runtime = FakeRuntime()
        checked = await self._executor(runtime).execute({
            "command_id": "c1", "op": "check_new_friends", "args": {},
        })
        self.assertEqual(checked.status, "done", checked.reason)
        self.now += timedelta(seconds=10)
        added = await self._add(runtime, "c2")
        self.assertEqual(added.status, "done", added.reason)

    async def test_a_rejected_attempt_does_not_burn_quota(self) -> None:
        """被节拍挡下的那一趟微信一下都没动，不该白占一次「5 次/小时」。"""
        runtime = FakeRuntime()
        await self._add(runtime, "c1")
        self.now += timedelta(seconds=10)
        await self._add(runtime, "c2", "13800000002")
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=1), action="friend")
        self.assertEqual(used, 1, "只有真做成的那次该记账")

    async def test_the_interval_is_configurable(self) -> None:
        """★ 间隔是**一个可配的数**，不是写死的 300。

        曾经有过一档「连续失败翻倍」（300→600→1200→2400），2026-08-28 按用户要求删了：
        等太久。想更慢就把这个数调大——一个看得见、改得动的数，好过一个自己会长的数。
        """
        runtime = FakeRuntime()
        executor = self._executor(runtime, write_intervals={"friend_write": 60.0})
        self.assertEqual((await executor.execute({
            "command_id": "c1", "op": "add_friend", "args": {"number": "13800000001"},
        })).status, "done")
        self.now += timedelta(seconds=61)
        second = await executor.execute({
            "command_id": "c2", "op": "add_friend", "args": {"number": "13800000002"},
        })
        self.assertEqual(second.status, "done", second.reason)

    async def test_failures_do_not_stretch_the_interval(self) -> None:
        """★ 连着加不上也**不**放慢。这条是上面那条的另一面，专门守「别把退避加回来」。"""
        failed = await self._add(FakeRuntime(status="failed", result={}), "c1")
        self.assertEqual(failed.status, "failed", "前提：这一次确实没加上")

        self.now += timedelta(seconds=301)
        third = await self._add(FakeRuntime(), "c2", "13800000002")
        self.assertEqual(third.status, "done", third.reason)

    async def test_zero_turns_the_gate_off(self) -> None:
        """`0` = 这道闸关掉，只剩配额。配置得能真的关掉，否则就是假配置。"""
        runtime = FakeRuntime()
        executor = self._executor(runtime, write_intervals={"friend_write": 0.0})
        for i in range(3):
            r = await executor.execute({
                "command_id": f"c{i}", "op": "add_friend", "args": {"number": f"1380000000{i}"},
            })
            self.assertEqual(r.status, "done", r.reason)

    async def test_sending_a_message_is_not_slowed_down_by_this(self) -> None:
        """闸只挂在登记过的写动作上。发消息有它自己的节奏——对方在等回话。"""
        runtime = FakeRuntime()
        for i in range(3):
            r = await self._executor(runtime).execute({
                "command_id": f"m{i}", "op": "send_message",
                "args": {"to": "张三", "text": "在吗"},
            })
            self.assertEqual(r.status, "done", r.reason)


class LikeMomentsTests(CommandTestCase):
    """朋友圈点赞（用户 2026-09-07：时间线形态，一天上限 20）。

    形态是**刷自己的时间线**，不逐个好友翻——逐个翻是搜人 → 开资料 → 进相册那条路，
    和加好友前搜号同形，正是风控最盯的动作。这里守三件事：一条指令按 limit 预留、
    按真点了几个结算；一天的配额是按**天**算的 20；两趟之间有最小间隔。
    """

    def _like(self, runtime: FakeRuntime, cid: str = "c1", **kwargs: Any):
        return self._executor(runtime, **kwargs).execute({
            "command_id": cid, "op": "like_moments", "args": {},
        })

    async def test_默认一趟最多点_20_个_且只算真点了的(self) -> None:
        runtime = FakeRuntime(result={"liked": 4, "scanned": 6, "already_liked": 2, "stopped": "older"})
        result = await self._like(runtime)
        self.assertEqual(result.status, "done", result.reason)
        task_type, payload, _conf = runtime.calls[0]
        self.assertEqual(task_type, TaskType.LIKE_MOMENTS)
        self.assertEqual(payload["limit"], 20)
        self.assertFalse(payload["dry_run"], "写类指令，真动微信")
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action="moments_like")
        self.assertEqual(used, 4, "预留 20、真点 4 → 只记 4，剩下的额度还回去")
        self.assertEqual(result.result["liked"], 4)

    async def test_一天的额度用完就减量_减到零就拒(self) -> None:
        """额度不够时**减量放行**（点 5 个总比一个不点强），一个都不剩才拒。"""
        first = await self._like(FakeRuntime(result={"liked": 15}), "c1", write_intervals={"moments_like_write": 0.0})
        self.assertEqual(first.status, "done", first.reason)

        runtime = FakeRuntime(result={"liked": 5})
        second = await self._like(runtime, "c2", write_intervals={"moments_like_write": 0.0})
        self.assertEqual(second.status, "done", second.reason)
        self.assertEqual(runtime.calls[0][1]["limit"], 5, "只剩 5 个名额，就让微信最多点 5 个")

        runtime = FakeRuntime(result={"liked": 1})
        third = await self._like(runtime, "c3", write_intervals={"moments_like_write": 0.0})
        self.assertEqual(third.status, "rejected")
        self.assertIn("朋友圈点赞", third.reason)
        self.assertIn("20", third.reason)
        self.assertEqual(runtime.calls, [], "额度为零时一下微信都不该动")

    async def test_额度按天算_过了一天恢复(self) -> None:
        await self._like(FakeRuntime(result={"liked": 20}), "c1")
        self.now += timedelta(hours=5)
        self.assertEqual((await self._like(FakeRuntime(result={"liked": 1}), "c2")).status, "rejected",
                         "5 小时后还在同一天的窗口里")
        self.now += timedelta(hours=20)
        ok = await self._like(FakeRuntime(result={"liked": 1}), "c3")
        self.assertEqual(ok.status, "done", ok.reason)

    async def test_两趟之间要隔够时间(self) -> None:
        """连着开关朋友圈窗口不像人。默认 600 秒，可配。"""
        runtime = FakeRuntime(result={"liked": 3})
        self.assertEqual((await self._like(runtime, "c1")).status, "done")
        self.now += timedelta(seconds=120)
        second = await self._like(runtime, "c2")
        self.assertEqual(second.status, "rejected")
        self.assertIn("600", second.reason)
        self.assertEqual(len(runtime.calls), 1)
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action="moments_like")
        self.assertEqual(used, 3, "被节拍挡下的那趟不占配额")

        self.now += timedelta(seconds=500)
        self.assertEqual((await self._like(runtime, "c3")).status, "done")

    async def test_没做成就把额度全还回去(self) -> None:
        failed = await self._like(FakeRuntime(status="failed", result={}), "c1")
        self.assertEqual(failed.status, "failed")
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action="moments_like")
        self.assertEqual(used, 0)

    async def test_不在时间窗内一律不点(self) -> None:
        self.now = MIDNIGHT
        runtime = FakeRuntime(result={"liked": 1})
        result = await self._like(runtime)
        self.assertEqual(result.status, "rejected")
        self.assertIn("时间窗", result.reason)
        self.assertEqual(runtime.calls, [])

    async def test_点赞不算联系了某个人_不上报消息事件(self) -> None:
        """朋友圈点赞没有收件人：不进 MESSAGE_EVENT_OPS，不该被当成给谁发了条微信。"""
        from wxclient.gateway.commands import MESSAGE_EVENT_OPS
        self.assertNotIn("like_moments", MESSAGE_EVENT_OPS)

    def test_只走流程的机器如实报做不了(self) -> None:
        from wxclient.gateway.capabilities import CAP_MOMENTS_LIKE
        caps = probe_capabilities(
            uia={"logged_in": True, "uia_exposed": True},
            allow_real_actions=False, has_file_channel=True,
        )
        self.assertFalse(caps[CAP_MOMENTS_LIKE].available)
        self.assertEqual(capability_of("like_moments"), CAP_MOMENTS_LIKE)
        real = probe_capabilities(
            uia={"logged_in": True, "uia_exposed": True},
            allow_real_actions=True, has_file_channel=True,
        )
        self.assertTrue(real[CAP_MOMENTS_LIKE].available)


class CommentMomentsTests(CommandTestCase):
    """朋友圈评论（用户 2026-09-07：业务端计划触发、AI 端写字、一天几条可配）。

    本机只管三件事：按正文认动态（在 uia 层）、按量（这里：按要评几条预留、按真评了几条
    结算、一天的配额按**天**、两趟隔够时间）、**同一条动态只评一次**（本机台账）。
    """

    POSTS = (
        {"content": "新到一批岗位，包吃住", "text": "包吃住太香了，求详情"},
        {"content": "周末去爬山", "text": "好地方，注意安全"},
    )

    def _comment(self, runtime: FakeRuntime, cid: str = "c1", comments=None, **kwargs: Any):
        return self._executor(runtime, **kwargs).execute({
            "command_id": cid, "op": "comment_moments",
            "args": {"comments": list(self.POSTS) if comments is None else comments},
        })

    async def test_按要评几条预留_按真评了几条结算(self) -> None:
        runtime = FakeRuntime(result={
            "commented": 1, "scanned": 4, "matched": 2,
            "commented_contents": ["新到一批岗位，包吃住"], "missed": ["周末去爬山"], "stopped": "all",
        })
        result = await self._comment(runtime)
        self.assertEqual(result.status, "done", result.reason)
        task_type, payload, _conf = runtime.calls[0]
        self.assertEqual(task_type, TaskType.COMMENT_MOMENTS)
        self.assertEqual([c["content"] for c in payload["comments"]], [p["content"] for p in self.POSTS])
        self.assertFalse(payload["dry_run"], "写类指令，真动微信")
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action="moments_comment")
        self.assertEqual(used, 1, "预留 2、真评 1 → 只记 1")
        self.assertEqual(result.result["commented"], 1)

    async def test_形状不对当场拒_不动微信(self) -> None:
        runtime = FakeRuntime()
        for bad in (
            {},
            {"comments": []},
            {"comments": "字符串"},
            {"comments": [{"content": "一", "text": "正文太短"}]},
            {"comments": [{"content": "好岗位", "text": ""}]},
            {"comments": [{"content": "好岗位", "text": "x" * 201}]},
            {"comments": [{"content": f"第{i}条", "text": "x"} for i in range(6)]},
        ):
            result = await self._executor(runtime).execute({"command_id": "c", "op": "comment_moments", "args": bad})
            self.assertEqual(result.status, "rejected", bad)
        self.assertEqual(runtime.calls, [])

    async def test_同一条动态只评一次_第二次直接算做完(self) -> None:
        """★ 计划重投 / 读回同一批动态再写一遍，微信上分不出评过没有，靠本机台账挡。"""
        first = await self._comment(FakeRuntime(result={
            "commented": 2, "commented_contents": [p["content"] for p in self.POSTS], "missed": [],
        }), "c1", write_intervals={"moments_comment_write": 0.0})
        self.assertEqual(first.status, "done", first.reason)

        runtime = FakeRuntime(result={"commented": 2})
        second = await self._comment(runtime, "c2", write_intervals={"moments_comment_write": 0.0})
        self.assertEqual(second.status, "done", "全评过了 = 做完了，不是拒")
        self.assertEqual(runtime.calls, [], "一下微信都不动")
        self.assertEqual(second.result["commented"], 0)
        self.assertEqual(second.result["already_commented"], 2)

        # 混着一条新的：只评新的那条，评过的那条不再发。
        runtime = FakeRuntime(result={"commented": 1, "commented_contents": ["新的一条"]})
        third = await self._comment(runtime, "c3", comments=[
            self.POSTS[0], {"content": "新的一条", "text": "评"},
        ], write_intervals={"moments_comment_write": 0.0})
        self.assertEqual(third.status, "done", third.reason)
        self.assertEqual([c["content"] for c in runtime.calls[0][1]["comments"]], ["新的一条"])
        self.assertEqual(third.result["already_commented"], 1)

    async def test_没评上的不进台账_下次还能评(self) -> None:
        await self._comment(FakeRuntime(result={
            "commented": 1, "commented_contents": [self.POSTS[0]["content"]], "missed": ["周末去爬山"],
        }), "c1", write_intervals={"moments_comment_write": 0.0})
        runtime = FakeRuntime(result={"commented": 1, "commented_contents": ["周末去爬山"]})
        again = await self._comment(runtime, "c2", write_intervals={"moments_comment_write": 0.0})
        self.assertEqual(again.status, "done", again.reason)
        self.assertEqual([c["content"] for c in runtime.calls[0][1]["comments"]], ["周末去爬山"])

    async def test_一天的额度用完就减量_减到零就拒(self) -> None:
        many = [{"content": f"第{i}条动态", "text": "评"} for i in range(5)]
        first = await self._comment(FakeRuntime(result={
            "commented": 4, "commented_contents": [f"第{i}条动态" for i in range(4)],
        }), "c1", comments=many, write_intervals={"moments_comment_write": 0.0})
        self.assertEqual(first.status, "done", first.reason)

        runtime = FakeRuntime(result={"commented": 1, "commented_contents": ["A条"]})
        second = await self._comment(runtime, "c2", comments=[
            {"content": "A条", "text": "a"}, {"content": "B条", "text": "b"},
        ], write_intervals={"moments_comment_write": 0.0})
        self.assertEqual(second.status, "done", second.reason)
        self.assertEqual([c["content"] for c in runtime.calls[0][1]["comments"]], ["A条"],
                         "只剩 1 个名额，清单砍到 1 条")

        runtime = FakeRuntime(result={"commented": 1})
        third = await self._comment(runtime, "c3", comments=[{"content": "C条", "text": "c"}],
                                    write_intervals={"moments_comment_write": 0.0})
        self.assertEqual(third.status, "rejected")
        self.assertIn("朋友圈评论", third.reason)
        self.assertEqual(runtime.calls, [])

    async def test_两趟之间要隔够时间_且被挡的不占配额(self) -> None:
        runtime = FakeRuntime(result={"commented": 1, "commented_contents": ["新到一批岗位，包吃住"]})
        self.assertEqual((await self._comment(runtime, "c1")).status, "done")
        self.now += timedelta(seconds=120)
        second = await self._comment(runtime, "c2", comments=[{"content": "另一条", "text": "x"}])
        self.assertEqual(second.status, "rejected")
        self.assertIn("600", second.reason)
        self.assertEqual(len(runtime.calls), 1)
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action="moments_comment")
        self.assertEqual(used, 1)

    async def test_没做成就把额度全还回去(self) -> None:
        failed = await self._comment(FakeRuntime(status="failed", result={}), "c1")
        self.assertEqual(failed.status, "failed")
        used = self.store.sends_since(ACCOUNT, self.now - timedelta(hours=24), action="moments_comment")
        self.assertEqual(used, 0)

    async def test_不在时间窗内一律不评(self) -> None:
        self.now = MIDNIGHT
        runtime = FakeRuntime(result={"commented": 1})
        result = await self._comment(runtime)
        self.assertEqual(result.status, "rejected")
        self.assertIn("时间窗", result.reason)
        self.assertEqual(runtime.calls, [])
