"""重型 import 的串行闸。

★ 2026-08-14 真机死锁：桌面端起来了、界面能动，但网关**永远起不来**（日志停在
「桌面端已启动」，没有「网关启动」）。py-spy 连抓两次、相隔两分钟栈一模一样：

    wx-gateway  → create_module  ← 正在 LoadLibrary  SQLAlchemy 的 Cython 扩展
    wx-ui-probe → create_module  ← 正在 LoadLibrary  pywinauto 的 C 扩展

两个线程同时首次 import 重型 C 扩展 = 同时 `LoadLibrary` 一个 .pyd，撞上
**Windows 进程级加载器锁**（loader lock）。谁都不放手，两边一起停住。
这也解释了前一晚那个「进程起来了却一条日志都没打」的实例——同一个 race，
只是那次卡在更早的位置。间歇性发作：取决于探测线程和网关线程有没有撞上。

所以凡是**在后台线程上第一次 import 重型模块**的地方，都必须先拿这把闸。
串行化只是把并发的首次加载排成队，第二次之后模块已在 `sys.modules` 里，
`import` 直接命中缓存、几乎不花时间——代价可以忽略。

放在**包根**而不是 `ui/` 下：拿这把闸的既有桌面端（探测线程），也有网关装配
（`gateway/factory.py` 的能力探测）。让 gateway 反过来 import ui 是层次颠倒。

★ 2026-09-17 第二次发作：启动动画停在「正在准备界面…」不动了，一定概率。这回撞的是
**主线程**：`MainWindow.__init__` 一建完就起探测线程，而主线程紧接着还在 import pystray、
登录页那一串。探测线程那边 `probe_wechat_uia()` 里的 psutil / win32api / pywinauto 全是
**函数内惰性 import**——上面那把闸围住的只是 `from wxclient.uia_probe import ...` 这一句
纯 Python 的 import，真正 LoadLibrary 那几个 .pyd 从来没在闸里。所以 08-14 的修法其实是
空的：同一个 race 换了个对手继续间歇发作。主线程卡在 mainloop 之前 = 动画不动、
「桌面端已启动」那行日志也永远写不出来，文件里连一条线索都没有。

两条改法一起上：
  ① `import_wechat_driver()`：微信驱动那套 C 扩展的**首次** import 集中在这一处、在闸里做；
     `probe_wechat_uia()` 和 `UiaWeChatDriver` 进门先喊它——闸围住的终于是真的 LoadLibrary。
  ② 探测线程改到 mainloop 起来之后（`root.after(0)`）才第一次起（`ui/window.py`）：
     主线程自己的启动 import **不拿闸**（tk 线程不能被闸挡住），那就让它先把自己的 import
     跑完，再放后台线程出来。
"""

from __future__ import annotations

import importlib
import platform
import threading

#: 进程内唯一。谁要在后台线程上首次 import 重型模块（网关装配 / 微信驱动），先拿它。
HEAVY_IMPORT_LOCK = threading.Lock()

#: 微信驱动链上会 LoadLibrary 的模块：comtypes（COM）、pywin32、psutil，最后 pywinauto
#: 把它们连带拉齐。谁真正用它们时 import 已命中 `sys.modules`，不再碰加载器。
WECHAT_DRIVER_MODULES = ("comtypes", "win32api", "win32gui", "psutil", "pywinauto")

_driver_imported = False


def import_wechat_driver() -> None:
    """在闸里做一次微信驱动 C 扩展的首次 import；之后再喊只是查一个布尔。

    非 Windows 直接返回：那儿没有加载器锁，也没有 pywinauto。
    import 失败不抛——谁真要用谁会在自己的惰性 import 上得到原样的 ImportError；
    这里只负责「第一次加载排队」，不负责「一定装得上」。装不上的也标成做过：
    反复重试只会每 15 秒再走一遍失败的查找。
    """
    global _driver_imported
    if _driver_imported or platform.system() != "Windows":
        return
    with HEAVY_IMPORT_LOCK:
        if _driver_imported:
            return
        for name in WECHAT_DRIVER_MODULES:
            try:
                importlib.import_module(name)
            except Exception:  # noqa: BLE001 - 见 docstring：装不上由真正用它的那处报
                pass
        _driver_imported = True
