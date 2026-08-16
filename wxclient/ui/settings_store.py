"""参数的持久化：一个本机 env 文件。

界面上改的参数最终仍要变成 ``WXCLIENT_*`` 环境变量——配置的**唯一读法**不变，
这里只是给它加了一个「上次填的值存哪」。文件落在运行数据目录（``.wxclient/``），
和日志、队列库一起，**永不入库**（见 README「永不入库」）。

★ 这里**一个凭证都没有**：客户端 Token 由业务端签发，加密存在 `client-session.json`
（DPAPI，见 `token_store.py`），永远不进这个 env 文件、不进日志、不进环境变量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..endpoints import DEFAULT_AI_WS_URL, DEFAULT_BUSINESS_API_URL

FILE_NAME = "gateway.env"

# ★ 曾经还有一份 `gateway.remote.env`（后端下发、本机覆盖）。2026-08-15 删了：
#   正式的客户端接口（`docs/招聘分身_客户端认证与状态接口_v1.1.md`）**不下发任何配置**，
#   留着那一层只会让「我改的到底生不生效」有两个答案。参数只有这一份。


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str = ""
    secret: bool = False
    kind: str = "text"      # text | bool
    section: str = "conn"   # 归到哪一段，见 SECTIONS
    unit: str = ""          # 输入框右边贴着的单位（设计稿上「秒」是框内的一格）
    placeholder: str = ""   # 空着时框里的灰字。**只是提示，永远不会被当成值**
    default: str = ""       # 没配过时**真正生效**的值（和 placeholder 不是一回事）


@dataclass(frozen=True)
class Section:
    """配置中心的一段（`docs/setting.png`）：标题 + 一句说明，底下一条横线。

    分段是给人看的，不影响存储——所有键仍然平铺在同一个 env 文件里。
    """

    key: str
    title: str
    desc: str
    columns: int = 1  # 一行摆几组「标签 + 输入框 + 说明」


SECTIONS: tuple[Section, ...] = (
    Section("conn", "连接配置", "配置与 AI 端及业务端的连接信息"),
    Section("sched", "调度配置", "配置消息读取与任务调度规则", columns=2),
    Section("boot", "启动设置", "配置服务启动后的行为"),
)


#: 界面上只放**必须由这台机器的人来定**的参数（用户 2026-08-13：「配置简洁一点，不用搞一堆」）。
#:
#: 刻意不放的三类：
#:   ① 有正确默认值、改了只会更糟的（任务类型、去抖窗口、单轮上限）；
#:   ② 监听哪些会话——权威在 AI 端，由 WS 推下来（``gateway/watchlist.py``），不该在这里手抄；
#:   ③ 群聊开关等应急项——留作环境变量，出事时手工加一行，不占日常界面；
#:   ④ **登录接口已经给了的**（2026-08-15）：微信号（改用登录返回的 agent_id 记账，见
#:      `auth.Identity.account`）、内部 key（换票改用客户端 Token）、本地票据文件（仅联调，
#:      留作环境变量 `WXCLIENT_GATEWAY_LOCAL_TICKET_FILE`）。手抄凭证这件事到此为止。
#: 这些键手工写进 gateway.env 依然生效（``render_env_text`` 会原样保留、``apply_to_environ``
#: 会照灌），只是不再摆在人人都会点的界面上。

#: ★ 每一项都必须有 `default`——**界面上不许出现空格子**。
#:
#: 2026-08-16 装完安装包第一次打开就是这个问题：干净机器上没有 gateway.env，于是
#: 「连接配置 / 调度配置 / 启动设置」全是空的，网关当场报「AI_WS_URL 必填」。而这些
#: 参数**运行时明明有默认值**（在 `GatewaySettings` / `UiSettings` 里），只是界面不知道，
#: 于是人看到的是「什么都没配」，实际上是「配好了只是没显示」——最坏的一种不一致。
#:
#: 值抄自那两个 Settings 类，但**不在这里 import 它们**：那会把 pydantic 拉进桌面端
#: 启动最早的几秒（启动慢的老问题，见 `ui/__main__.py` 顶上的说明）。
#: 抄漏了由 `tests/test_ui.py::SettingsDefaultsTests` 当场判死——它会去比对两边。
FIELDS: tuple[Field, ...] = (
    Field("WXCLIENT_GATEWAY_BUSINESS_API_URL", "业务端地址",
          "登录和换票都用它，默认已填好正式地址",
          default=DEFAULT_BUSINESS_API_URL),
    Field("WXCLIENT_GATEWAY_AI_WS_URL", "AI 端地址", "默认已填好正式地址",
          default=DEFAULT_AI_WS_URL),
    Field("WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS", "读消息间隔",
          "多久看一次未读，下限 30 秒", section="sched", unit="秒", default="60"),
    Field("WXCLIENT_GATEWAY_FRIEND_CHECK_INTERVAL_SECONDS", "查新好友间隔",
          "单独的节拍，0=不查", section="sched", unit="秒", default="900"),
    Field("WXCLIENT_GATEWAY_ACTIVE_HOURS", "工作时间窗", "如 09:00-21:00，窗外不动微信",
          section="sched", default="09:00-21:00"),
    Field("WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR", "每小时发送上限", "防止消息风控",
          section="sched", default="30"),
    Field("WXCLIENT_UI_AUTOSTART", "开机即启动服务", "随系统启动自动运行服务",
          kind="bool", section="boot", default="1"),
    Field("WXCLIENT_UI_START_HIDDEN", "启动后缩到托盘", "启动后最小化到系统托盘",
          kind="bool", section="boot", default="1"),
)


def fields_of(section: str) -> tuple[Field, ...]:
    return tuple(field for field in FIELDS if field.section == section)

_TRUE = {"1", "true", "yes", "on", "是"}


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def parse_env_text(text: str) -> dict[str, str]:
    """解析 ``KEY=VALUE`` 文本。忽略空行与 ``#`` 注释，值里的等号原样保留。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def render_env_text(values: dict[str, str]) -> str:
    lines = [
        "# 劳小智微信网关参数（本机运行数据，永不入库）",
        "# 由桌面端「参数设置」写入；也可手工编辑，重启服务生效。",
    ]
    for field in FIELDS:
        value = values.get(field.key, "")
        if value != "":
            lines.append(f"{field.key}={value}")
    # 保留界面不认识但用户手工加过的键，别默默吃掉
    known = {field.key for field in FIELDS}
    extras = sorted(key for key in values if key not in known)
    if extras:
        lines.append("")
        lines.append("# 以下是界面之外手工添加的参数")
        lines.extend(f"{key}={values[key]}" for key in extras)
    return "\n".join(lines) + "\n"


