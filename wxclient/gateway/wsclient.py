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

#: 单帧上限。websockets 默认 1MB，而 067 发文件时**字节随指令一起推下来**
#: （两张海报 ≈ 0.7MB base64，已经贴着默认值）。给到 16MB，超限会直接断连很难查。
MAX_FRAME_BYTES = 16 * 1024 * 1024

#: 底层保活：`websockets` 自己每 ``PING_INTERVAL_SECONDS`` 发一次 ping，``PING_TIMEOUT_SECONDS`` 内
#: 没等到 pong 就以 1011 关连接（日志里那句 ``keepalive ping timeout``）。
#:
#: ★ 为什么超时给到 120 秒而不是库默认的 20 秒（2026-09-15 事故）：服务器出网被业务后端占满，
#:   实测到专员电脑只有 ≈14 KB/s；AI 端发朋友圈配图是**字节随指令帧一起推下来**的（067），一张
#:   1.6 MB 的 PNG 转 base64 要传两分半——帧还没传完，pong 排在它后面进不来，20 秒一到连接就被
#:   自己判死，指令连「已收到」都来不及回，朋友圈一次都没发成（当天三次 1011）。
#:   AI 端那头已把图压到百 KB 级，这里再放宽：慢≠死，判死的门槛要高过一帧正常传完的时间。
#:   代价是真断线要 120 秒才发现——读消息本来就是 180 秒一趟，等得起。
PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 120.0


async def _connect_with_limits(url: str):
    return await ws_connect(
        url,
        max_size=MAX_FRAME_BYTES,
        ping_interval=PING_INTERVAL_SECONDS,
        ping_timeout=PING_TIMEOUT_SECONDS,
    )

# 一轮结束的两种信号：AI 端既可能发 ev=done，也可能发 status{state:done}。两者都要认。
_TURN_DONE_EVENTS = frozenset({"done"})


class AgentWsError(RuntimeError):
    """与 AI 端通信失败。"""


class Unauthorized(AgentWsError):
    """鉴权失败——票据无效、过期或已被撤销。调用方应重新换票再连。"""


class SessionGone(Unauthorized):
    """AI 端说这个会话不存在了（``not_found``）：被名单上的「清除」删掉，或过期回收了。

    归成 ``Unauthorized`` 的一种，好让每个调用点原有的那条「丢掉本地映射、开新会话」直接接住它。
    在这之前它是普通的 ``AgentWsError``：调用方只会拿同一个旧会话重试，这个联系人从此每轮都失败。
    """


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
        """这一轮的**结论卡**。网关只关心一张，但多张也不丢（见 cards）。

        ★ 不是「首张」。2026-09-07 事故：模型探路时跑了个不存在的子命令，AI 端先出了一张
          ``failed`` 通知卡，两分钟后才出真的 ``wechat_reply`` 卡；这里原来取 ``cards[0]``，
          于是出站看到的是通知卡 → 「本轮不回复」，候选人的三条消息一个字没回。

        取法：最后一张 ``body.kind == "wechat_reply"`` 的卡（同一轮出了两张就以后一张为准——
        那是它改过主意之后的结论）；一张都没有再退回首张（通知卡也好、别的卡也好，
        让出站按它的 kind 如实记「为什么不回」）。
        """
        for card in reversed(self.cards):
            body = card.get("body")
            if isinstance(body, dict) and body.get("kind") == "wechat_reply":
                return card
        return self.cards[0] if self.cards else None


