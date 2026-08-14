"""单实例守卫：**同一台机器上只许有一个网关进程驱动微信。**

2026-08-13 真机出过一次：一次重启点了两下，两个进程同时接管同一个微信，
于是同一条消息被读两遍、回两遍，而且两边都在按各自的节拍动窗口——
风控最不喜欢的形状，账号状态机（需求文档 §12.3）也彻底失去意义。

做法是最土也最可靠的一种：在数据目录里占一个锁文件，锁**跟着进程走**——
进程没了（哪怕是被强杀、蓝屏），操作系统自动释放，不会留下一把要人工清理的死锁。
不用「写 PID 再检查进程在不在」，那套在 PID 被复用时会误判。

这一层是独立的：它只回答「我是不是第一个」，谁来用、拿到 False 之后做什么都不管。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType

import structlog

_log = structlog.get_logger(__name__)

#: 锁文件名。放在数据目录里，跟日志、gateway.db 做伴。
LOCK_FILENAME = "ui.lock"


def _lock_exclusive(handle) -> bool:
    """给已打开的文件加**非阻塞**独占锁。拿不到返回 False（说明别人占着）。"""
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


class SingleInstance:
    """占住锁文件就代表「这台机器上我是唯一的那个」。

    典型用法：

        guard = SingleInstance(data_dir)
        if not guard.acquire():
            return 0        # 已经有一个在跑了，这次启动直接退出

    ★ 拿到锁之后**必须一直持有到进程结束**——所以这个对象要活到最后，
    别让它被 GC 掉（文件一关，锁就没了）。
    """

    def __init__(self, data_dir: str | Path, filename: str = LOCK_FILENAME) -> None:
        self.path = Path(data_dir) / filename
        self._handle = None

    def acquire(self) -> bool:
        """返回 True = 我是唯一的那个；False = 已经有一个在跑了。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+")
        except Exception as exc:  # noqa: BLE001 —— 见下：什么原因都不该拦住启动
            # 锁文件建不出来（目录只读、盘满、路径本身就不合法）——这时候**放行**：
            # 为了一个防重复的机制把**唯一**一个实例也拦在门外，比偶尔重复更糟。
            # 所以这里刻意不挑异常类型：能想到的是 OSError，想不到的那些同样不该拦人。
            _log.warning("单实例锁文件打不开，跳过检查", error=str(exc))
            return True

        if not _lock_exclusive(handle):
            handle.close()
            return False

        self._handle = handle
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass       # PID 只是给人看的，写不进去不影响锁本身
        return True

    def release(self) -> None:
        """松开锁。正常退出时调；进程没了操作系统也会自动放。"""
        if self._handle is None:
            return
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def raise_existing_window(title: str) -> bool:
    """尽力把已经在跑的那个窗口拽到前台。做不到就算了，返回 False。

    纯粹是体验：不这么做的话，用户双击图标后**什么都不会发生**（第二个进程默默退出），
    看起来就像程序坏了。Windows 之外没有这回事，直接返回 False。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        handle = user32.FindWindowW(None, title)
        if not handle:
            return False
        user32.ShowWindow(handle, 9)        # SW_RESTORE：最小化的也拉起来
        user32.SetForegroundWindow(handle)
    except Exception as exc:  # noqa: BLE001 - 纯体验，失败不该影响退出
        _log.debug("拉起已有窗口失败", error=str(exc))
        return False
    return True
