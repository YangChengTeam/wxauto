"""劳小智 AI 服务的 WS 客户端。

网关是 AI 端那道门的普通客户端——协议与浏览器走的完全一致，AI 端零改动。
帧格式与事件序列见 ``CLAUDE.md`` §4.2。

一条连接绑定单一身份（一个招聘专员），可承载该专员名下多个候选人会话；
换专员必须换连接。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import structlog
from websockets.asyncio.client import connect as ws_connect

_log = structlog.get_logger(__name__)

# 一轮结束的两种信号：AI 端既可能发 ev=done，也可能发 status{state:done}。两者都要认。
_TURN_DONE_EVENTS = frozenset({"done"})


class AgentWsError(RuntimeError):
    """与 AI 端通信失败。"""


class Unauthorized(AgentWsError):
    """鉴权失败——票据无效、过期或已被撤销。调用方应重新换票再连。"""


class TurnTimeout(AgentWsError):
    """一轮对话超时。"""


@dataclass
class TurnResult:
    """一轮对话的产出。"""

    session_id: str
    cards: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    elapsed_ms: int | None = None
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def card(self) -> dict[str, Any] | None:
        """首张卡片。网关只关心一张，但多张也不丢（见 cards）。"""
        return self.cards[0] if self.cards else None


@dataclass
class _Turn:
    """进行中的一轮：收集流式产出，直到 done/error。"""

    session_id: str
    future: asyncio.Future[TurnResult]
    cards: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)
    on_delta: Callable[[str], None] | None = None

    def finish(self, *, elapsed_ms: int | None = None, error: dict | None = None) -> None:
        if self.future.done():
            return
        self.future.set_result(
            TurnResult(
                session_id=self.session_id,
                cards=self.cards,
                text="".join(self.chunks),
                elapsed_ms=elapsed_ms,
                error=error,
            )
        )


class AgentWsClient:
    """AI 端 WS 门的客户端。

    生命周期：``connect(ticket)`` → ``start_session``/``resume_session`` →
    ``send_message`` × N → ``close()``。票只在 connect 时用一次，之后不再需要。
    """

    def __init__(
        self,
        ws_url: str,
        *,
        turn_timeout_seconds: float = 180.0,
        op_timeout_seconds: float = 30.0,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._url = ws_url
        self._turn_timeout = turn_timeout_seconds
        self._op_timeout = op_timeout_seconds
        self._connect = connect_factory or ws_connect

        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._ids = count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._turns: dict[str, _Turn] = {}
        self._closed = asyncio.Event()

    # ────────────────────────── 连接与鉴权 ──────────────────────────

    async def connect(self, session_ticket: str) -> None:
        """建连并用短票鉴权。票用完即弃，不保存。"""
        self._ws = await self._connect(self._url)
        self._closed.clear()
        self._reader = asyncio.create_task(self._read_loop())

        try:
            data = await self._request("auth", {"session_ticket": session_ticket})
        except AgentWsError:
            await self.close()
            raise

        if data.get("state") != "authenticated":
            await self.close()
            raise Unauthorized(f"鉴权未通过: {data!r}")
        _log.info("已连接 AI 端")

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不影响调用方
                pass
        self._closed.set()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader = None
        self._fail_all(AgentWsError("连接已关闭"))
        self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._closed.is_set()

    # ────────────────────────── 会话 ──────────────────────────

    async def start_session(self, task_type: str) -> str:
        """新建会话，返回 session_id。对外只有 session_id，没有 thread_id。"""
        data = await self._request("session.start", {"task_type": task_type})
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise AgentWsError(f"session.start 未返回 session_id: {data!r}")
        return session_id

    async def resume_session(self, session_id: str) -> None:
        """接回已有会话（同一 thread，历史还在）。断线重连走这条。"""
        await self._request("session.resume", {"session_id": session_id})

    # ────────────────────────── 对话 ──────────────────────────

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        refs: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        on_delta: Callable[[str], None] | None = None,
    ) -> TurnResult:
        """发一轮并等到收口。

        同一 session 同时只允许一轮——重复调用会直接报错，不排队。串行由调用方保证。
        """
        if session_id in self._turns:
            raise AgentWsError(f"会话 {session_id} 上已有进行中的一轮")

        payload: dict[str, Any] = {"session_id": session_id, "text": text}
        if refs:
            payload["refs"] = refs
        if extra:
            payload.update(extra)

        loop = asyncio.get_running_loop()
        turn = _Turn(session_id=session_id, future=loop.create_future(), on_delta=on_delta)
        self._turns[session_id] = turn
        try:
            await self._send("message.send", payload)
            return await asyncio.wait_for(turn.future, timeout=self._turn_timeout)
        except TimeoutError as exc:
            raise TurnTimeout(f"会话 {session_id} 一轮超时") from exc
        finally:
            self._turns.pop(session_id, None)

    async def interrupt(self, session_id: str) -> None:
        await self._send("message.interrupt", {"session_id": session_id})

    # ────────────────────────── 内部 ──────────────────────────

    async def _send(self, op: str, data: dict[str, Any], *, ack: str | None = None) -> None:
        if self._ws is None:
            raise AgentWsError("尚未连接")
        # 每一帧都带 id：请求-响应型 op 靠它对号入座，流式 op 靠它把 AI 端回来的
        # ack 关联到本次发送（便于排障）。轮内事件仍按 session_id 归位，不依赖 ack。
        frame: dict[str, Any] = {"v": 1, "id": ack or f"{op}-{next(self._ids)}", "op": op, "data": data}
        try:
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            raise AgentWsError(f"发送 {op} 失败: {exc}") from exc

    async def _request(self, op: str, data: dict[str, Any]) -> dict[str, Any]:
        """发一个请求-响应型 op，按 ack 对号入座。"""
        ack = f"{op}-{next(self._ids)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[ack] = future
        try:
            await self._send(op, data, ack=ack)
            return await asyncio.wait_for(future, timeout=self._op_timeout)
        except TimeoutError as exc:
            raise AgentWsError(f"{op} 超时") from exc
        finally:
            self._pending.pop(ack, None)

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except Exception:  # noqa: BLE001 —— 坏帧不该掀翻连接
                    _log.warning("收到无法解析的帧")
                    continue
                if isinstance(frame, dict):
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.warning("读循环结束", error=str(exc))
        finally:
            self._closed.set()
            self._fail_all(AgentWsError("连接已断开"))

    def _dispatch(self, frame: dict[str, Any]) -> None:
        ev = frame.get("ev")
        ack = frame.get("ack")
        data = frame.get("data")
        data = data if isinstance(data, dict) else {}

        # 1) 轮内流式事件优先按 session_id 归位（ack 不一定在每条上）。
        session_id = data.get("session_id")
        turn = self._turns.get(session_id) if isinstance(session_id, str) else None

        if turn is not None and self._handle_turn_event(turn, ev, data):
            return

        # 2) 请求-响应型 op 按 ack 归位。
        if isinstance(ack, str) and ack in self._pending:
            future = self._pending[ack]
            if not future.done():
                if ev == "error":
                    future.set_exception(self._error_of(data))
                else:
                    future.set_result(data)
            return

        # 3) 未匹配的错误帧：若只有一轮在跑，归给它；否则记日志丢弃。
        if ev == "error" and len(self._turns) == 1:
            only = next(iter(self._turns.values()))
            only.finish(error=self._error_payload(data))
            return

        _log.debug("未归位的事件", ev=ev)

    def _handle_turn_event(self, turn: _Turn, ev: Any, data: dict[str, Any]) -> bool:
        """处理归属于某一轮的事件；返回 True 表示已消费。"""
        if ev == "text.delta":
            delta = data.get("delta")
            if isinstance(delta, str) and delta:
                turn.chunks.append(delta)
                if turn.on_delta is not None:
                    turn.on_delta(delta)
            return True

        if ev == "card":
            card = data.get("card")
            if isinstance(card, dict):
                turn.cards.append(card)
            return True

        if ev in _TURN_DONE_EVENTS:
            turn.finish(elapsed_ms=_as_int(data.get("elapsed_ms")))
            return True

        if ev == "status":
            state = data.get("state")
            if state == "done":
                turn.finish(elapsed_ms=_as_int(data.get("elapsed_ms")))
            # running / retrying / stage 等只是进度，不收口。
            return True

        if ev == "error":
            turn.finish(error=self._error_payload(data))
            return True

        return False

    @staticmethod
    def _error_payload(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": data.get("code") or "unknown",
            "message": data.get("message") or "",
            "fallback_card": data.get("fallback_card"),
        }

    @staticmethod
    def _error_of(data: dict[str, Any]) -> AgentWsError:
        code = data.get("code") or "unknown"
        message = data.get("message") or ""
        if code in {"unauthorized", "forbidden"}:
            return Unauthorized(f"{code}: {message}")
        return AgentWsError(f"{code}: {message}")

    def _fail_all(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        for turn in list(self._turns.values()):
            if not turn.future.done():
                turn.future.set_exception(exc)
        self._turns.clear()


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None