@dataclass
class _Turn:
    """进行中的一轮：收集流式产出，直到 done/error。"""

    session_id: str
    future: asyncio.Future[TurnResult]
    cards: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)
    on_delta: Callable[[str], None] | None = None
    on_stage: Callable[[dict[str, Any]], None] | None = None

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
        ping_timeout_seconds: float | None = None,
        connect_factory: Callable[..., Any] | None = None,
        on_push: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._url = ws_url
        self._turn_timeout = turn_timeout_seconds
        self._op_timeout = op_timeout_seconds
        # 没给就读模块的 `PING_TIMEOUT_SECONDS`（建对象时读，极速模式改得到）。
        self._ping_timeout = PING_TIMEOUT_SECONDS if ping_timeout_seconds is None else ping_timeout_seconds
        self._connect = connect_factory or _connect_with_limits
        self._on_push = on_push

        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._ids = count(1)
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._turns: dict[str, _Turn] = {}
        self._closed = asyncio.Event()
        #: 这一轮已经流下来多少段增量、上一次记下来的状态。只用于日志（见 `_log_frame`）。
        self._deltas = 0
        self._state = ""

    # ────────────────────────── 连接与鉴权 ──────────────────────────

    async def connect(self, session_ticket: str) -> None:
        """建连并用短票鉴权。票用完即弃，不保存。"""
        # 握手和鉴权是两步，卡住的时候要分得清卡在哪一步（票不对是鉴权失败，
        # 连不上是网络/反代），所以两步各留一条。**票本身一个字符都不进日志。**
        _log.info("正在连接 AI 端", url=self._url)
        self._ws = await self._connect(self._url)
        self._closed.clear()
        self._reader = asyncio.create_task(self._read_loop())

        _log.info("已握手，正在用短票鉴权")
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
            _log.info("正在断开 AI 端连接")
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001, S110 —— 关闭失败不影响调用方
                pass
        self._closed.set()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
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
        _log.info("已在 AI 端新建会话", task_type=task_type, session=session_id[:8])
        return session_id

    async def resume_session(self, session_id: str) -> None:
        """接回已有会话（同一 thread，历史还在）。断线重连走这条。"""
        await self._request("session.resume", {"session_id": session_id})
        _log.info("已接回 AI 端会话", session=session_id[:8])

    # ────────────────────────── 对话 ──────────────────────────

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        refs: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_stage: Callable[[dict[str, Any]], None] | None = None,
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
        turn = _Turn(session_id=session_id, future=loop.create_future(), on_delta=on_delta,
                     on_stage=on_stage)
        self._turns[session_id] = turn
        try:
            await self._send("message.send", payload)
            return await asyncio.wait_for(turn.future, timeout=self._turn_timeout)
        except TimeoutError as exc:
            raise TurnTimeout(f"会话 {session_id} 一轮超时") from exc
        finally:
            self._turns.pop(session_id, None)

    async def notify(self, op: str, data: dict[str, Any]) -> None:
        """发一个**不等回应**的 op（指令回执走这条）。

        回执不能用 `_request`：它要等服务端回一帧，而回执本身就是「我这边办完了」的
        单向通知，等回来只会把读循环和执行循环缠在一起。
        """
        await self._send(op, data)

    async def interrupt(self, session_id: str) -> None:
        await self._send("message.interrupt", {"session_id": session_id})

    async def sync_watchlist(self) -> dict[str, Any]:
        """请 AI 端**现在就把名单发一份下来**。返回它说的 ``{watching, blocked}``。

        名单平时是 AI 端主动推的（登记那一刻推一次、名单一变再推一次），够用；这条补的是
        推送**没送到**的时候——推的那一瞬间这台机器正在重连、或者名单是在别处改的。

        ★ 名单本身不在这条的应答里，它照旧走 ``ev: "wechat.watchlist"`` 那条推送
        （`_on_push` → `Watchlist.apply_push`）。**只留一个落地口**：多解析一处
        就多一份会漂的实现。这里等回应，是因为界面要照实话说「同步到几个会话」。
        """
        return await self._request("wechat.watchlist.sync", {})

    async def remove_watchlist(self, names: list[str]) -> dict[str, Any]:
        """把这几个会话移出自动回复名单。返回 AI 端说的 ``{removed, watching, blocked}``。

        **删除必须发生在 AI 端**：名单的权威在那儿，推一次覆盖一次——本机删掉只能撑到
        下一次推送，删了立刻又回来。这里等回应（`_request`），因为界面要照实话回复
        「删掉了没有」，而不是发完就说成了。
        """
        return await self._request("wechat.watchlist.remove", {"names": names})

    # ────────────────────────── 内部 ──────────────────────────

    async def ping(self) -> None:
        """保活：AI 端对 ``op=ping`` 回 ``pong``。

        反代会掐空闲的 WS（线上 nginx 配的是 600s），不定期 ping 的话连接会静悄悄地死，
        等到真有候选人来消息时才发现——那时候要现重连，白等一次握手。ping 顺带充当探活：
        断了就能在下一拍重连，而不是攒到第一条真消息上。

        ★ 超时与底层保活同一把尺（``PING_TIMEOUT_SECONDS``），不用普通 op 的 30 秒：AI 端正往下推
        一帧大文件时 pong 排在它后面，30 秒等不到就把连接作废等于把那条指令连同连接一起扔了。
        """
        await self._request("ping", {}, timeout=self._ping_timeout)

    async def _send(self, op: str, data: dict[str, Any], *, ack: str | None = None) -> None:
        if self._ws is None:
            raise AgentWsError("尚未连接")
        # 每一帧都带 id：请求-响应型 op 靠它对号入座，流式 op 靠它把 AI 端回来的
        # ack 关联到本次发送（便于排障）。轮内事件仍按 session_id 归位，不依赖 ack。
        frame: dict[str, Any] = {"v": 1, "id": ack or f"{op}-{next(self._ids)}", "op": op, "data": data}
        try:
            body = json.dumps(frame, ensure_ascii=False)
            await self._ws.send(body)
        except Exception as exc:
            raise AgentWsError(f"发送 {op} 失败: {exc}") from exc
        # 每一帧都留一行。**只记 op 和体积，不记 data**：里面有候选人说的原话、
        # 会话名、简历字段，那是真实 PII，日志文件不该有第二份。
        #
        # ★ 保活的 ping/pong 例外：30 秒一拍（`runner._keepalive_loop`），照记就是每小时
        #   往日志里灌 240 行「什么也没发生」，真正有用的那几行反而被埋掉。它通不通已经
        #   由调用方记账了（连不上会有「AI 端连接不可用」）。
        if op != "ping":
            _log.info("发往 AI 端", op=op, 字节=len(body.encode("utf-8")))

    async def _request(
        self, op: str, data: dict[str, Any], *, timeout: float | None = None,
    ) -> dict[str, Any]:
        """发一个请求-响应型 op，按 ack 对号入座。``timeout`` 缺省用普通 op 的那把尺。"""
        ack = f"{op}-{next(self._ids)}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[ack] = future
        try:
            await self._send(op, data, ack=ack)
            return await asyncio.wait_for(
                future, timeout=self._op_timeout if timeout is None else timeout,
            )
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
                    self._log_frame(frame)
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.warning("读循环结束", error=str(exc))
        finally:
            self._closed.set()
            self._fail_all(AgentWsError("连接已断开"))

    def _log_frame(self, frame: dict[str, Any]) -> None:
        """收到的每一帧留一行——**除了 ``text.delta``**。

        AI 端的回复是一个字一个字流下来的，逐条记等于把实时日志刷成一屏乱码，真正有用的
        那几行（卡片来了没有、这一轮收口了没有）反而被埋掉。所以流式增量只数个数，
        等这一轮 done 的时候一次说清「流了多少段」。

        同样只记事件名和形状，**正文和卡片内容一个字都不记**（真实 PII）。
        """
        ev = frame.get("ev")
        data = frame.get("data")
        data = data if isinstance(data, dict) else {}
        if ev == "text.delta":
            self._deltas += 1
            return
        if ev == "pong":
            return  # 保活的回声，同 `_send` 里 ping 的理由
        if ev == "done" or (ev == "status" and data.get("state") == "done"):
            _log.info("AI 端收口", 流式段=self._deltas, 已用毫秒=data.get("elapsed_ms"))
            self._deltas = 0
            self._state = ""
            return
        if ev == "status":
            # running / working / retrying 在一轮里会重复来很多帧。**只在状态真的变了
            # 的时候记一行**——照帧记的话，一轮几十秒就能把日志刷满同一句话。
            # 阶段那条更细的（`stage.text`）由 runner 单独记，这里只管状态本身。
            state = str(data.get("state") or "-")
            if state != self._state:
                self._state = state
                _log.info("AI 端进度", 状态=state)
            return
        if ev == "card":
            card = data.get("card")
            # kind 在 body 里（顶层没有这个键）；原来读顶层，这行日志永远是 "-"。
            body = card.get("body") if isinstance(card, dict) else None
            kind = body.get("kind") if isinstance(body, dict) else None
            _log.info("AI 端发来卡片", 类型=kind or "-", 状态=str(card.get("status") or "-") if isinstance(card, dict) else "-")
            return
        if ev == "error":
            _log.warning("AI 端发来错误帧", code=data.get("code") or "-")
            return
        _log.info("AI 端发来事件", ev=str(ev or "-"))

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

        # 4) AI 端主动推来的、不属于任何一轮的事件（如监听列表下发）。
        #    回调里出任何差错都只记一条日志——一个坏推送不该把整条连接的读循环掀翻。
        if isinstance(ev, str) and self._on_push is not None:
            try:
                self._on_push(ev, data)
            except Exception as exc:  # noqa: BLE001
                _log.warning("处理推送事件失败", ev=ev, error=str(exc))
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
                return True
            # running / working / retrying 只是进度，不收口。带 stage 的那种要往上报：
            # 055 的 `stage.text` 是「它这会儿在干哪一阶」的**唯一**可显示信号，
            # 丢了的话长任务在界面上就只剩一个干转的圈。
            stage = data.get("stage")
            if isinstance(stage, dict) and turn.on_stage is not None:
                turn.on_stage(stage)
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
        if code == "not_found":
            return SessionGone(f"{code}: {message}")
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
