"""UI 的启动参数。全部走环境变量，凭证一个都不在这（票据仍由网关自己换）。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class UiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WXCLIENT_UI_", extra="ignore")

    accounts: str = ""
    """要接管的微信号，多个用逗号分隔。"""

    allow_real_actions: bool = True
    """真的往微信发消息。

    ★ 2026-08-16 由**关**改成**开**（用户：「我们都是真机真实动作」）。原来默认关是
    开发期的取法，可装到招聘专员机器上的产品要是一条消息都不真发，它就什么也不是——
    而且更坏的是 dry-run 下每条写指令**照样回 done**，AI 端会照着回执告诉用户
    「已经发过去了」，其实一条都没发。

    要关掉仍然可以：``WXCLIENT_UI_ALLOW_REAL_ACTIONS=0``。关掉时界面右下角会重新出现
    「当前只走流程、不会真的发消息」，能力上报也会如实降成 ``limited``——那句提示不是
    装饰，是**当前档位的实话**，所以只随档位出现或消失，绝不单独藏掉。

    真发不等于放开了乱发：谁会被回复由 AI 端推下来的名单说了算（069 fail-closed，
    空名单 = 一个都不回），另有频次配额、工作时间窗和节拍下限各管一层。
    """

    autostart: bool = True
    """开窗即启动服务。"""

    start_hidden: bool = True
    """启动后直接缩到右下角托盘，不弹窗。"""

    auto_update: bool = True
    """后台定时查产品站有没有新版本（`ui/updater.py`）。关掉只是不自动查，配置中心里仍能手动查。"""

    update_manifest_url: str = ""
    """版本清单地址；空 = 产品站的 `latest.json`。只给测试/预发指向别处用，不进配置中心。"""

    log_lines: int = 2000
    """日志窗口最多保留多少行。"""
