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
"""

from __future__ import annotations

import threading

#: 进程内唯一。谁要在后台线程上首次 import 重型模块（网关装配 / 微信驱动），先拿它。
HEAVY_IMPORT_LOCK = threading.Lock()
