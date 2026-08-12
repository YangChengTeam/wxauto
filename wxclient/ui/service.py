"""服务控制器：把网关跑在后台线程里，给 UI 一个 启动 / 停止 / 当前状态 的把手。

UI 线程（tkinter）绝不能被 asyncio 阻塞，所以网关整体跑在自己的线程 + 自己的事件循环里。
本模块**不碰任何 GUI**，因此在 Linux 上可测；GUI 只读它的状态、调它的方法。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from enum import Enum

import structlog

from wxclient.gateway.factory import Gateway

_log = structlog.get_logger(__name__)

GatewayBuilder = Callable[[], Gateway]


class ServiceState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


STATE_LABELS = {
    ServiceState.STOPPED: "已停止",
    ServiceState.STARTING: "启动中",
    ServiceState.RUNNING: "运行中",
    ServiceState.STOPPING: "停止中",
    ServiceState.FAILED: "启动失败",
}


class GatewayService:
    """网关的生命周期管理。线程安全：UI 线程和网关线程都会碰它。"""

    def __init__(
        self,
        builder: GatewayBuilder,
        on_state_change: Callable[[ServiceState, str | None], None] | None = None,
    ) -> None:
        self._builder = builder
        self._on_state_change = on_state_change
        self._lock = threading.RLock()
        self._state = ServiceState.STOPPED
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gateway: Gateway | None = None
        self._ready = threading.Event()
        self._stop_requested = False
        self._running_since: float | None = None

    def set_state_listener(
        self, listener: Callable[[ServiceState, str | None], None] | None
    ) -> None:
        """事后接上状态回调——界面要先造出来才有东西可刷。"""
        with self._lock:
            self._on_state_change = listener

    @property
    def state(self) -> ServiceState:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def label(self) -> str:
        state = self.state
        text = STATE_LABELS[state]
        error = self.error
        return f"{text}：{error}" if state is ServiceState.FAILED and error else text

    def start(self) -> None:
        with self._lock:
            if self._state in {ServiceState.STARTING, ServiceState.RUNNING}:
                return
            self._set_state(ServiceState.STARTING, None)
            self._ready.clear()
            self._stop_requested = False
            self._thread = threading.Thread(target=self._run, name="wx-gateway", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        with self._lock:
            thread, loop = self._thread, self._loop
            if thread is None or not thread.is_alive():
                self._set_state(ServiceState.STOPPED, None)
                return
            self._set_state(ServiceState.STOPPING, None)
            self._stop_requested = True
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout)
        with self._lock:
            if not thread.is_alive():
                self._thread = None
                self._loop = None
                self._set_state(ServiceState.STOPPED, None)

    def uptime_seconds(self) -> float | None:
        """服务已经跑了多久；没在跑返回 None。"""
        with self._lock:
            since = self._running_since
        return None if since is None else time.monotonic() - since

    def connection_summary(self) -> tuple[int, int]:
        """(已连上的 AI 端连接数, 接管的微信号数)。没在跑就是 (0, 0)。"""
        with self._lock:
            gateway = self._gateway
        if gateway is None or self._state is not ServiceState.RUNNING:
            return (0, 0)
        try:
            return (gateway.runner.connected_count(), len(gateway.accounts))
        except Exception:  # noqa: BLE001 - 状态展示不该把服务拖下水
            return (0, len(gateway.accounts))

    def connect_error(self) -> str | None:
        """AI 端连不上的原因；连上了或没在跑就是 None。给状态栏区分「在连」与「连不上」。"""
        with self._lock:
            gateway = self._gateway
        if gateway is None or self._state is not ServiceState.RUNNING:
            return None
        try:
            return gateway.runner.connect_error
        except Exception:  # noqa: BLE001 - 同上，只是展示
            return None

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """等到网关真的跑起来（或启动失败）。给启动脚本和测试用。"""
        return self._ready.wait(timeout)

    # -- 网关线程内部 --------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
        try:
            gateway = self._builder()
        except Exception as exc:  # noqa: BLE001 - 配置错/装配失败都要显示到界面上
            _log.error("网关启动失败", error=str(exc))
            self._fail(str(exc))
            loop.close()
            return

        with self._lock:
            self._gateway = gateway
        try:
            gateway.runtime.start()
            self._set_state(ServiceState.RUNNING, None)
            self._ready.set()
            _log.info(
                "网关启动",
                accounts=len(gateway.accounts),
                real_actions=gateway.allow_real_actions,
                ai_ws_url=gateway.settings.ai_ws_url,
            )
            loop.run_until_complete(gateway.runner.run())
        except Exception as exc:  # noqa: BLE001 - 跑挂了要留痕并让界面看得见
            # 主动停服务时 loop.stop() 也会从 run_until_complete 里抛出来，
            # 那不是故障：报成「异常退出」会让托盘图标每次停服都闪一下红。
            if self._stop_requested:
                _log.info("网关按请求停止")
            else:
                _log.error("网关异常退出", error=str(exc))
                self._fail(str(exc))
        finally:
            try:
                loop.run_until_complete(gateway.runner.stop())
            except Exception as exc:  # noqa: BLE001 - 收尾失败不掩盖真正的退出原因
                _log.warning("网关收尾异常", error=str(exc))
            gateway.runtime.stop()
            loop.close()
            self._ready.set()
            with self._lock:
                self._gateway = None
                if self._state not in {ServiceState.FAILED}:
                    self._set_state(ServiceState.STOPPED, None)
            _log.info("网关已停止")

    def _fail(self, error: str) -> None:
        self._set_state(ServiceState.FAILED, error)
        self._ready.set()

    def _set_state(self, state: ServiceState, error: str | None) -> None:
        with self._lock:
            self._state = state
            self._error = error
            if state is ServiceState.RUNNING:
                self._running_since = self._running_since or time.monotonic()
            elif state in {ServiceState.STOPPED, ServiceState.FAILED}:
                self._running_since = None
            callback = self._on_state_change
        if callback is not None:
            try:
                callback(state, error)
            except Exception:  # noqa: BLE001 - UI 抽风不该拖垮服务
                _log.warning("状态回调异常", state=state.value)
