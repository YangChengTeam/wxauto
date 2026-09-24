"""日志缓冲：把 root logger 上的日志抄一份到内存，供 UI 窗口显示。

日志本身仍旧走 ``logging_config`` 那套（控制台 + 轮转文件），这里只是**多挂一个 handler**
把渲染好的同一行文本收进环形缓冲。UI 不重新实现一套格式化，看到的和日志文件里的一致。

⚠️ 缓冲里可能出现候选人昵称等真实信息（日志本来就有）。它只活在内存和本机日志文件里，
**不要**把缓冲内容写进仓库、截图进文档或上报到任何远端。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Iterable

from wxclient.logging_config import make_formatter

DEFAULT_CAPACITY = 2000

Listener = Callable[[str], None]


class LogBuffer:
    """线程安全的定长日志缓冲。写入方是日志线程，读取方是 UI 线程。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("缓冲容量必须为正数")
        self._lines: deque[str] = deque(maxlen=capacity)
        self._listeners: list[Listener] = []
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._lines.maxlen or 0

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            listeners = list(self._listeners)
        for listener in listeners:
            # 一个瞎了的监听者（比如窗口已经销毁）不该让日志链路跟着崩
            try:
                listener(line)
            except Exception:  # noqa: BLE001, S110 - 日志路径上绝不抛异常
                pass

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """注册监听者，返回退订函数。"""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.append(line)


class LogBufferHandler(logging.Handler):
    """把日志行塞进 ``LogBuffer`` 的 handler。"""

    def __init__(self, buffer: LogBuffer, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:  # noqa: BLE001 - handleError 会走标准的降级路径
            self.handleError(record)


class LogPipeline:
    """把日志接到缓冲上，并在被第三方顶掉之后自动接回来。

    2026-08-12 真机实测：``pyweixin`` 在 import 时会自己 ``logging.config.fileConfig()``
    （它自带 ``logger/logging.conf``），把 root 上的 handler **整套换掉**——我们的轮转
    文件 handler 和界面缓冲 handler 一起没了，界面从此再不刷新，日志文件也变了格式。
    它是 vendored 的第三方库，改它不现实，所以这里定期查一眼、掉了就接回去。
    """

    def __init__(
        self,
        buffer: LogBuffer,
        configure: Callable[[], None],
        level: int = logging.INFO,
    ) -> None:
        self.buffer = buffer
        self._configure = configure
        self._level = level
        self._handler: LogBufferHandler | None = None

    def install(self) -> LogBufferHandler:
        self._configure()
        self._handler = attach_to_root(self.buffer, self._level)
        return self._handler

    def ensure(self) -> bool:
        """handler 还在就什么都不做；被顶掉了就整套重装。返回是否重装过。"""
        if self._handler is not None and self._handler in logging.getLogger().handlers:
            return False
        self.install()
        logging.getLogger(__name__).info("日志通道被第三方重置，已重新接回界面")
        return True


def attach_to_root(buffer: LogBuffer, level: int = logging.INFO) -> LogBufferHandler:
    """把缓冲挂到 root logger 上，用不带执行栈的中文 formatter。

    必须在 ``configure_logging()`` **之后**调用——它会清空 root 上的 handler。
    """
    root = logging.getLogger()
    handler = LogBufferHandler(buffer, level)
    # 界面上不贴 Python 执行栈（只留异常最后一行），完整执行栈仍在日志文件里。
    handler.setFormatter(make_formatter(show_traceback=False))
    root.addHandler(handler)
    return handler