def load(data_dir: Path) -> dict[str, str]:
    path = settings_path(data_dir)
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


def effective(data_dir: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """界面要显示的是「当前实际生效的值」，不是「文件里存了什么」。

    命令行 `set` 出来的参数在文件里没有，界面上却空着——用户会以为没配，
    保存时再把空值写回去，等于悄悄把配置抹了。所以文件缺的键回落到环境变量，
    再回落到 ``Field.default``（有默认值的参数，界面上就该显示那个默认值）。
    """
    target = os.environ if environ is None else environ
    values = load(data_dir)
    for field in FIELDS:
        value = values.get(field.key) or target.get(field.key, "") or field.default
        if value:
            values[field.key] = value
        else:
            values.pop(field.key, None)  # 三处都没有：当成没配过，别在界面上留个空键
    return values


def save(data_dir: Path, values: dict[str, str]) -> Path:
    path = settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text(render_env_text(values), encoding="utf-8")
    tmp.replace(path)  # 原子替换：写一半断电也不会留下半个配置
    return path


def apply_to_environ(
    values: dict[str, str],
    environ: dict[str, str] | None = None,
    *,
    force: bool = False,
) -> None:
    """把配置灌进环境变量。

    默认**真实环境变量优先**：命令行 `set` 出来的值不会被文件悄悄覆盖，否则排障时
    「我明明设了」会变成一桩悬案。``force=True`` 用于界面上刚改完参数要重启服务的场景
    ——那一刻用户的意图明确就是「按我刚填的来」。
    """
    target = os.environ if environ is None else environ
    for key, value in values.items():
        if value == "":
            continue
        if force or key not in target:
            target[key] = value


def as_bool(value: str, default: bool = False) -> bool:
    text = str(value).strip().lower()
    if not text:
        return default
    return text in _TRUE
