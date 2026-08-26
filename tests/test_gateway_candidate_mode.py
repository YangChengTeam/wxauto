"""候选人级接管/托管同步（091 · 业务端 v1.5 §4；`docs/19` v2 §1）。

★ 全组盯同一条：**先落本机、同步失败不回滚**，唯独 409 例外——那是业务端说「你手上那份
过期了」，本机把这一行记成欠账等同步名单带真值回来，而不是自己猜一个档位写上去。

★ 第二条：`request` 与 `report` 是**两段**。少了第二段，业务端那份 `syncStatus` 永远停在
`pending`，自动动作也不会被释放（v1.5 §7 末两条）。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from wxclient.gateway import GatewayStore
from wxclient.gateway.candidate_mode import (
    AI_MANAGED,
    HUMAN_TAKEOVER,
    CandidateModeClient,
    ModeOutcome,
)
from wxclient.gateway.guard import OutboundGuard

T0 = datetime(2026, 8, 26, 10, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"
CANDIDATE = "8bf92e36-2222-4f61-bbbb-123456789abc"


def _ok(**over: Any) -> dict:
    body = {
        "accepted": True,
        "candidateId": CANDIDATE,
        "modeVersion": 1,
        "triggerId": "client-manual:p1:c1:1:abc",
        "aiRecruitment": {"status": HUMAN_TAKEOVER, "version": 4, "modeVersion": 1},
    }
    body.update(over)
    return body


class _Backend:
    """假业务端。记下每一次 PUT，按 `replies` 依次回。"""

    def __init__(self, *replies: httpx.Response) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._replies = list(replies)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, json.loads(request.content.decode())))
        return self._replies.pop(0) if self._replies else httpx.Response(200, json=_ok())

    def client(self) -> CandidateModeClient:
        transport = httpx.MockTransport(self.handler)
        return CandidateModeClient(
            "https://biz.example.com",
            lambda: "client-token",
            client=httpx.AsyncClient(transport=transport),
            clock=lambda: T0,
        )


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_takeover_goes_to_the_candidate_url(self) -> None:
        backend = _Backend()
        outcome = await backend.client().request(
            CANDIDATE, HUMAN_TAKEOVER, reason="候选人要求真人处理", expected_version=3,
        )
        self.assertTrue(outcome.accepted)
        path, body = backend.calls[0]
        # ★ URL 里只有 candidateId：客户端**不传 planId**，归属由服务端自己解析（§4）。
        self.assertEqual(path, f"/recruitment-agent/client/candidates/{CANDIDATE}/conversation-mode")
        self.assertNotIn("planId", body)
        self.assertNotIn("tenantId", body)
        self.assertEqual(body["action"], "request")
        self.assertEqual(body["mode"], HUMAN_TAKEOVER)
        self.assertEqual(body["reason"], "候选人要求真人处理")
        self.assertEqual(body["expectedVersion"], 3)
        self.assertTrue(body["eventId"])
        self.assertEqual(body["occurredAt"], T0.isoformat())
        # 服务端给的这三样要带回来：下一段 report 原样回传，下一次 request 当 expectedVersion。
        self.assertEqual((outcome.version, outcome.mode_version), (4, 1))
        self.assertEqual(outcome.trigger_id, "client-manual:p1:c1:1:abc")

    async def test_takeover_without_a_reason_never_leaves_the_machine(self) -> None:
        """★ §4.1：接管的 `reason` 必填。本机先说清楚，别拿一个必然 400 的请求去撞。"""
        backend = _Backend()
        outcome = await backend.client().request(CANDIDATE, HUMAN_TAKEOVER)
        self.assertFalse(outcome.accepted)
        self.assertFalse(outcome.should_retry, "写错了的请求不该占欠账")
        self.assertEqual(backend.calls, [])

    async def test_handing_back_needs_no_reason(self) -> None:
        backend = _Backend(httpx.Response(200, json=_ok(
            aiRecruitment={"status": AI_MANAGED, "version": 5})))
        outcome = await backend.client().request(CANDIDATE, AI_MANAGED)
        self.assertTrue(outcome.accepted)
        self.assertEqual(outcome.mode, AI_MANAGED)
        self.assertNotIn("reason", backend.calls[0][1])

    async def test_a_session_with_no_candidate_id_is_not_an_owed_report(self) -> None:
        """同步名单里还没有他（陌生人、刚加上还没归位）。本机闸照样成立，这条同步无从谈起。"""
        backend = _Backend()
        outcome = await backend.client().request("", HUMAN_TAKEOVER, reason="我来跟")
        self.assertFalse(outcome.accepted)
        self.assertFalse(outcome.should_retry)
        self.assertEqual(backend.calls, [])

    async def test_the_same_retry_reuses_its_event_id(self) -> None:
        """§2.3 / §8：同一次请求重试必须原样复用 `eventId`，否则会撞 `EVENT_ID_CONFLICT`。"""
        backend = _Backend(httpx.Response(500), httpx.Response(200, json=_ok()))
        client = backend.client()
        first = await client.request(CANDIDATE, HUMAN_TAKEOVER, reason="我来跟", event_id="ev-1")
        self.assertTrue(first.should_retry, "5xx 是「还没送到」，欠着")
        await client.request(CANDIDATE, HUMAN_TAKEOVER, reason="我来跟", event_id="ev-1")
        self.assertEqual([b["eventId"] for _p, b in backend.calls], ["ev-1", "ev-1"])


class NeverRollBackTests(unittest.IsolatedAsyncioTestCase):
    """★★ 本模块最容易做错的一条：哪一种失败才允许本机让步。"""

    async def test_network_failures_are_owed_not_rolled_back(self) -> None:
        for reply in (httpx.Response(500), httpx.Response(429), httpx.Response(401)):
            with self.subTest(status=reply.status_code):
                outcome = await _Backend(reply).client().request(
                    CANDIDATE, HUMAN_TAKEOVER, reason="我来跟",
                )
                self.assertFalse(outcome.accepted)
                self.assertEqual(outcome.conflict_version, 0, "不是版本冲突，本机不让步")
                self.assertTrue(outcome.should_retry)

    async def test_a_dead_network_is_owed_too(self) -> None:
        def boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        client = CandidateModeClient(
            "https://biz.example.com", lambda: "client-token",
            client=httpx.AsyncClient(transport=httpx.MockTransport(boom)), clock=lambda: T0,
        )
        outcome = await client.request(CANDIDATE, HUMAN_TAKEOVER, reason="我来跟")
        self.assertTrue(outcome.should_retry, "断网不该把本机的接管撤掉")

    async def test_candidate_not_assigned_stops_syncing_that_event(self) -> None:
        """★ 404 `CANDIDATE_NOT_ASSIGNED`：v1.5 §6 要求「停止同步该事件」。

        重试一万次也还是不在计划里，所以**不占欠账**；但本机接管照样成立——
        招聘老师按下去了，不能因为对不上计划就把自动回复的闸放开。
        """
        backend = _Backend(httpx.Response(404, json={
            "error": {"code": "CANDIDATE_NOT_ASSIGNED", "message": "候选人未加入该招聘分身计划或计划已关闭"},
        }))
        outcome = await backend.client().request(CANDIDATE, HUMAN_TAKEOVER, reason="我来跟")
        self.assertFalse(outcome.accepted)
        self.assertFalse(outcome.should_retry, "不在计划里不是「还没送到」，别一直重试")
        self.assertTrue(outcome.local_reject)
        self.assertEqual(outcome.conflict_version, 0, "不是版本冲突，本机档位一个字都不许改")
        self.assertIn("不在当前招聘计划", outcome.reason)

    async def test_a_404_without_an_envelope_is_handled_the_same_way(self) -> None:
        """路由不存在（老版本后端）返回的 404 没有错误信封——解析不出来也不能崩。"""
        outcome = await _Backend(httpx.Response(404, text="Not Found")).client().request(
            CANDIDATE, HUMAN_TAKEOVER, reason="我来跟",
        )
        self.assertFalse(outcome.accepted)
        self.assertTrue(outcome.local_reject)

    async def test_a_version_conflict_carries_the_current_version(self) -> None:
        backend = _Backend(httpx.Response(409, json={
            "error": {
                "code": "CANDIDATE_CONVERSATION_MODE_VERSION_CONFLICT",
                "message": "已发生变化",
                "details": {"currentVersion": 7},
            },
        }))
        outcome = await backend.client().request(CANDIDATE, HUMAN_TAKEOVER, reason="我来跟")
        self.assertFalse(outcome.accepted)
        self.assertEqual(outcome.conflict_version, 7)
        self.assertFalse(outcome.should_retry, "版本冲突不是欠账，是要重新取数再来一次")

    async def test_a_conflict_with_no_version_still_stops_the_retry(self) -> None:
        """★ 版本取不出来也不能退回「欠着」那一档：那会让同一条请求一直撞同一堵墙。"""
        outcome = await _Backend(httpx.Response(409, json={"error": {}})).client().request(
            CANDIDATE, HUMAN_TAKEOVER, reason="我来跟",
        )
        self.assertFalse(outcome.should_retry)
        self.assertEqual(outcome.conflict_version, 1)


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_replays_trigger_and_mode_version_verbatim(self) -> None:
        """★ §4.3：`triggerId`/`modeVersion` **原样回传**，客户端不解析、不重算。"""
        backend = _Backend()
        outcome = await backend.client().report(
            CANDIDATE, HUMAN_TAKEOVER, trigger_id="handoff:p1:c1:1:xyz", mode_version=3,
        )
        self.assertTrue(outcome.accepted)
        _path, body = backend.calls[0]
        self.assertEqual(body["action"], "report")
        self.assertEqual(body["triggerId"], "handoff:p1:c1:1:xyz")
        self.assertEqual(body["modeVersion"], 3)
        self.assertEqual(body["result"], "applied")
        self.assertNotIn("errorCode", body)

    async def test_a_failed_apply_always_carries_an_error_code(self) -> None:
        backend = _Backend()
        await backend.client().report(
            CANDIDATE, HUMAN_TAKEOVER, trigger_id="t1", mode_version=1, applied=False,
        )
        _path, body = backend.calls[0]
        self.assertEqual(body["result"], "failed")
        self.assertEqual(body["errorCode"], "CLIENT_APPLY_FAILED")

    async def test_a_report_with_no_trigger_id_is_not_sent(self) -> None:
        backend = _Backend()
        outcome = await backend.client().report(
            CANDIDATE, HUMAN_TAKEOVER, trigger_id="", mode_version=1,
        )
        self.assertFalse(outcome.accepted)
        self.assertEqual(backend.calls, [])

    async def test_a_null_agent_state_is_still_a_success(self) -> None:
        """★ §4.3 兜底：候选人任务还没初始化时 `agentState` 就是 null，账本照样写进去了。

        客户端**不许**因为它为空回滚本地接管——以 `aiRecruitment.status/version` 为准。
        """
        backend = _Backend(httpx.Response(200, json=_ok(agentState=None)))
        outcome = await backend.client().report(
            CANDIDATE, HUMAN_TAKEOVER, trigger_id="t1", mode_version=1,
        )
        self.assertTrue(outcome.accepted)


class GuardTests(unittest.TestCase):
    """本机那道闸：**一个人一把**，只挂写侧。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.guard = OutboundGuard(self.store, max_sends_per_hour=100, active_hours="00:00-23:59")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _take_over(self, name: str) -> None:
        self.store.set_candidate_mode(ACCOUNT, name, HUMAN_TAKEOVER, reason="我自己跟")

    def test_only_the_taken_over_candidate_is_blocked(self) -> None:
        """★★ 这是与已拆除的分身级总闸最要紧的差别：**别人不受影响**。"""
        self._take_over("张三")
        blocked = self.guard.check(ACCOUNT, "张三", now=T0)
        self.assertFalse(blocked.allowed)
        self.assertIn("人工接管", blocked.reason)
        self.assertTrue(self.guard.check(ACCOUNT, "李四", now=T0).allowed)

    def test_reads_keep_running_during_a_takeover(self) -> None:
        """接管期间照常读消息——把读也停了，交回分身之后就是一段空白。"""
        self._take_over("张三")
        self.assertTrue(self.guard.allow_read(ACCOUNT, "check_new_messages", now=T0).allowed)

    def test_a_takeover_defers_instead_of_settling(self) -> None:
        """★ `temporary=True`：接管两小时 ≠ 丢这个人。上层据此 defer 重投，而不是销账。"""
        self._take_over("张三")
        held = self.guard.reserve(ACCOUNT, "张三", now=T0)
        self.assertFalse(held.allowed)
        self.assertTrue(held.temporary)

    def test_handing_back_reopens_that_candidate(self) -> None:
        self._take_over("张三")
        self.store.set_candidate_mode(ACCOUNT, "张三", AI_MANAGED)
        self.assertTrue(self.guard.check(ACCOUNT, "张三", now=T0).allowed)

    def test_a_candidate_nobody_ever_set_is_not_taken_over(self) -> None:
        """默认必须是「分身在跟」：接管是例外状态，默认接管等于装上就不干活。"""
        row = self.store.candidate_mode(ACCOUNT, "王五")
        self.assertFalse(row.known, "没记录要认得出来，界面不能画成「确定是托管」")
        self.assertFalse(row.taken_over)
        self.assertTrue(self.guard.check(ACCOUNT, "王五", now=T0).allowed)


