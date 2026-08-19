"""AI 端推来的「这一轮发生了什么」（``wechat.notice``）——只进日志，不执行任何动作。

★ 为什么加这条通道（用户 2026-08-19）：这台电脑上原本**只在有指令下来时**才有动静。
后端按计划触发的一轮，如果 AI 判断这次不用发消息，本机日志里一个字都没有——
拿着电脑的人分不出「AI 没收到」「AI 觉得不用做」「投递丢了」哪一种。

这里钉两件事：① 它真的会写进日志（不然等于没加）；② 它**不会**碰微信
（一条只用来看的推送要是能触发动作，就成了绕过指令白名单的第二条路）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from wxclient.gateway import (
    NOTICE_EVENT,
    GatewayRunner,
    GatewaySettings,
    GatewayStore,
    LocalFileTicketProvider,
)

T0 = datetime(2026, 8, 19, 14, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "wxid_zhangsan_work"


class SilentSource:
    async def fetch(self, account_id: str) -> list:
        return []


class SilentSink:
    async def send(self, account_id: str, peer_id: str, text: str) -> str:  # pragma: no cover
        raise AssertionError("一条只用来看的通知不该往微信发任何东西")


class NoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        tickets = root / "tickets.json"
        tickets.write_text(json.dumps({ACCOUNT: "tkt-ok"}), encoding="utf-8")
        self.runner = GatewayRunner(
            settings=GatewaySettings(
                ai_ws_url="ws://127.0.0.1:1/v1/ws",
                local_ticket_file=tickets,
                turn_timeout_seconds=10.0,
                active_hours="00:00-23:59",
            ),
            ticket_provider=LocalFileTicketProvider(tickets),
            store=GatewayStore(root / "gateway.db"),
            source=SilentSource(),
            sink=SilentSink(),
            accounts=[ACCOUNT],
            clock=lambda: T0,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_it_writes_the_line_into_the_local_log(self) -> None:
        with patch("wxclient.gateway.runner._log") as log:
            self.runner._on_push(ACCOUNT, NOTICE_EVENT, {"text": "收到一轮任务：入职第 3 天回访", "source": "trigger"})
        log.info.assert_called_once()
        self.assertIn("入职第 3 天回访", json.dumps(log.info.call_args.kwargs, ensure_ascii=False))

    def test_an_empty_notice_writes_nothing(self) -> None:
        """AI 端偶尔推一条空的，不该在日志里留一行空白。"""
        with patch("wxclient.gateway.runner._log") as log:
            self.runner._on_push(ACCOUNT, NOTICE_EVENT, {"text": "   "})
            self.runner._on_push(ACCOUNT, NOTICE_EVENT, {})
        log.info.assert_not_called()

    def test_it_never_dispatches_anything(self) -> None:
        """★ 通知里就算写着看起来像指令的东西，也一条都不许下发。"""
        with patch.object(self.runner, "_dispatch_commands") as dispatch, \
             patch("wxclient.gateway.runner._log"):
            self.runner._on_push(
                ACCOUNT,
                NOTICE_EVENT,
                {"text": "给张三发消息", "commands": [{"op": "send_message", "args": {"to": "张三", "text": "x"}}]},
            )
        dispatch.assert_not_called()

    def test_an_unknown_push_is_still_ignored_quietly(self) -> None:
        """AI 端将来加别的推送时，旧网关必须照跑（不抛、不断连）。"""
        with patch("wxclient.gateway.runner._log"):
            self.runner._on_push(ACCOUNT, "wechat.something.new", {"whatever": 1})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
