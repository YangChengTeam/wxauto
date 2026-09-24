"""名单里的会话连续几轮都「查无此人」→ 移出监听名单，之后不再读它（用户 2026-09-24）。

重点是**不该移的都没移**：移出落在本机「不管」里、界面上没有恢复按钮，
一次偶发（搜索那一刻 UI 没刷出来）或别的读失败（微信卡了、窗口打不开）都不能把人踢出去。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wxclient.gateway import GatewayStore
from wxclient.gateway.runner import GatewayRunner
from wxclient.gateway.watchlist import Watchlist
from wxclient.gateway.wechat_io import QueueMessageSource
from wxclient.wechat_api.errors import CONTACT_NOT_FOUND_CODE

ACCOUNT = "wxid_test"
NAME = "张三"

NOT_FOUND = {
    "status": "failed",
    "last_error": f"微信里搜不到「{NAME}」这个会话（备注名对不上，或还不是好友），没读到消息",
    "result": {"outcome": "not_found", "reason_code": CONTACT_NOT_FOUND_CODE, "retry_suppressed": True},
}
OTHER_FAILURE = {"status": "failed", "last_error": "打开会话失败"}
OK = {"status": "succeeded", "result": {"messages": []}}


class NotFoundStrikesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = GatewayStore(Path(self._tmp.name) / "gateway.db")
        self.shouts: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _source(self, answers: list[dict]) -> QueueMessageSource:
        class _Rt:
            def __init__(self) -> None:
                self._left = list(answers)

            async def submit(self, task_type, payload):
                return self._left.pop(0)

        async def shout(account_id: str, peer_id: str) -> None:
            self.shouts.append((account_id, peer_id))

        watchlist = Watchlist()
        watchlist.apply_backend({"only": [NAME]})
        source = QueueMessageSource(runtime=_Rt(), store=self.store, watchlist=watchlist)
        source.set_not_found_handler(shout)
        return source

    async def _rounds(self, answers: list[dict]) -> None:
        source = self._source(answers)
        for _ in answers:
            await source.fetch(ACCOUNT)

    async def test_three_rounds_in_a_row_shouts_once(self) -> None:
        await self._rounds([NOT_FOUND] * 3)
        self.assertEqual(self.shouts, [(ACCOUNT, NAME)])

    async def test_one_or_two_rounds_do_not_shout(self) -> None:
        await self._rounds([NOT_FOUND] * 2)
        self.assertEqual(self.shouts, [], "偶发的查无此人不能就把人移出去")

    async def test_other_read_failures_never_count(self) -> None:
        """★ 微信卡了、窗口打不开也读不到——那不是「查无此人」，一个都不能移。"""
        await self._rounds([OTHER_FAILURE] * 5)
        self.assertEqual(self.shouts, [])

    async def test_it_has_to_be_in_a_row(self) -> None:
        await self._rounds([NOT_FOUND, NOT_FOUND, OTHER_FAILURE, NOT_FOUND, NOT_FOUND])
        self.assertEqual(self.shouts, [], "中间换成别的失败，连击清零")
        await self._rounds([NOT_FOUND, NOT_FOUND, OK, NOT_FOUND, NOT_FOUND])
        self.assertEqual(self.shouts, [], "中间读通过一次，连击清零")


class RunnerExclusionTests(unittest.IsolatedAsyncioTestCase):
    """runner 收到喊话 → 本机「不管」；093 确认通过了好友 → 放回来。"""

    def _runner(self) -> SimpleNamespace:
        watchlist = Watchlist()
        watchlist.apply_backend({"only": [NAME, "李四"]})
        return SimpleNamespace(_watchlist=watchlist)

    async def test_not_found_moves_him_out_of_the_watchlist(self) -> None:
        runner = self._runner()
        await GatewayRunner._on_contact_not_found(runner, ACCOUNT, NAME)  # type: ignore[arg-type]
        self.assertEqual(runner._watchlist.watched(), ["李四"])
        # 业务端同步只换「只管」，不会把他带回来。
        runner._watchlist.apply_backend({"only": [NAME, "李四"]})
        self.assertEqual(runner._watchlist.watched(), ["李四"])

    async def test_confirmed_friend_comes_back(self) -> None:
        runner = self._runner()
        runner._watchlist.exclude(NAME)
        self.assertTrue(runner._watchlist.include(NAME))
        self.assertEqual(runner._watchlist.watched(), ["张三", "李四"])
        self.assertFalse(runner._watchlist.include("王五"), "本来就不在「不管」里的，什么都不做")


if __name__ == "__main__":
    unittest.main()