class ContactsSyncTests(unittest.TestCase):
    """同步名单是候选人状态的来源（用户 2026-08-26 定）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_conversation_mode_lands_when_the_backend_sends_it(self) -> None:
        """业务端 2026-08-26 给 contacts 每行加了 `conversation_mode` 三件套。"""
        self.store.save_contacts(ACCOUNT, [{
            "number": "13800138000", "session_name": "张三", "worker_id": CANDIDATE,
            "updated_at": "2026-08-26T09:00:00.000Z",
            "conversation_mode": HUMAN_TAKEOVER,
            "conversation_mode_source": "enterprise_candidate_mode",
            "conversation_mode_updated_at": "2026-08-26T08:59:00.000Z",
        }])
        row = self.store.candidate_mode(ACCOUNT, "张三")
        self.assertTrue(row.taken_over)
        self.assertEqual(row.candidate_id, CANDIDATE)
        self.assertEqual(row.source, "enterprise_candidate_mode")

    def test_the_backend_can_hand_a_candidate_back_to_the_agent(self) -> None:
        """别人在企业端交回托管 → 这台机器下一次同步名单就该跟上。"""
        self.store.set_candidate_mode(
            ACCOUNT, "张三", HUMAN_TAKEOVER, candidate_id=CANDIDATE, reason="我先跟",
        )
        # 把本机那一行的写入时间推到隔离窗之外（模拟「上周按的」）。
        self._age_local_row("张三", "2026-08-20T09:00:00+00:00")
        self.store.save_contacts(ACCOUNT, [{
            "number": "13800138000", "session_name": "张三", "worker_id": CANDIDATE,
            "updated_at": "2026-08-26T09:00:00.000Z",
            "conversation_mode": AI_MANAGED,
            "conversation_mode_source": "enterprise_candidate_mode",
        }])
        self.assertFalse(self.store.candidate_mode(ACCOUNT, "张三").taken_over)

    def test_a_stale_ai_managed_cannot_undo_a_fresh_local_takeover(self) -> None:
        """★★ 本类最要紧的一条：**「只有 409 才回滚」的第二条路**。

        招聘老师刚按下接管，一条**在那之前就发出去**的名单请求晚到了，里面还写着
        `ai_managed`。照单全收＝「明明点了接管，机器还在发消息」。
        """
        self.store.set_candidate_mode(
            ACCOUNT, "张三", HUMAN_TAKEOVER, candidate_id=CANDIDATE, reason="我来跟",
        )
        self.store.save_contacts(ACCOUNT, [{
            "number": "13800138000", "session_name": "张三", "worker_id": CANDIDATE,
            "updated_at": "2026-08-26T09:00:00.000Z",
            "conversation_mode": AI_MANAGED,
        }])
        self.assertTrue(
            self.store.candidate_mode(ACCOUNT, "张三").taken_over,
            "刚按下的接管不许被一条晚到的托管盖掉",
        )

    def test_a_takeover_from_the_backend_is_always_taken(self) -> None:
        """反方向**不设闸**：往「别发消息」那一侧倒永远是安全的，不用等隔离窗。"""
        self.store.set_candidate_mode(
            ACCOUNT, "张三", AI_MANAGED, candidate_id=CANDIDATE, reason="交回",
        )
        self.store.save_contacts(ACCOUNT, [{
            "number": "13800138000", "session_name": "张三", "worker_id": CANDIDATE,
            "updated_at": "2026-08-26T09:00:00.000Z",
            "conversation_mode": HUMAN_TAKEOVER,
        }])
        self.assertTrue(self.store.candidate_mode(ACCOUNT, "张三").taken_over)

    def test_a_quarantined_row_is_not_skipped_forever(self) -> None:
        """★★ 隔离窗与**增量水位**的咬合：挡下的那一行必须还回得来。

        同步名单是增量的（`updated_since` 水位）。水位一旦推过被挡下的那一行，
        它除非在服务端再变一次否则**永远不会再发回来**——本机就一直停在接管上，
        谁也解不开。所以挡下的时候要把水位**压回它自己那一刻**。
        """
        self.store.set_candidate_mode(
            ACCOUNT, "张三", HUMAN_TAKEOVER, candidate_id=CANDIDATE, reason="我来跟",
        )
        self.store.save_contacts(ACCOUNT, [
            {
                "number": "13800138000", "session_name": "张三", "worker_id": CANDIDATE,
                "updated_at": "2026-08-26T09:00:00.000Z",
                "conversation_mode": AI_MANAGED,
            },
            {
                "number": "13900139000", "session_name": "李四",
                "updated_at": "2026-08-26T10:00:00.000Z",
                "conversation_mode": AI_MANAGED,
            },
        ])
        self.assertLessEqual(
            self.store.contacts_synced_until(ACCOUNT), "2026-08-26T09:00:00.000Z",
            "水位不许越过被挡下的那一行，否则它再也回不来了",
        )
        # 隔离窗过了（模拟「上周按的」）→ 同一行再来一次就该收下。
        self._age_local_row("张三", "2026-08-20T09:00:00+00:00")
        self.store.save_contacts(ACCOUNT, [{
            "number": "13800138000", "session_name": "张三", "worker_id": CANDIDATE,
            "updated_at": "2026-08-26T09:00:00.000Z",
            "conversation_mode": AI_MANAGED,
        }])
        self.assertFalse(self.store.candidate_mode(ACCOUNT, "张三").taken_over)

    def test_a_clean_batch_still_moves_the_watermark_forward(self) -> None:
        """没有被挡下的行时，水位照旧只进不退。"""
        self.store.save_contacts(ACCOUNT, [{
            "number": "13900139000", "session_name": "李四",
            "updated_at": "2026-08-26T10:00:00.000Z",
            "conversation_mode": AI_MANAGED,
        }])
        self.assertEqual(self.store.contacts_synced_until(ACCOUNT), "2026-08-26T10:00:00.000Z")

    def test_a_contacts_row_without_the_field_touches_nothing(self) -> None:
        """老后端不带这个字段。**没带 ≠ 他是托管**——一个字都不能动。"""
        self.store.set_candidate_mode(ACCOUNT, "张三", HUMAN_TAKEOVER, reason="我自己跟")
        self.store.save_contacts(ACCOUNT, [{
            "number": "13800138000", "session_name": "张三", "worker_id": CANDIDATE,
            "updated_at": "2026-08-26T09:00:00.000Z",
        }])
        self.assertTrue(self.store.candidate_mode(ACCOUNT, "张三").taken_over)

    def test_an_unknown_status_word_is_ignored(self) -> None:
        self.store.save_contacts(ACCOUNT, [{
            "number": "13800138000", "session_name": "张三",
            "updated_at": "2026-08-26T09:00:00.000Z",
            "conversation_mode": "paused",
        }])
        self.assertFalse(self.store.candidate_mode(ACCOUNT, "张三").known)

    def _age_local_row(self, session_name: str, written_at: str) -> None:
        """把本机那一行的写入时间改老，绕过接管隔离窗（只在测试里这么干）。"""
        # ★ `with sqlite3.connect(...)` 只管事务**不关连接**。不 close 的话
        #   Windows 上 tearDown 删不掉临时目录（WinError 32），Linux 却照删不误——
        #   一个只在真机上炸的测试 bug。
        with closing(sqlite3.connect(self.store._path)) as conn:
            conn.execute(
                "UPDATE gateway_candidate_mode SET updated_at=? "
                "WHERE account_id=? AND session_name=?",
                (written_at, ACCOUNT, session_name),
            )
            # ★ `closing` 只关连接**不提交**（`with connect(...)` 才提交）。
            #   两个都要：不提交这行改不动，不关连接 Windows 上删不掉临时目录。
            conn.commit()


class OutcomeTests(unittest.TestCase):
    def test_should_retry_separates_the_three_states(self) -> None:
        """欠着 / 版本冲突 / 本机自己拦下——三态，**别压成布尔**。"""
        self.assertTrue(ModeOutcome(False).should_retry, "还没送到 → 欠着")
        self.assertFalse(ModeOutcome(False, conflict_version=3).should_retry, "重新取数再来")
        self.assertFalse(ModeOutcome(False, local_reject=True).should_retry, "写错了，撞墙无用")
        self.assertFalse(ModeOutcome(True).should_retry)


if __name__ == "__main__":
    unittest.main()
