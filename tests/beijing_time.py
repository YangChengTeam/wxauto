"""把测试进程的本机时区钉在东八区。

`guard.within_active_hours` 比的是**本机墙上时间**（见它的 docstring），用例里的「工作日下午两点」
是按北京时间写的：机器不在东八区（比如 CI 或在国外的开发机）时，同一个时刻换成本地就落到时间窗
外面，几十个用例跟着失败——那是测试对环境的隐含假设，不是产品坏了。

需要的测试模块 ``import beijing_time`` 即可。Windows 没有 ``time.tzset``，那里不动（真机本来就在东八区）。
"""

from __future__ import annotations

import os
import time

if hasattr(time, "tzset"):
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
