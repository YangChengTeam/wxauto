"""Fake 劳小智 AI 端 WS 服务——按契约回放事件序列，让协议层在 Linux 上可测。

只实现 ``CLAUDE.md`` §4.2 的对外契约，不含任何 codex / 大模型。
Phase B 的入站/出站测试也复用它。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from websockets.asyncio.server import serve


@dataclass
class TurnScript:
    """一轮的回放脚本。"""

    deltas: list[str] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    """一轮里报的阶段（055）。真 AI 端发的是 `status{state:"working", stage:{key,text}}`，
    这里照同一个形状回放——网关侧的 `on_stage` 就是靠它把「AI 现在在干什么」打到实时日志。"""
    cards: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 1234
    error: dict[str, Any] | None = None
    delay_seconds: float = 0.0
    """收到 message.send 后先拖多久——用来测超时。"""


class FakeAgentServer:
    """按契约应答的假 AI 端。

    ``turn_script`` 可以是固定脚本，也可以是 ``(session_id, text) -> TurnScript``
    的函数，用来按输入分支。
    """

    def __init__(
        self,
        *,
        valid_tickets: set[str] | None = None,
        turn_script: TurnScript | Callable[[str, str], TurnScript] | None = None,
        push_on_auth: tuple[str, dict[str, Any]] | None = None,
        pong_delay_seconds: float = 0.0,
    ) -> None:
        self._valid_tickets = valid_tickets
        self._pong_delay = pong_delay_seconds
        """pong 晚多久才回——模拟一帧大文件把 pong 排在后面的样子。"""
        self._push_on_auth = push_on_auth
        """鉴权成功后主动推一帧（不带 ack），用来模拟 AI 端下发监听列表。"""
        self._turn_script = turn_script or TurnScript(
            deltas=["你好", "，还在招人"],
            cards=[{"type": "info", "body": {"kind": "wechat_reply", "reply_segments": ["你好"]}}],
        )
        self.sessions: set[str] = set()
        self.watchlist: set[str] = set()
        """这位员工的监听名单。桌面端可以请求把谁移出去（`wechat.watchlist.remove`）。"""
        self.received: list[dict[str, Any]] = []
        """收到的所有客户端帧，供断言。"""
        self._server: Any = None
        self.port: int = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/ws"

    async def __aenter__(self) -> FakeAgentServer:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ────────────────────────── 协议实现 ──────────────────────────

    async def _handle(self, websocket: Any) -> None:
        authenticated = False
        async for raw in websocket:
            try:
                frame = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            self.received.append(frame)

            op = frame.get("op")
            ack = frame.get("id")
            data = frame.get("data") or {}

            if op == "auth":
                ticket = data.get("session_ticket") or ""
                if self._valid_tickets is not None and ticket not in self._valid_tickets:
                    await self._emit(websocket, ack, "error", {
                        "code": "unauthorized",
                        "message": "authentication failed",
                    })
                    await websocket.close(4401, "unauthorized")
                    return
                authenticated = True
                await self._emit(websocket, ack, "status", {"state": "authenticated"})
                if self._push_on_auth is not None:
                    push_ev, push_data = self._push_on_auth
                    await self._emit(websocket, None, push_ev, push_data)
                continue

            if not authenticated:
                await self._emit(websocket, ack, "error", {
                    "code": "unauthorized",
                    "message": "auth required",
                })
                continue

            if op == "ping":
                # 与真 AI 端一致：ping → pong（网关拿它保活，反代会掐空闲连接）
                if self._pong_delay:
                    await asyncio.sleep(self._pong_delay)
                await self._emit(websocket, ack, "pong", {})
                continue

            if op == "session.start":
                session_id = str(uuid4())
                self.sessions.add(session_id)
                await self._emit(websocket, ack, "session.created", {"session_id": session_id})
                continue

            if op == "session.resume":
                session_id = data.get("session_id") or ""
                if session_id not in self.sessions:
                    await self._emit(websocket, ack, "error", {
                        "code": "forbidden",
                        "message": "session does not belong to current principal",
                    })
                    continue
                await self._emit(websocket, ack, "session.resumed", {"session_id": session_id})
                continue

            if op == "wechat.watchlist.sync":
                # 和真 AI 端同一份契约（`ws/wechatChannel.ts::handleWatchlistSync`）：
                # 什么都不改，只把名单**照推送那条路**再发一遍，再回一帧说清有几个。
                # ★ 名单不进这一帧——网关只有 `apply_push` 一个落地口。
                await self._emit(websocket, None, "wechat.watchlist",
                                 {"only": sorted(self.watchlist), "never": []})
                await self._emit(websocket, ack, "wechat.watchlist.synced",
                                 {"watching": len(self.watchlist), "blocked": 0})
                continue

            if op == "wechat.watchlist.remove":
                # 和真 AI 端同一份契约（`ws/wechatChannel.ts::handleWatchlistRemove`）：
                # 真删 → 立刻把新名单推回来 → 再回一帧说清删掉了哪几个。
                names = [str(n) for n in (data.get("names") or []) if str(n).strip()]
                removed = [n for n in names if n in self.watchlist]
                for name in removed:
                    self.watchlist.discard(name)
                if removed:
                    await self._emit(websocket, None, "wechat.watchlist",
                                     {"only": sorted(self.watchlist), "never": []})
                await self._emit(websocket, ack, "wechat.watchlist.removed",
                                 {"removed": removed, "watching": len(self.watchlist),
                                  "blocked": 0})
                continue

            if op == "message.send":
                asyncio.create_task(self._run_turn(websocket, ack, data))
                continue

    async def _run_turn(self, websocket: Any, ack: str | None, data: dict[str, Any]) -> None:
        session_id = data.get("session_id") or ""
        text = data.get("text") or ""
        script = (
            self._turn_script(session_id, text)
            if callable(self._turn_script)
            else self._turn_script
        )

        await self._emit(websocket, ack, "status", {"session_id": session_id, "state": "running"})
        if script.delay_seconds:
            await asyncio.sleep(script.delay_seconds)

        if script.error is not None:
            await self._emit(websocket, ack, "error", {"session_id": session_id, **script.error})
            return

        for stage in script.stages:
            await self._emit(
                websocket, ack, "status",
                {"session_id": session_id, "state": "working", "stage": stage},
            )
        for delta in script.deltas:
            await self._emit(
                websocket, ack, "text.delta", {"session_id": session_id, "delta": delta}
            )
        for card in script.cards:
            await self._emit(websocket, ack, "card", {"session_id": session_id, "card": card})
        await self._emit(
            websocket,
            ack,
            "status",
            {"session_id": session_id, "state": "done", "elapsed_ms": script.elapsed_ms},
        )

    @staticmethod
    async def _emit(websocket: Any, ack: str | None, ev: str, data: dict[str, Any]) -> None:
        frame: dict[str, Any] = {"v": 1, "ev": ev, "data": data}
        if ack:
            frame["ack"] = ack
        await websocket.send(json.dumps(frame, ensure_ascii=False))
