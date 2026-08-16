"""对端地址的默认值。**放在包根，是因为它两边都不属于。**

业务后端地址同时是两条路的起点：桌面端登录（`ui/client_api.py`）和网关换票
（`gateway/ticket.py`）。默认值以前只写在配置中心的字段表（`ui/settings_store.py`）里，
于是「有没有默认值」取决于**谁先启动**——从界面进来的有，`python -m wxclient.gateway`
直接起的没有，后者会当场被 `validate_for_start` 顶回来说「必须配置 business_api_url」。

放在这个没有任何依赖的叶子模块里，两边 import 它都不会把对方的重家伙拖进来
（`gateway/__init__.py` 会把整个网关拉起来，`ui` 那边启动最早的几秒经不起这个）。
"""

from __future__ import annotations

#: 业务后端的正式地址。给了默认值，装机的人就不用抄一串域名——抄错的表现是
#: 「登录一直转圈」，而人根本不知道该怀疑哪里。改这里要连 README 一起改。
DEFAULT_BUSINESS_API_URL = "https://api.laoxiaozhi.cn"
