"""服务控制器：把网关跑在后台线程里，给 UI 一个 启动 / 停止 / 当前状态 的把手。

UI 线程（tkinter）绝不能被 asyncio 阻塞，所以网关整体跑在自己的线程 + 自己的事件循环里。
本模块**不碰任何 GUI**，因此在 Linux 上可测；GUI 只读它的状态、调它的方法。
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # 只为类型标注，别在这儿真 import
    from wxclient.gateway.factory import Gateway

# 为什么不在模块顶上 import：`wxclient.gateway.factory` 会连带把 SQLAlchemy、队列、
# 微信驱动整套拉起来，真机实测 **2.7 秒**。这是桌面端「双击了半天不出窗口」的大头——
# 那 2.7 秒全花在窗口出现之前。改成惰性 import 之后，这笔开销挪到网关线程里、跟启动
# 服务一起花，界面先出来。
_log = structlog.get_logger(__name__)

GatewayBuilder = Callable[[], "Gateway"]


class ServiceNotRunning(RuntimeError):
    """要用的能力得在服务跑着时才有（调试会话就是一例）。"""


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


#: 后台状态轮询的间隔。界面本来就一秒刷一次，跟着这个节拍够用。
STATUS_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class GatewayStatus:
    """状态栏要显示的那几样，**整体替换的只读快照**。

    ★ 为什么要有它：这些值原来是界面**在 tkinter 主线程上直接问网关**拿的，其中
    ``pending_backlog`` 一路走到 ``GatewayStore.count_pending_inbound()``——那是
    **开一条 SQLite 连接 + PRAGMA + COUNT**，一秒一次，跑在主线程上。

    2026-08-14 真机实测（3 条线程同时写库，模拟正在收发微信消息）：这一句中位数
    **15 ms**、p90 **31 ms**、最慢 **46 ms**；库闲着时是 0 ms。所以它**不足以**解释
    「卡死」，但确实是每秒一次的顿挫，而且是主线程上唯一一处文件 I/O——
    界面线程本就不该碰库。现在这些活全挪到后台轮询线程，界面只读这份快照：
    不拿锁、不碰库、不进网关。真正的卡顿另有来源，靠 `window.py` 的卡顿看门狗取证。
    """

    connected: int = 0
    accounts: int = 0
    backlog: int = 0
    watched: tuple[str, ...] = field(default_factory=tuple)
    connect_error: str | None = None
    #: 离下一趟读消息还有几秒。None = 这会儿没有排期（正在读，或不在工作时间窗）。
    next_read_seconds: float | None = None
    #: 在不在工作时间窗之外。窗外一个微信动作都不做，所以也就没有「下一趟」。
    quiet_hours: bool = False
    #: 091 · 每个候选人现在由谁跟：会话名 → `human_takeover` / `ai_managed`。
    #: ★ **不在这个表里 = 不知道**（本机没记录、同步名单也还没带回来），不是「确定是托管」。
    #:   界面据此把按钮画成「接管」但不谎报当前档。
    candidate_modes: Mapping[str, str] = field(default_factory=dict)


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
        # ★ 状态快照（(state, error) 元组，整体替换）。界面每秒读好几次状态，**读它一律不拿锁**：
        #   只要主线程会为这把锁等，任何一次「持锁的慢操作」都会变成窗口不重绘 = 卡死。
        #   单次赋值在 CPython 下是原子的，读到的永远是某一时刻的完整快照。
        self._snapshot: tuple[ServiceState, str | None] = (ServiceState.STOPPED, None)
        # ★ 状态回调改成**异步单线程派发**（见 _notifier）。回调里要动托盘/界面，
        #   在调用者线程上直接跑就等于把 UI 的毛病传染给网关线程。
        self._events: queue.Queue[tuple[ServiceState, str | None]] = queue.Queue()
        self._notifier = threading.Thread(target=self._pump_events, name="wx-svc-notify", daemon=True)
        self._notifier.start()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gateway: Gateway | None = None
        self._ready = threading.Event()
        self._stop_requested = False
        self._running_since: float | None = None
        self._restarting = False
        # ★ 状态快照 + 专职轮询线程：界面读快照，真正去问网关（可能查库、可能等锁）的
        #   活全在这条线程上。它卡多久都只卡自己，主线程照旧每秒重绘。
        self._command_listener: Callable[[dict], None] | None = None
        self._status = GatewayStatus()
        #: 上一次报过的积压批数。只在**变了**的时候往实时日志写一行——轮询是一秒一次，
        #: 每次都写就是每秒一行刷屏。None = 还没报过（服务没跑起来时不报）。
        self._logged_backlog: int | None = None
        self._poller = threading.Thread(target=self._poll_status, name="wx-svc-status", daemon=True)
        self._poller.start()

    def set_state_listener(
        self, listener: Callable[[ServiceState, str | None], None] | None
    ) -> None:
        """事后接上状态回调——界面要先造出来才有东西可刷。"""
        with self._lock:
            self._on_state_change = listener

    @property
    def state(self) -> ServiceState:
        return self._snapshot[0]  # 无锁：见 __init__ 里 _snapshot 的说明

    @property
    def error(self) -> str | None:
        return self._snapshot[1]

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

    # ── 异步版本：给界面用 ────────────────────────────────────────
    #
    # ``stop()`` 里那个 ``thread.join(timeout=15)`` 谁调谁被钉住 15 秒。tkinter 主线程
    # 被钉住 = 窗口连重绘都不做，看起来就是死了；托盘线程被钉住 = 菜单点不动。
    # 所以凡是从界面（主窗口 / 托盘 / 参数页）来的停机与重启，一律走下面这两个。

    def stop_async(
        self, *, on_done: Callable[[], None] | None = None, timeout: float = 15.0
    ) -> threading.Thread:
        """后台停机，立刻返回。``on_done`` 在后台线程上回调（要动界面自己 after 回去）。"""
        return self._in_background("wx-gateway-stop", lambda: self.stop(timeout=timeout), on_done)

    def restart_async(
        self,
        *,
        before_start: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
        timeout: float = 15.0,
    ) -> threading.Thread | None:
        """后台「停 → （改参数）→ 起」。正在重启时再点一次直接忽略，返回 ``None``。

        ``before_start`` 跑在停机之后、启动之前——参数要在这一刻才灌进环境变量，
        否则新参数会被还没停干净的旧实例读走。
        """
        with self._lock:
            if self._restarting:
                _log.info("已经在重启了，忽略这次请求")
                return None
            self._restarting = True

        def work() -> None:
            try:
                self.stop(timeout=timeout)
                if before_start is not None:
                    before_start()
                self.start()
            finally:
                with self._lock:
                    self._restarting = False

        return self._in_background("wx-gateway-restart", work, on_done)

    @property
    def restarting(self) -> bool:
        with self._lock:
            return self._restarting

    def _in_background(
        self, name: str, work: Callable[[], None], on_done: Callable[[], None] | None
    ) -> threading.Thread:
        def run() -> None:
            try:
                work()
            except Exception as exc:  # noqa: BLE001 - 后台线程里抛出去没人接，只会静默消失
                _log.error("后台操作失败", op=name, error=str(exc))
            finally:
                if on_done is not None:
                    try:
                        on_done()
                    except Exception as exc:  # noqa: BLE001 - 收尾回调失败不该盖住上面的原因
                        _log.warning("后台操作收尾回调失败", op=name, error=str(exc))

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread

    # ── 调试会话（`ui/debug_page.py`）─────────────────────────────

    def debug_turn(
        self,
        text: str,
        *,
        on_delta: Callable[[str], None] | None = None,
        on_stage: Callable[[dict], None] | None = None,
    ) -> Future[Any]:
        """把一句话发给 AI 端，返回一个**还没完成的 Future**。

        ★ 不阻塞、也不回调 UI：调用方（界面）自己在心跳里 `future.done()` 问一句就行。
        这是本仓一贯的跨线程办法——后台线程只放值，tk 线程只读值，谁都不去动对方的东西
        （`window._probe_wechat` 是同一个路数）。回调进来还得再解决「回调跑在网关线程上、
        却要动控件」，那正是 2026-08-13 卡死的形状。

        `on_delta` 会在**网关线程**上被调用（流式增量），所以界面那头传进来的必须是
        线程安全的东西（一个 `queue.Queue.put` 就够），绝不能直接改控件。
        """
        with self._lock:
            loop, gateway = self._loop, self._gateway
        if loop is None or gateway is None or self.state is not ServiceState.RUNNING:
            raise ServiceNotRunning("服务没在运行，先在左边把它起起来")
        return asyncio.run_coroutine_threadsafe(
            gateway.runner.debug_turn(text, on_delta=on_delta, on_stage=on_stage), loop
        )

    def sync_watched(self) -> Future[Any]:
        """请 AI 端把名单重发一份，返回一个还没完成的 Future（界面在心跳里问它）。

        和 `remove_watched` 一样是「意图发过去、结果推回来」：新名单走 `wechat.watchlist`
        推送落地，这个 Future 的值只是回执（``{watching, blocked}``）。
        """
        with self._lock:
            loop, gateway = self._loop, self._gateway
        if loop is None or gateway is None or self.state is not ServiceState.RUNNING:
            raise ServiceNotRunning("服务没在运行，同步不了名单")
        return asyncio.run_coroutine_threadsafe(gateway.runner.sync_watched(), loop)

    def remove_watched(self, name: str) -> Future[Any]:
        """把一个会话移出自动回复名单，返回一个还没完成的 Future（界面在心跳里问它）。

        删在 AI 端（名单权威在那儿）；删完新名单会被推回来，本机那份跟着变。
        """
        with self._lock:
            loop, gateway = self._loop, self._gateway
        if loop is None or gateway is None or self.state is not ServiceState.RUNNING:
            raise ServiceNotRunning("服务没在运行，名单改不了")
        return asyncio.run_coroutine_threadsafe(gateway.runner.remove_watched(name), loop)

    def set_candidate_mode(self, session_name: str, mode: str, *, reason: str = "") -> Future[Any]:
        """按下某个候选人后面那个小按钮（091）。返回还没完成的 Future（界面在心跳里问它）。

        ★ 落本机这一步在网关线程上同步做完，**不等网络**：接管的意思是「现在立刻别再发给他了」。
          Future 的值是 `{mode, synced, note}`——`synced=False` 时界面标一句「未同步」，
          **但那一行照旧显示成接管中**（本机才是即时执行的权威）。
        """
        with self._lock:
            loop, gateway = self._loop, self._gateway
        if loop is None or gateway is None or self.state is not ServiceState.RUNNING:
            raise ServiceNotRunning("服务没在运行，接管状态改不了")
        return asyncio.run_coroutine_threadsafe(
            gateway.runner.set_candidate_mode(session_name, mode, reason=reason), loop
        )

    def interrupt_debug(self) -> None:
        """停掉调试会话正在跑的那一轮。丢到网关线程上做，界面不等它。"""
        with self._lock:
            loop, gateway = self._loop, self._gateway
        if loop is None or gateway is None:
            return
        asyncio.run_coroutine_threadsafe(gateway.runner.interrupt_debug(), loop)

    def set_command_listener(self, listener: Callable[[dict], None] | None) -> None:
        """登记指令回执的听众。**回调跑在网关线程上**——界面那头只准往队列里放东西。

        服务没在跑时先记着：等它起来（`_run` 里装配完 runner）再挂上去，
        不然「先打开调试会话页、后启动服务」这条路径就永远收不到回执。
        """
        with self._lock:
            self._command_listener = listener
            gateway = self._gateway
        if gateway is not None:
            gateway.runner.set_command_listener(listener)

    def uptime_seconds(self) -> float | None:
        """服务已经跑了多久；没在跑返回 None。

        不拿锁：界面每秒读它，任何一次「为锁等待」都会变成窗口不重绘。单次读浮点是原子的。
        """
        since = self._running_since
        return None if since is None else time.monotonic() - since

    # ── 状态读法：一律读后台轮询攒下的快照，**绝不在调用者线程上问网关** ──────
    #
    # 调这几个的是 tkinter 主线程（每秒一次）。真去问网关意味着可能查 SQLite、可能等
    # 写锁（`sqlite3.connect` 默认等 5 秒），主线程被钉住就是「界面卡死」。见 GatewayStatus。

    def status(self) -> GatewayStatus:
        """当前状态快照。无锁、无 I/O，随便从哪个线程读。"""
        return self._status

    def connection_summary(self) -> tuple[int, int]:
        """(已连上的 AI 端连接数, 接管的微信号数)。没在跑就是 (0, 0)。"""
        snapshot = self._status
        return (snapshot.connected, snapshot.accounts)

    def pending_backlog(self) -> int:
        """入站待处理队列积压批次数；没在跑就是 0。"""
        return self._status.backlog

    def watched_peers(self) -> list[str]:
        """当前在盯哪些会话（AI 端推下来的名单）。没在跑就是空。

        名单只在日志里闪一行「只管=2」，界面上看不到——于是「为什么这个人没被回」
        永远要去翻日志（用户 2026-08-14 反馈：名单应该在 Windows 这端看得到）。
        """
        return list(self._status.watched)

    def connect_error(self) -> str | None:
        """AI 端连不上的原因；连上了或没在跑就是 None。给状态栏区分「在连」与「连不上」。"""
        return self._status.connect_error

    def refresh_status_now(self) -> GatewayStatus:
        """立刻同步取一次（会查库、可能等锁）。**别在 UI 线程上调**，测试和脚本用。"""
        self._status = self._read_status()
        return self._status

    def _poll_status(self) -> None:
        """专职问网关的线程。它可以慢、可以被库锁住，界面不跟着一起卡。"""
        while True:
            try:
                self._status = self._read_status()
                self._report_backlog(self._status.backlog)
            except Exception:  # noqa: BLE001 - 展示用的东西不该把这条线程弄死
                self._status = GatewayStatus()
            time.sleep(STATUS_POLL_SECONDS)

    def _report_backlog(self, backlog: int) -> None:
        """积压批数变了就往实时日志写一行，说清楚它到底是什么意思。

        ★ 这件事原来只在左边状态栏显示成「已连接 · 欠 N 批」（用户 2026-08-21：
        「欠这个意思也不懂」）。「欠」是记账口径，不是人话，而且状态栏那一格只放得下
        几个字，说不清「读到了没回出去」和「消息丢了」的区别。所以搬到实时日志：
        那里有整行的地方，能把**发生了什么、要不要管、会不会重复**一次讲完。

        **只在变化时报**：轮询一秒一次，每次都报就是每秒一行刷屏。
        服务没在跑时不报——那时候 backlog 恒为 0，停服务会白报一句「清空了」。
        """
        if self._state is not ServiceState.RUNNING:
            self._logged_backlog = None
            return
        if backlog == self._logged_backlog:
            return
        previous = self._logged_backlog
        self._logged_backlog = backlog
        if previous is None and backlog == 0:
            return          # 刚起来就没有积压，没什么可说的
        _log.info("gateway.backlog.changed", backlog=backlog, previous=previous or 0)

    def _read_status(self) -> GatewayStatus:
        """真去问一遍网关。每一项各自兜底——一项坏了不该把整排状态清空。"""
        with self._lock:
            gateway = self._gateway
        if gateway is None or self._state is not ServiceState.RUNNING:
            return GatewayStatus()

        def read(what: Callable[[], object], fallback: object) -> object:
            try:
                return what()
            except Exception:  # noqa: BLE001 - 状态展示不该把服务拖下水
                return fallback

        accounts = read(lambda: len(gateway.accounts), 0)
        # 091：本机记着档位的那几个人。读不到就是空表——「不知道」比猜一个安全。
        rows = read(lambda: gateway.runner.candidate_modes(), {})
        modes = {
            str(name): str(getattr(row, "mode", "") or "")
            for name, row in (rows.items() if isinstance(rows, dict) else ())
        } if isinstance(rows, dict) else {}
        return GatewayStatus(
            # 一律裹成 lambda：直接写 `gateway.runner.xxx` 会在进 read() 之前就求值，
            # runner 缺席时抛出来的 AttributeError 会绕过这里的兜底、把整排状态清空。
            connected=int(read(lambda: gateway.runner.connected_count(), 0)),  # type: ignore[arg-type]
            accounts=int(accounts),  # type: ignore[arg-type]
            backlog=int(read(lambda: gateway.runner.pending_backlog(), 0)),  # type: ignore[arg-type]
            watched=tuple(read(lambda: gateway.runner.watched_peers(), ())),  # type: ignore[arg-type]
            connect_error=read(lambda: gateway.runner.connect_error, None),  # type: ignore[arg-type]
            next_read_seconds=read(lambda: gateway.runner.seconds_to_next_read(), None),  # type: ignore[arg-type]
            quiet_hours=bool(read(lambda: gateway.runner.in_quiet_hours(), False)),
            candidate_modes=modes,
        )

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
            listener = self._command_listener
        # 页面可能在服务起来之前就登记过听众（见 set_command_listener）
        if listener is not None:
            gateway.runner.set_command_listener(listener)
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
        """改状态并**异步**通知。绝不在这里直接跑回调。

        ★ 2026-08-13 真机：「保存并重启」点下去之后停机成功、却再也没有启动，界面全程卡死。
        病根就在这一句原来是**同步调回调**的：`start()`/`stop()` 是在 `with self._lock:`
        里调 `_set_state` 的（RLock，外层那圈还持着），于是回调跑在**持锁状态**下；
        而回调里有一步是 `tray.refresh()`——从非托盘线程打 Win32 托盘接口，一旦对面没就绪
        就干等。结果两头一起完蛋：
          · 重启线程卡在回调里，永远走不到 `self.start()` → 日志停在「网关已停止」；
          · 主线程每秒要读一次状态，被这把锁挡住 → 窗口不重绘 = 用户看到的「卡死」。
        现在锁内只改字段，通知丢给单独一条线程发，谁都不等谁。
        """
        with self._lock:
            self._state = state
            self._error = error
            if state is ServiceState.RUNNING:
                self._running_since = self._running_since or time.monotonic()
            elif state in {ServiceState.STOPPED, ServiceState.FAILED}:
                self._running_since = None
        self._snapshot = (state, error)  # 无锁读的那份，锁外更新
        self._events.put((state, error))

    def _pump_events(self) -> None:
        """专职发状态通知的线程。回调再慢再卡，卡的也只是它自己。"""
        while True:
            state, error = self._events.get()
            callback = self._on_state_change
            if callback is None:
                continue
            try:
                callback(state, error)
            except Exception:  # noqa: BLE001 - UI 抽风不该拖垮服务
                _log.warning("状态回调异常", state=state.value)
