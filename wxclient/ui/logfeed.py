"""日志节拍器：网关线程往里塞行，tk 线程按节拍取走。

**为什么要这一层。** 原来每来一行日志就 ``root.after(0, ...)`` 一次，每次回调新建一行
控件、再 ``update_idletasks()`` 逼一次全量重排。可日志从来不是一行一行来的——网关一忙
（收一批消息、掉线重连、启动那几秒）就是成串地来，于是界面被几百次全量重排压住，用户
那边的观感就是「动一下就没反应」。这里把行先攒着，tk 线程每 ~120ms 取一批、重排一次。

顺带堵掉一个隐患：``root.after`` 本不该在别的线程上调（tkinter 不保证线程安全）。改成
「网关线程只往队列里塞、tk 线程自己定时来取」之后，跨线程碰 tk 的地方一处都不剩。

**缩在托盘里就别画了。** 这程序绝大多数时间是最小化着的，没人看的日志一行都不该画。
暂停期间来的行直接丢掉——全量日志本来就在 ``LogBuffer`` 和日志文件里躺着，窗口再打开
时从缓冲回放最后一屏即可（``resume()`` 返回 True 就是在说「你得重画一次」）。

本模块不碰 GUI，所以能在 Linux 上单测。
"""

from __future__ import annotations

import threading

#: tk 线程取日志的节拍（毫秒）。120ms ≈ 8 次/秒，肉眼仍是「实时」，但把成串日志
#: 合并成一次重排。调小 → 更跟手也更容易被日志洪水压住；调大 → 省 CPU 但滚动发涩。
FLUSH_MS = 120

#: 攒着没取走的行数上限。tk 线程真被卡住时（比如用户拖着窗口不放）不能让队列涨到没边，
#: 超了就丢最旧的——丢的只是「界面上少显示几行」，日志文件里一行不少。
PENDING_CAP = 2000


class LogFeed:
    """一个线程安全的小队列 + 「现在有没有人在看」的开关。"""

    def __init__(self, *, pending_cap: int = PENDING_CAP) -> None:
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._cap = pending_cap
        self._paused = True  # 窗口默认缩在托盘里，先别画
        self._missed = False
        self._dropped = 0

    # -- 生产端（任意线程）---------------------------------------------

    def push(self, line: str) -> None:
        with self._lock:
            if self._paused:
                self._missed = True  # 记一笔「暂停期间有新日志」，恢复时整屏回放
                return
            self._pending.append(line)
            if len(self._pending) > self._cap:
                overflow = len(self._pending) - self._cap
                del self._pending[:overflow]
                self._dropped += overflow

    # -- 消费端（tk 线程）----------------------------------------------

    def drain(self) -> list[str]:
        """取走攒下的行。没有就返回空表（调用方据此跳过重排）。"""
        with self._lock:
            if not self._pending:
                return []
            lines, self._pending = self._pending, []
            return lines

    # -- 可见性 ---------------------------------------------------------

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._pending.clear()  # 攒着的也不用画了，缓冲里有

    def resume(self) -> bool:
        """恢复渲染。返回 True = 暂停期间有新日志，调用方需要整屏回放一次。"""
        with self._lock:
            missed = self._missed
            self._paused = False
            self._missed = False
            self._pending.clear()  # 整屏回放会覆盖，别再叠一遍
            return missed

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def dropped(self) -> int:
        """因为界面跟不上而没画出来的行数（只影响显示，日志文件不受影响）。"""
        with self._lock:
            return self._dropped
