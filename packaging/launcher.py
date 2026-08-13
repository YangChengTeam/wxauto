"""打包入口的一层壳。

**别直接把 ``wxclient/ui/__main__.py`` 喂给 PyInstaller。** 那样它是以「顶层脚本」的
身份被执行的，模块里 ``from . import settings_store`` 这类相对 import 立刻失效：

    ImportError: attempted relative import with no known parent package

而且 windowed 构建（``console=False``）里这条报错是弹在一个**看不见的对话框**上的——
真机现象就是「双击了，进程在任务管理器里，窗口永远不出来、日志一个字都不写」，
非常难查。这里按「包」的身份把 ``main`` 请出来，问题就没了。
"""

from __future__ import annotations

import sys

from wxclient.ui.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
