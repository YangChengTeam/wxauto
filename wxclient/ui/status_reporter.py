"""状态上报：这台机器在不在线、本机微信连没连上。

契约 §7/§8 的形状很特别，值得记住：**没有心跳**。客户端只在状态**变化**时 PUT 一次，
服务端不按时间把 online 自动改成 offline。这带来两条硬要求——

1. **正常退出前必须先报 offline**，再结束进程。漏了这一步，企业端会一直显示这台机器在线，
   而它其实早关机了（契约 §7「无心跳时的状态边界」明写了这一点）。
2. **相同状态不重复上报**：微信探测是 15 秒一拍，照拍上报等于每天几千条无意义的审计。

所有请求都在这条后台线程上发。界面线程一律只往队列里丢——上报是网络调用，最长 8 秒，
放在 tk 线程上就是「点一下卡一下」。唯一的例外是退出时那次 offline：那一下必须**同步等**，
不然进程先没了，请求也就没发出去（但也只等几秒，退出不能被网络卡住）。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Any

import structlog

from . import uia_status
from .client_api import ApiError, ClientApi, new_event_id
from .uia_status import UiaState

_log = structlog.get_logger(__name__)

ONLINE = "online"
OFFLINE = "offline"
ERROR = "error"

#: 退出时那次 offline 最多等多久。等不到就走人——「关不掉」比「状态没同步」严重得多。
SHUTDOWN_TIMEOUT_SECONDS = 3.0


class StatusReporter:
    """把状态变化送到业务端。线程安全：界面线程、探测线程、网关线程都会调它。"""

    def __init__(self, api: ClientApi, token: Callable[[], str], *,
                 client_version: str = "",
                 capability_check: Callable[[dict[str, Any]], str] | None = None,
                 on_revoked: Callable[[], None] | None = None) -> None:
        self._api = api
        self._token = token
        self._client_version = client_version
        self._capability_check = capability_check
        self._on_revoked = on_revoked
        self._lock = threading.Lock()
        self._client_status = ""
        self._wechat: UiaState | None = None
        #: 还没送到的那条状态用的幂等键：``{"client": (这条状态的指纹, eventId)}``。
        #: 见 `_event_id`——重试必须**沿用**同一个 eventId。
        self._events: dict[str, tuple[Any, str]] = {}
        self._queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._worker = threading.Thread(target=self._pump, name="wx-ui-status", daemon=True)
        self._worker.start()

    # ── 客户端状态 ───────────────────────────────────────────────

    def client_online(self, reason: str = "") -> None:
        """本机恢复可用。**登录成功后不用调**——服务端在登录时就把它置成 online 了（契约 §7）。"""
        self._client(ONLINE, reason)

    def client_error(self, reason: str) -> None:
        """有会话但跑不动了（网关起不来、UIA 初始化失败）。"""
        self._client(ERROR, reason)

    def client_offline(self, reason: str = "client_shutdown") -> None:
        self._client(OFFLINE, reason)

    def session_changed(self) -> None:
        """换了一个会话（刚登录 / 刚退出）：忘掉去重记录。

        ★ 去重是「这个状态我跟**服务端**说过了」。换了会话，那句话就作废了——不清的话，
        重新登录后微信状态没变，我们会以为报过了，而新会话那边一直是空的。
        """
        with self._lock:
            self._client_status = ""
            self._wechat = None
            # 幂等键也一起扔掉：eventId 在服务端是全局唯一的，把上一个会话没送出去的键
            # 拿到新会话里用，只会换来一个 409 EVENT_ID_CONFLICT。
            self._events.clear()

    def shutdown(self, reason: str = "client_shutdown") -> None:
        """退出前把 offline 送出去，**同步等**（有上限）。

        退出登录也走它：offline 必须在撤销会话**之前**送到（见 `ui/__main__.on_logout`）。
        """
        with self._lock:
            if self._client_status == OFFLINE or not self._token():
                return
            self._client_status = OFFLINE
            event_id = self._event_id("client", OFFLINE)
        done = threading.Event()

        def work() -> None:
            try:
                self._put_client(OFFLINE, reason, event_id)
            finally:
                done.set()

        threading.Thread(target=work, name="wx-ui-status-bye", daemon=True).start()
        if not done.wait(SHUTDOWN_TIMEOUT_SECONDS):
            _log.warning("退出前上报离线超时，先走了")

    def _event_id(self, kind: str, fingerprint: Any) -> str:
        """这条状态的幂等键。**同一条状态重试时沿用同一个 id**（契约 §12.3）。

        ★ 每次重发都现生成一个新 UUID 的话，幂等键就白设了：请求送到了、回包在路上丢了，
        我们判成失败重发一次，服务端看到的是两个不同的事件，于是审计里同一件事记两遍。
        状态**变了**才换新 id——那本来就是另一件事。
        """
        previous, event_id = self._events.get(kind, (None, ""))
        if previous != fingerprint or not event_id:
            event_id = new_event_id()
            self._events[kind] = (fingerprint, event_id)
        return event_id

    def _client(self, status: str, reason: str) -> None:
        with self._lock:
            if status == self._client_status or not self._token():
                return
            self._client_status = status
            event_id = self._event_id("client", status)
        self._submit(lambda: self._put_client(status, reason, event_id))

    def _put_client(self, status: str, reason: str, event_id: str = "") -> None:
        try:
            self._api.put_client_status(self._token(), status,
                                        client_version=self._client_version,
                                        reason=reason, event_id=event_id)
            with self._lock:
                self._events.pop("client", None)  # 送到了，这个键的使命结束
            _log.info("已上报客户端状态", status=status, reason=reason or None)
        except ApiError as exc:
            self._failed("客户端状态", exc, lambda: self._reset_client(status))

    def _reset_client(self, status: str) -> None:
        """上报失败就忘掉这次记录，下一次同样的状态还得再报一遍。

        不这么做的话，一次网络抖动会让这个状态**永远**报不出去——去重表说「报过了」，
        而服务端那边压根没收到。
        """
        with self._lock:
            if self._client_status == status:
                self._client_status = ""

    # ── 微信 UIA 状态 ───────────────────────────────────────────

    def wechat_probe(self, probe: dict[str, Any]) -> None:
        if not self._token():
            # ★ 还没登录：这一拍的结论没人要（`_wechat_state` 到最后也会丢掉）。
            # 但下面两件事都不便宜——认微信账号要枚举进程打开的文件（真机 2.4 秒），
            # 算能力表要把网关那套重型模块 import 进来。它们跑在后台线程上，照样把
            # 主线程的 GIL 抢走：真机上就是一条「界面卡了一下 795ms」，而当时人还停在
            # 登录页，什么都还没开始。**没登录就一件都别干。**
            return
        state = uia_status.classify(probe)
        if state is None:
            return
        if state.status == uia_status.CONNECTED and self._capability_check is not None:
            # 连上了还得说「能干哪几件事」：只有 passed 才让企业端进入「运行中」（契约 §8）。
            try:
                state = state.with_capability(self._capability_check(probe))
            except Exception as exc:  # noqa: BLE001 - 算不出能力表不该拖垮状态上报
                _log.warning("能力检测失败", error=str(exc)[:120])
        self._wechat_state(state)

    def wechat_probe_failed(self, error: BaseException) -> None:
        # 这条不用像上面那样先挡一道：`probe_failed` 只是拼一个 UiaState，
        # 没登录的判断由 `_wechat_state` 一处统一管。
        self._wechat_state(uia_status.probe_failed(error))

    def _wechat_state(self, state: UiaState) -> None:
        with self._lock:
            if not self._token():
                return
            state = uia_status.transition(self._wechat, state)
            if state.same_as(self._wechat):
                return  # 相同状态不重复上报（契约 §8）
            previous, self._wechat = self._wechat, state
            event_id = self._event_id("wechat", state.identity)
        self._submit(lambda: self._put_wechat(state, previous, event_id))

    def _put_wechat(self, state: UiaState, previous: UiaState | None,
                    event_id: str = "") -> None:
        try:
            self._api.put_wechat_uia_status(self._token(), state.status,
                                            instance=state.instance,
                                            failure=state.failure,
                                            capability_status=state.capability_status,
                                            event_id=event_id)
            with self._lock:
                self._events.pop("wechat", None)
            _log.info("已上报微信连接状态", status=state.status,
                      failure=state.failure_code or None,
                      capability=state.capability_status or None)
        except ApiError as exc:
            self._failed("微信连接状态", exc, lambda: self._reset_wechat(state, previous))

    def _reset_wechat(self, state: UiaState, previous: UiaState | None) -> None:
        with self._lock:
            if self._wechat is state:
                self._wechat = previous

    # ── 公共 ─────────────────────────────────────────────────────

    @staticmethod
    def _permanent(exc: ApiError) -> bool:
        """重发同样的内容也不会变的失败——**别重试**。

        ★ 这条判断留在上报这一层，不放进 `ApiError`：同样是 409，在别的接口上意思正相反——
        登录的 `ACTIVE_CLIENT_SESSION_EXISTS` 是「问过人再带 replace 重来」，换票的
        `CLIENT_NOT_ONLINE` 是「等状态同步就自己好」，两个都该重试。按状态码一刀切写进
        共用的错误类型，第一个照着用的调用方就会拿到错答案。

        状态上报这条路上只有三种：内容不合格（400）、幂等键撞了、微信被别的分身占着
        （契约表里写明 `retryable=false`）。判成瞬时错误的代价是每 15 秒重发一次、
        日志刷出几百条一模一样的告警，真正的原因反而被埋掉。
        """
        return exc.status == 400 or exc.code in {
            "VALIDATION_ERROR", "EVENT_ID_CONFLICT", "WECHAT_UIA_ACCOUNT_IN_USE",
        }

    def _failed(self, what: str, exc: ApiError, rollback: Callable[[], None]) -> None:
        if exc.revoked:
            rollback()
            _log.warning("客户端会话已被撤销")
            if self._on_revoked is not None:
                self._on_revoked()
            return
        if self._permanent(exc):
            # ★ 重发同样的内容也不会变：**不回滚**。回滚等于每 15 秒把同一份内容再发一遍，
            # 日志里刷出几百条一模一样的告警，真正的原因反而被埋掉（2026-08-15 真机上出过）。
            # details 里是服务端指出的字段，必须留下来——不然只剩一句「填的内容格式不对」。
            _log.warning(f"上报{what}被拒：重发也不会变，先不重试", error=str(exc)[:120],
                         code=exc.code or None, details=exc.details or None)
            return
        rollback()
        # 上报失败不是致命错：它只影响企业端那一格显示，不影响这台机器继续干活。
        _log.warning(f"上报{what}失败", error=str(exc)[:120], code=exc.code or None,
                     details=exc.details or None)

    def _submit(self, job: Callable[[], None]) -> None:
        self._queue.put(job)

    def _pump(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job()
            except Exception as exc:  # noqa: BLE001 - 这条线程死了就再也不上报了
                _log.warning("上报线程异常", error=str(exc)[:120])

    def stop(self) -> None:
        self._queue.put(None)
