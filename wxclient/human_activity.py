"""有人在用这台电脑，微信自动操作就停手：鼠标一动、键盘一按就暂停，停手 N 秒后自动恢复。

移植自映身（`lxz_yingshen/scripts/runtime/human_activity.py`）：全局低级钩子
（``WH_KEYBOARD_LL`` / ``WH_MOUSE_LL``）看**真人**输入——带 ``INJECTED`` 标记的（pywinauto
自己点的、粘贴的）不算，否则自动操作一动就把自己暂停了。按着不放的键 / 鼠标键一直算「在用」。

**钩子放独立子进程，不放客户端进程里。** 低级钩子的回调是在装钩子那个线程上同步跑的，全系统
每动一下鼠标都要回一次 Python；客户端进程里界面、asyncio、UIA 都在抢 GIL（0.1.131 就是为这个
把读库挪出去的），回调一晚，**全机的鼠标都跟着卡**，超过 LowLevelHooksTimeout 还会被 Windows
悄悄摘掉。子进程里什么别的都不干，回调只记一笔就返回。

协议：子进程 stdout 一行一个，``ready`` / ``error <原因>`` 之后，每次有真人输入回 ``h <按住几个键>``
（光是移动 0.2 秒内只回一次；按下/松开立刻回）；按着的键核实已松开回 ``r <按住几个键>``（不算动静）。只回「有动静」和按住的个数，**不回按的是哪个键**。
父进程关 stdin 子进程就退。

暂停只管**微信 UI 操作**（队列里的任务，`queue.worker` 取任务前问 `busy()`；加好友 / 朋友圈每步
之间 `wait_idle()`）。读本地库不碰界面，照常进行、照常打日志。钩子装不上（非 Windows、权限）就当
没这个功能，**照常执行**并打一条警告——不能因为它让客户端整个不干活。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable

import structlog

_log = structlog.get_logger(__name__)

WATCH_FLAG = "--input-watch"

#: 默认停手多久算「人走开了」（秒）。映身用 2 秒；人看聊天、想一句话时手常常停两三秒，
#: 2 秒就抢回去太急。配置中心可改（`WXCLIENT_GATEWAY_HUMAN_IDLE_SECONDS`），0＝不暂停。
DEFAULT_IDLE_SECONDS = 5.0

#: 子进程同样的状态最快多久回一次（秒）。鼠标一秒上百个事件，没必要每个都过管道。
_EMIT_EVERY_SECONDS = 0.2

#: 等子进程装好钩子最多多久（秒）。
_READY_TIMEOUT_SECONDS = 10.0


# ───────────────────────────── 子进程这一侧 ─────────────────────────────

def serve() -> int:
    """子进程入口：装键盘、鼠标两个低级钩子，有真人输入就往 stdout 报一行，stdin 关了就退。"""
    import ctypes as c
    from ctypes import wintypes as w

    # 冻结包是无控制台的窗口程序，sys.stdin/stdout 可能是 None，直接拿管道的文件描述符（同 `wechat_db.worker`）。
    proto_in = sys.stdin.buffer if sys.stdin else os.fdopen(0, "rb")
    out = sys.stdout.buffer if sys.stdout else os.fdopen(1, "wb")
    lock = threading.Lock()
    held: set[tuple[str, int]] = set()
    dirty = threading.Event()
    state = {"changed": False}

    def write(line: str) -> None:
        out.write(line.encode("ascii") + b"\n")
        out.flush()

    def note(kind: str, key: int, down: bool | None) -> None:
        with lock:
            before = len(held)
            if down is True:
                held.add((kind, key))
            elif down is False:
                held.discard((kind, key))
            if len(held) != before:
                state["changed"] = True
        dirty.set()

    api = c.WinDLL("user32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    callback_type = c.WINFUNCTYPE(c.c_ssize_t, c.c_int, w.WPARAM, w.LPARAM)

    class Keyboard(c.Structure):
        _fields_ = [("vk", w.DWORD), ("scan", w.DWORD), ("flags", w.DWORD),
                    ("time", w.DWORD), ("extra", c.c_size_t)]

    class Mouse(c.Structure):
        _fields_ = [("point", w.POINT), ("data", w.DWORD), ("flags", w.DWORD),
                    ("time", w.DWORD), ("extra", c.c_size_t)]

    api.SetWindowsHookExW.argtypes = [c.c_int, callback_type, w.HINSTANCE, w.DWORD]
    api.SetWindowsHookExW.restype = w.HANDLE
    api.CallNextHookEx.argtypes = [w.HANDLE, c.c_int, w.WPARAM, w.LPARAM]
    api.CallNextHookEx.restype = c.c_ssize_t
    api.UnhookWindowsHookEx.argtypes = [w.HANDLE]
    api.GetMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
    api.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
    kernel.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel.GetModuleHandleW.restype = w.HMODULE

    def keyboard(code, message, pointer):
        if code >= 0:
            event = c.cast(pointer, c.POINTER(Keyboard)).contents
            if not event.flags & 0x10:  # LLKHF_INJECTED：程序发的键不算人
                note("keyboard", event.vk, message in (0x100, 0x104))
        return api.CallNextHookEx(None, code, message, pointer)

    buttons = {0x201: (1, True), 0x202: (1, False), 0x204: (2, True), 0x205: (2, False),
               0x207: (4, True), 0x208: (4, False)}

    def mouse(code, message, pointer):
        if code >= 0:
            event = c.cast(pointer, c.POINTER(Mouse)).contents
            if not event.flags & 1:  # LLMHF_INJECTED：pywinauto 点的不算人
                if message in (0x20B, 0x20C):  # 侧键：data 高位区分 X1/X2
                    key, down = (5 if event.data >> 16 == 1 else 6), message == 0x20B
                else:
                    key, down = buttons.get(message, (0, None))
                note("button", key, down)  # 移动、滚轮：down=None，只算一次动静
        return api.CallNextHookEx(None, code, message, pointer)

    callbacks = [callback_type(keyboard), callback_type(mouse)]  # 摘钩子之前不能被回收
    hooks = []
    try:
        for kind, callback in zip((13, 14), callbacks):
            hook = api.SetWindowsHookExW(kind, callback, kernel.GetModuleHandleW(None), 0)
            if not hook:
                raise c.WinError(c.get_last_error())
            hooks.append(hook)
    except Exception as exc:  # noqa: BLE001 —— 装不上如实回一行，父进程照常干活
        write(f"error {type(exc).__name__}")
        return 1
    write("ready")

    thread_id = kernel.GetCurrentThreadId()
    stopped = threading.Event()

    def watch_stdin() -> None:
        proto_in.read()  # 父进程关管道 / 退出 → EOF
        stopped.set()
        dirty.set()
        api.PostThreadMessageW(thread_id, 0x0012, 0, 0)  # WM_QUIT，叫醒消息循环

    def emit() -> None:
        # 回调里只记一笔就返回（钩子回调慢了全机鼠标都卡）；写管道在这个线程里做。
        while True:
            with lock:
                holding = bool(held)
            if not dirty.wait(1.0 if holding else None):
                # 有键按着、一秒没新动静：问一下系统它们是不是真还按着。松开那下可能没被钩子看到
                # （Win+L 锁屏后松手是在安全桌面上），不核的话会永远当它按着、永远暂停。
                # 报 ``r``（只改按住个数），不报 ``h``：这不是一次真人动静，不该重新计时。
                with lock:
                    before = len(held)
                    for item in [i for i in held if not api.GetAsyncKeyState(i[1]) & 0x8000]:
                        held.discard(item)
                    count = len(held)
                if count != before:
                    try:
                        write(f"r {count}")
                    except OSError:
                        stopped.set()
                        api.PostThreadMessageW(thread_id, 0x0012, 0, 0)
                        return
                continue
            if stopped.is_set():
                return
            dirty.clear()
            with lock:
                changed, state["changed"] = state["changed"], False
                count = len(held)
            try:
                write(f"h {count}")
            except OSError:
                stopped.set()
                api.PostThreadMessageW(thread_id, 0x0012, 0, 0)
                return
            if not changed:
                stopped.wait(_EMIT_EVERY_SECONDS)  # 光是移动：限流；这段时间里的按下/松开下一圈立刻报

    threading.Thread(target=watch_stdin, name="input-watch-stdin", daemon=True).start()
    threading.Thread(target=emit, name="input-watch-emit", daemon=True).start()
    message = w.MSG()
    while not stopped.is_set() and api.GetMessageW(c.byref(message), None, 0, 0) > 0:
        api.TranslateMessage(c.byref(message))
        api.DispatchMessageW(c.byref(message))
    for hook in hooks:
        api.UnhookWindowsHookEx(hook)
    return 0


# ───────────────────────────── 父进程这一侧 ─────────────────────────────

class HumanActivity:
    """「刚才有没有人在用电脑」：最近一次真人输入离现在不到 ``idle_seconds``，或有键按着没放。"""

    def __init__(self, idle_seconds: float = DEFAULT_IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.idle_seconds = float(idle_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._last: float | None = None  # 还没见过真人输入 = 不暂停
        self._held = 0
        self._announced = False
        self._process: subprocess.Popen | None = None

    def note(self, held: int) -> None:
        """子进程报来一次真人输入（按住 ``held`` 个键）。"""
        with self._lock:
            self._last = self._clock()
            self._held = max(0, int(held))

    def released(self, held: int) -> None:
        """子进程核过：按着的键其实已经松开了（只改个数，不算一次动静）。"""
        with self._lock:
            self._held = max(0, int(held))

    def busy(self) -> bool:
        """现在该不该停手。状态一翻转就打一条日志（只在有任务等着时才会被问到，见 `queue.worker`）。"""
        with self._lock:
            idle = None if self._last is None else self._clock() - self._last
            paused = self._held > 0 or (idle is not None and idle < self.idle_seconds)
            flipped = paused != self._announced
            self._announced = paused
        if flipped and paused:
            _log.info("检测到有人在用鼠标键盘，微信自动操作暂停", 停手后恢复秒数=f"{self.idle_seconds:g}")
        elif flipped:
            _log.info("鼠标键盘已停下，微信自动操作恢复", 停了秒数=round(idle or 0.0, 1))
        return paused

    def wait_idle(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """一直等到没人在用。只在微信操作的两步之间调（加好友 / 朋友圈的 `_human_pause`）。"""
        while self.busy():
            sleep(0.2)

    # ── 子进程 ──

    def start(self) -> bool:
        """起子进程装钩子。成功 True；装不上 False（打警告，照常执行，不暂停）。"""
        try:
            process = subprocess.Popen(
                _watch_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            _log.warning("鼠标键盘监听起不来，有人用电脑时不会自动暂停", 原因=str(exc)[:160])
            return False
        first: list[bytes] = []
        reader = threading.Thread(target=lambda: first.append(process.stdout.readline()),
                                  name="input-watch-ready", daemon=True)
        reader.start()
        reader.join(_READY_TIMEOUT_SECONDS)
        line = first[0].strip() if first else b""
        if line != b"ready":
            process.kill()
            _log.warning("鼠标键盘监听装不上，有人用电脑时不会自动暂停",
                         原因=line.decode("ascii", "replace")[:160] or "超时没回应")
            return False
        self._process = process
        threading.Thread(target=self._pump, args=(process,), name="input-watch", daemon=True).start()
        _log.info("鼠标键盘监听已开启：有人用电脑时微信自动操作暂停", 停手后恢复秒数=f"{self.idle_seconds:g}")
        return True

    def _pump(self, process: subprocess.Popen) -> None:
        for raw in process.stdout:
            parts = raw.split()
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            if parts[0] == b"h":
                self.note(int(parts[1]))
            elif parts[0] == b"r":
                self.released(int(parts[1]))
        if self._process is process:
            # 子进程没了：不能卡在「最后一刻按着键」上永远暂停，当它全松开。
            with self._lock:
                self._held = 0
            _log.warning("鼠标键盘监听意外退出，有人用电脑时不会自动暂停")

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001
            process.kill()


def _watch_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, WATCH_FLAG]
    return [sys.executable, "-m", "wxclient.human_activity"]


# ───────────────────────────── 全进程一份 ─────────────────────────────

_guard: HumanActivity | None = None
_guard_lock = threading.Lock()


def start(idle_seconds: float) -> None:
    """网关起来时开（`gateway.factory`）。``idle_seconds <= 0`` 或非 Windows：不开，不暂停。"""
    global _guard
    with _guard_lock:
        if _guard is not None:
            _guard.idle_seconds = float(idle_seconds)
            if idle_seconds > 0:
                return
            _guard.close()
            _guard = None
        if idle_seconds <= 0:
            _log.info("有人用电脑时自动暂停：已关闭（停手后恢复秒数=0）")
            return
        if sys.platform != "win32":
            return
        guard = HumanActivity(idle_seconds)
        if guard.start():
            _guard = guard


def stop() -> None:
    global _guard
    with _guard_lock:
        guard, _guard = _guard, None
    if guard is not None:
        guard.close()


def busy() -> bool:
    """有人在用电脑、微信 UI 操作该停手。没开监听时恒 False。"""
    guard = _guard
    return guard is not None and guard.busy()


def wait_idle() -> None:
    guard = _guard
    if guard is not None:
        guard.wait_idle()


if __name__ == "__main__":
    sys.exit(serve())
