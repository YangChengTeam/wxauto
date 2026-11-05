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

if __name__ == "__main__":
    # 本地库子进程（`wxclient.wechat_db.worker`）：同一个 exe 带 `--db-worker` 起。
    # **在 import 界面之前分流**——它不要窗口、不要单实例锁、不要启动动画。
    if "--db-worker" in sys.argv[1:]:
        from wxclient.wechat_db.worker import serve

        sys.exit(serve())
    # 鼠标键盘监听子进程（`wxclient.human_activity`）：同上，只装钩子、报动静。
    if "--input-watch" in sys.argv[1:]:
        from wxclient.human_activity import serve as watch

        sys.exit(watch())
    from wxclient.ui.__main__ import main

    sys.exit(main())
