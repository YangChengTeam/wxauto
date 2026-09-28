"""名单外挂红点的单聊也读一下，交 AI 端判断（用户 2026-09-28）。

重点是**不该读的都没读**：名单里的人（走老路）、群、服务号/公众号、本机拉黑的、人工接管中的；
以及**不该触发的都没触发**——读不到时不能进「连续读不到 → 加好友 / 移出名单」那两条计数，
也不能往监听名单里加任何人（名单只认同步，AGENTS.md §10）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway import GatewayStore
from wxclient.gateway.inbound import InboundMessage, message_from_row, message_to_row
from wxclient.gateway.sessions import HUMAN_TAKEOVER
from wxclient.gateway.watchlist import Watchlist
from wxclient.gateway.wechat_io import MAX_UNREAD_STRANGERS_PER_POLL, QueueMessageSource
from wxclient.queue import TaskType

ACCOUNT = "wxid_test"
WATCHED = "张三"


def unread(*pairs: tuple[str, int]) -> dict:
    return {
        "status": "succeeded",
        "result": {"unread_sessions": [{"name": n, "unread_count": c} for n, c in pairs]},
    }


def history(*texts: str, is_group: bool = False) -> dict:
    return {
        "status": "succeeded",
        "result": {
            "messages": [
                {"text": t, "sender": "对方", "timestamp_is_today": True, **({"is_group": True} if is_group else {})}
                for t in texts
            ]
        },
    }


class _Rt:
    """按任务类型回话的假运行时；记下每一次读了谁。"""

    def __init__(self, unread_result, histories: dict[str, dict]) -> None:
        self.unread_result = unread_result
        self.histories = histories
        self.reads: list[str] = []
        self.unread_calls = 0

    async def submit(self, task_type, payload, confirmation: str = ""):
        if task_type == TaskType.GET_UNREAD_MESSAGES:
            self.unread_calls += 1
            if isinstance(self.unread_result, Exception):
                raise self.unread_result
            return self.unread_result
        if task_type == TaskType.READ_CHAT_HISTORY:
            self.reads.append(payload["friend"])
            return self.histories.get(payload["friend"], history())
        return {"status": "succeeded", "result": {}}


class UnreadStrangerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.watchlist = Watchlist()
        self.watchlist.apply_backend({"only": [WATCHED]})
        self.shouts: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _source(self, rt: _Rt, *, allow_real: bool = True, enabled: bool = True) -> QueueMessageSource:
        source = QueueMessageSource(
            runtime=rt, store=self.store, watchlist=self.watchlist,
            prefilter_by_session_list=False, read_unread_strangers=enabled, allow_real_actions=allow_real,
        )

        async def unreachable(account_id, peer_id, reason):
            self.shouts.append(f"unreachable:{peer_id}")

        async def not_found(account_id, peer_id):
            self.shouts.append(f"not_found:{peer_id}")

        source.set_unreachable_handler(unreachable)
        source.set_not_found_handler(not_found)
        return source

    async def test_stranger_with_red_dot_is_read_and_marked_first_contact(self) -> None:
        rt = _Rt(unread(("李四", 2)), {"李四": history("你好", "还招人吗")})
        got = await self._source(rt).fetch(ACCOUNT)
        stranger = [m for m in got if m.peer_id == "李四"]
        self.assertEqual([m.text for m in stranger], ["你好", "还招人吗"])
        self.assertTrue(all(m.first_contact for m in stranger))
        self.assertEqual(self.watchlist.watched(), [WATCHED], "★ 绝不往名单里加人")

    async def test_watched_peers_are_not_marked(self) -> None:
        rt = _Rt(unread(), {WATCHED: history("在吗")})
        got = await self._source(rt).fetch(ACCOUNT)
        self.assertEqual([(m.peer_id, m.first_contact) for m in got], [(WATCHED, False)])

    async def test_only_as_many_as_the_red_dot_says(self) -> None:
        rt = _Rt(unread(("李四", 1)), {"李四": history("旧的一句", "新的一句")})
        got = await self._source(rt).fetch(ACCOUNT)
        self.assertEqual([m.text for m in got if m.peer_id == "李四"], ["新的一句"])

    async def test_excluded_sessions_are_never_opened(self) -> None:
        self.store.blacklist(ACCOUNT, "拉黑的")
        self.store.mark_peer_kind(ACCOUNT, "某群", is_group=True)
        self.store.set_candidate_mode(ACCOUNT, "接管中", HUMAN_TAKEOVER)
        self.watchlist.apply_backend({"only": [WATCHED, "不管的"]})
        self.watchlist.exclude("不管的")  # 配置中心点过「移出」的：落在本机 never，红点亮了也不读
        rt = _Rt(
            unread(
                (WATCHED, 3), ("微信支付", 1), ("腾讯新闻", 2), ("服务号", 4), ("文件传输助手", 1),
                ("拉黑的", 1), ("某群", 5), ("接管中", 1), ("不管的", 1),
            ),
            {},
        )
        await self._source(rt).fetch(ACCOUNT)
        self.assertEqual(rt.reads, [WATCHED], "名单外的这些一个都不该开窗")

    async def test_group_found_on_open_is_dropped_and_remembered(self) -> None:
        rt = _Rt(unread(("新群", 3)), {"新群": history("大家好", is_group=True)})
        source = self._source(rt)
        got = await source.fetch(ACCOUNT)
        self.assertEqual([m for m in got if m.peer_id == "新群"], [])
        self.assertTrue(self.store.is_known_group(ACCOUNT, "新群"))

    async def test_at_most_n_per_round(self) -> None:
        names = [f"人{i}" for i in range(MAX_UNREAD_STRANGERS_PER_POLL + 2)]
        rt = _Rt(unread(*((n, 1) for n in names)), {})
        await self._source(rt).fetch(ACCOUNT)
        self.assertEqual([r for r in rt.reads if r != WATCHED], names[:MAX_UNREAD_STRANGERS_PER_POLL])

    async def test_read_failure_never_counts_toward_add_friend_or_removal(self) -> None:
        """★ 陌生会话读不到 ≠ 名单里的人读不到：那两条计数数够了会去加好友 / 移出名单。"""
        failed = {"status": "failed", "last_error": "查无此人", "result": {"reason_code": "CONTACT_NOT_FOUND"}}
        rt = _Rt(unread(("李四", 1)), {"李四": failed})
        source = self._source(rt)
        for _ in range(5):
            source._last_full_at = None  # 每轮都当全量巡检
            await source.fetch(ACCOUNT)
        self.assertEqual(self.shouts, [])

    async def test_unread_read_failure_does_not_break_the_watchlist_round(self) -> None:
        rt = _Rt(RuntimeError("微信卡住了"), {WATCHED: history("在吗")})
        got = await self._source(rt).fetch(ACCOUNT)
        self.assertEqual([m.peer_id for m in got], [WATCHED])

    async def test_dry_run_machine_or_switch_off_does_not_look(self) -> None:
        for kwargs in ({"allow_real": False}, {"enabled": False}):
            rt = _Rt(unread(("李四", 1)), {})
            await self._source(rt, **kwargs).fetch(ACCOUNT)
            self.assertEqual(rt.unread_calls, 0, kwargs)

    async def test_empty_watchlist_still_looks_at_red_dots(self) -> None:
        self.watchlist.apply_backend({"only": []})
        rt = _Rt(unread(("李四", 1)), {"李四": history("你好")})
        got = await self._source(rt).fetch(ACCOUNT)
        self.assertEqual([m.peer_id for m in got], ["李四"])

    async def test_quick_rounds_do_not_look(self) -> None:
        """红点只在全量巡检那轮看——快读每 30 秒一次，那节拍看红点就是多动一次微信。"""
        rt = _Rt(unread(("李四", 1)), {})
        source = self._source(rt)
        source.set_full_interval(180)
        await source.fetch(ACCOUNT)
        await source.fetch(ACCOUNT)
        self.assertEqual(rt.unread_calls, 1)


class FirstContactRowTests(unittest.TestCase):
    def test_survives_the_buffer_round_trip(self) -> None:
        from datetime import datetime, timezone

        msg = InboundMessage(
            account_id=ACCOUNT, peer_id="李四", msg_id="李四#1", text="你好",
            at=datetime.now(timezone.utc), first_contact=True,
        )
        self.assertTrue(message_from_row(ACCOUNT, "李四", message_to_row(msg)).first_contact)


class TurnMetaTests(unittest.TestCase):
    def _meta(self, first_contact: bool) -> dict:
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from wxclient.gateway.runner import GatewayRunner

        msg = InboundMessage(
            account_id=ACCOUNT, peer_id="李四", msg_id="李四#1", text="你好",
            at=datetime.now(timezone.utc), first_contact=first_contact,
        )
        return GatewayRunner._turn_meta(SimpleNamespace(watched_peers=lambda: [WATCHED]), "李四", [msg])

    def test_red_dot_turn_tells_the_ai_service(self) -> None:
        self.assertIs(self._meta(True).get("first_contact"), True)

    def test_watched_turn_does_not(self) -> None:
        self.assertNotIn("first_contact", self._meta(False))


if __name__ == "__main__":
    unittest.main()
