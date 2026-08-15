"""参数的持久化：一个本机 env 文件。

界面上改的参数最终仍要变成 ``WXCLIENT_*`` 环境变量——配置的**唯一读法**不变，
这里只是给它加了一个「上次填的值存哪」。文件落在运行数据目录（``.wxclient/``），
和日志、队列库一起，**永不入库**（见 README「永不入库」）。

内部 key 属于凭证：它进这个本机文件，但绝不进仓库、不进日志、界面上也只显示掩码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

FILE_NAME = "gateway.env"

#: 后端下发的那一份。**和本机那份分开存**，因为两份的权威不同：下发的可以随时被后端整份
#: 覆盖，本机填的是这台机器上的人自己的决定，不能被下一次拉取冲掉（用户 2026-08-15 裁决：
#: 「本地可以改 后端可下发」）。合并规则只有一条：同名键**本机盖过后端**，见 ``effective``。
REMOTE_FILE_NAME = "gateway.remote.env"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str = ""
    secret: bool = False
    kind: str = "text"      # text | bool | file
    section: str = "conn"   # 归到哪一段，见 SECTIONS
    unit: str = ""          # 输入框右边贴着的单位（设计稿上「秒」是框内的一格）
    placeholder: str = ""   # 空着时框里的灰字。**只是提示，永远不会被当成值**


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
#:   ③ 群聊开关等应急项——留作环境变量，出事时手工加一行，不占日常界面。
#: 这些键手工写进 gateway.env 依然生效（``render_env_text`` 会原样保留、``apply_to_environ``
#: 会照灌），只是不再摆在人人都会点的界面上。
FIELDS: tuple[Field, ...] = (
    Field("WXCLIENT_UI_ACCOUNTS", "微信号", "要接管的微信号，多个用逗号分隔"),
    Field("WXCLIENT_GATEWAY_AI_WS_URL", "AI 端地址", "wss://<ai-host>/v1/ws"),
    Field("WXCLIENT_GATEWAY_BUSINESS_API_URL", "业务端地址", "https://<business-host>，用于换票",
          placeholder="用于换票，可选"),
    Field("WXCLIENT_GATEWAY_INTERNAL_KEY", "内部 key", "网关唯一持有的凭证", secret=True),
    Field("WXCLIENT_GATEWAY_LOCAL_TICKET_FILE", "本地票据文件", "仅联调用，与业务端地址二选一",
          kind="file"),
    Field("WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS", "读消息间隔",
          "多久看一次未读，下限 30 秒", section="sched", unit="秒"),
    Field("WXCLIENT_GATEWAY_FRIEND_CHECK_INTERVAL_SECONDS", "查新好友间隔",
          "单独的节拍，0=不查", section="sched", unit="秒"),
    Field("WXCLIENT_GATEWAY_ACTIVE_HOURS", "工作时间窗", "如 09:00-21:00，窗外不动微信",
          section="sched"),
    Field("WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR", "每小时发送上限", "防止消息风控",
          section="sched"),
    Field("WXCLIENT_UI_AUTOSTART", "开机即启动服务", "随系统启动自动运行服务",
          kind="bool", section="boot"),
    Field("WXCLIENT_UI_START_HIDDEN", "启动后缩到托盘", "启动后最小化到系统托盘",
          kind="bool", section="boot"),
)


def fields_of(section: str) -> tuple[Field, ...]:
    return tuple(field for field in FIELDS if field.section == section)

_TRUE = {"1", "true", "yes", "on", "是"}


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def remote_path(data_dir: Path) -> Path:
    return Path(data_dir) / REMOTE_FILE_NAME


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


def load_remote(data_dir: Path) -> dict[str, str]:
    path = remote_path(data_dir)
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


def save_remote(data_dir: Path, values: dict[str, str]) -> Path:
    """整份覆盖后端那一份。

    **整份覆盖、不做合并**：后端删掉一个键，意思就是「这个键不该再有值」，
    合并会让被删的键在本机永远活下去。本机自己填的在另一个文件里，冲不掉。
    """
    path = remote_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 后端下发的网关参数（登录后自动拉取，本机改动请在「配置中心」里改）",
        "# 这个文件每次拉取都会被整份覆盖，手工改这里没意义。",
    ]
    lines.extend(f"{key}={value}" for key, value in sorted(values.items()) if value != "")
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def merged(data_dir: Path) -> dict[str, str]:
    """后端下发打底，本机填的盖在上面。**服务真正按这份跑。**"""
    values = dict(load_remote(data_dir))
    for key, value in load(data_dir).items():
        if value != "":
            values[key] = value
    return values


def overridden(data_dir: Path) -> dict[str, str]:
    """本机改过、和后端下发的不一样的键 → 后端那个值。

    配置中心要把这件事标出来：两处各有一份真相时，「我改的到底生不生效」必须一眼看得见，
    否则排障时会对着后端管理页反复怀疑人生。
    """
    remote = load_remote(data_dir)
    local = load(data_dir)
    return {
        key: remote[key]
        for key, value in local.items()
        if value != "" and key in remote and remote[key] != value
    }


def effective(data_dir: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """界面要显示的是「当前实际生效的值」，不是「文件里存了什么」。

    命令行 `set` 出来的参数在文件里没有，界面上却空着——用户会以为没配，
    保存时再把空值写回去，等于悄悄把配置抹了。所以文件缺的键回落到环境变量。
    """
    target = os.environ if environ is None else environ
    values = merged(data_dir)
    for field in FIELDS:
        if not values.get(field.key):
            from_env = target.get(field.key, "")
            if from_env:
                values[field.key] = from_env
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
