"""本地库读取放进**独立子进程**：扫钥、解密、查询都在那边跑，客户端只拿查询结果。

**为什么非得是进程、不能是线程。** 扫钥和解密大半是 Python 循环，和界面线程抢同一把 GIL。
真机 2026-11-04：单跑扫钥 4 秒；同进程里挂一个 tk 窗口就拖到 22 秒、界面一卡 0.6 秒；
到了真客户端（还有 UIA、asyncio、日志线程）界面卡 9 秒，一拖窗口就「未响应」被 Windows
关掉（AppHangB1，0.1.129/0.1.130 各一次）。换线程、调 switchinterval 都只是减轻；
换进程，界面那把 GIL 就再也不会被它占着。

协议：子进程 stdin/stdout 上一行一个 JSON。启动后先回一行 ``{"ok": true}``（连上本地库）或
``{"ok": false, "error": ...}``；之后每行请求 ``{"m": 方法名, "a": [参数]}``，回
``{"r": 结果}`` 或 ``{"e": 错误}``。**密钥只在子进程内存里，从不过管道**；管道上只有查询
结果（会话表、新消息），和原来同进程读出来的东西一样，不落盘。

子进程怎么起：冻结包是同一个 exe 带 ``--db-worker``（`packaging/launcher.py` 在 import 界面之前
就分流）；源码跑是 ``python -m wxclient.wechat_db.worker``。父进程一退，子进程 stdin 读到 EOF
自己退出。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

WORKER_FLAG = "--db-worker"

#: 子进程可被调用的只读查询（白名单：管道那头不能随便调别的方法）。
_METHODS = frozenset({
    "sessions", "unread_counts", "new_messages", "session_cursor",
    "resolve_username", "display_name", "poll_changes",
})

#: 等子进程连上本地库最多多久（秒）。它在自己进程里扫钥，单跑几秒；留足余量。
ATTACH_TIMEOUT_SECONDS = 180.0


# ───────────────────────────── 子进程这一侧 ─────────────────────────────

def serve() -> int:
    """子进程入口：连本地库，然后一问一答直到 stdin 关掉。"""
    proto_in = sys.stdin.buffer if sys.stdin else os.fdopen(0, "rb")
    proto_out = sys.stdout.buffer if sys.stdout else os.fdopen(1, "wb")
    # 协议独占 stdout：日志（structlog 默认打 stdout）全改去 stderr，别把协议行搅乱。
    # stderr 固定 UTF-8：管道默认走系统代码页（GBK），父进程按 UTF-8 解出来全是乱码。
    if sys.stderr:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr if sys.stderr else open(os.devnull, "w", encoding="utf-8")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stdout))

    def send(payload: dict) -> None:
        proto_out.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        proto_out.flush()

    from .reader import build_reader

    reader = build_reader()
    if reader is None:
        send({"ok": False, "error": "本地库连不上（密钥没扫到 / 账号对不上 / 非 Windows）"})
        return 1
    send({"ok": True})
    for raw in proto_in:
        try:
            request = json.loads(raw)
            method = request.get("m")
            if method not in _METHODS:
                raise ValueError(f"不支持的调用：{method}")
            send({"r": getattr(reader, method)(*request.get("a", []))})  # 结果都是 str/int 键的 dict、list
        except Exception as exc:  # noqa: BLE001 —— 单次查询出错回给父进程，子进程不退
            send({"e": f"{type(exc).__name__}: {exc}"[:300]})
    reader.close()
    return 0


# ───────────────────────────── 父进程这一侧 ─────────────────────────────

def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [sys.executable, "-m", "wxclient.wechat_db.worker"]


class WorkerReader:
    """跑在子进程里的 `WeChatDbReader` 的替身：方法一样，调用变成一来一回的管道请求。

    调用是阻塞的（等管道时不占 GIL，界面照常动）；同一时刻只许一个请求在路上。
    """

    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._lock = threading.Lock()

    def _call(self, method: str, *args: Any) -> Any:
        with self._lock:
            if self._process.poll() is not None:
                raise RuntimeError("本地库子进程已退出")
            line = json.dumps({"m": method, "a": list(args)}, ensure_ascii=False).encode("utf-8") + b"\n"
            self._process.stdin.write(line)
            self._process.stdin.flush()
            reply = self._process.stdout.readline()
        if not reply:
            raise RuntimeError("本地库子进程没有回应")
        payload = json.loads(reply)
        if "e" in payload:
            raise RuntimeError(payload["e"])
        return payload.get("r")

    def sessions(self) -> dict[str, tuple[int, str | None]]:
        return {name: (int(value[0]), value[1]) for name, value in self._call("sessions").items()}

    def unread_counts(self) -> dict[str, int]:
        return {name: int(count) for name, count in self._call("unread_counts").items()}

    def new_messages(self, username: str, after_local_id: int) -> list[dict[str, Any]]:
        return list(self._call("new_messages", username, int(after_local_id)))

    def session_cursor(self, username: str) -> int:
        return int(self._call("session_cursor", username))

    def resolve_username(self, display_name: str) -> str | None:
        return self._call("resolve_username", display_name)

    def display_name(self, username: str) -> str:
        return str(self._call("display_name", username))

    def poll_changes(self) -> bool:
        return bool(self._call("poll_changes"))

    def close(self) -> None:
        try:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._process.kill()


def _forward_stderr(process: subprocess.Popen) -> None:
    """子进程的日志（扫钥计数等）转进客户端日志，排障看得见。只有计数，不含密钥和正文。"""
    for raw in process.stderr:
        text = raw.decode("utf-8", "replace").strip()
        if text:
            _log.info("本地库子进程", 输出=text[-300:])


def build_worker_reader() -> WorkerReader | None:
    """起子进程、等它连上本地库。成了返回替身，否则返回 None（上层回退 UIA、稍后再试）。阻塞。"""
    try:
        process = subprocess.Popen(
            _worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        _log.info("本地库子进程起不来，回退到 UIA 轮询", 原因=str(exc)[:160])
        return None
    threading.Thread(target=_forward_stderr, args=(process,), name="wx-db-worker-log", daemon=True).start()
    ready: dict[str, Any] = {}

    def wait_ready() -> None:
        line = process.stdout.readline()
        if line:
            ready.update(json.loads(line))

    waiter = threading.Thread(target=wait_ready, name="wx-db-worker-ready", daemon=True)
    waiter.start()
    waiter.join(ATTACH_TIMEOUT_SECONDS)
    if not ready.get("ok"):
        _log.info("本地库入站不可用，回退到 UIA 轮询", 原因=str(ready.get("error") or "子进程超时没连上")[:160])
        process.kill()
        return None
    _log.info("已连上本地微信库（子进程）", pid=process.pid)
    return WorkerReader(process)


if __name__ == "__main__":
    sys.exit(serve())
