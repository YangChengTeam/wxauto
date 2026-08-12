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


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str = ""
    secret: bool = False
    kind: str = "text"  # text | bool


FIELDS: tuple[Field, ...] = (
    Field("WXCLIENT_UI_ACCOUNTS", "微信号", "要接管的微信号，多个用逗号分隔"),
    Field("WXCLIENT_GATEWAY_AI_WS_URL", "AI 端地址", "wss://<ai-host>/v1/ws"),
    Field("WXCLIENT_GATEWAY_BUSINESS_API_URL", "业务端地址", "https://<business-host>，用于换票"),
    Field("WXCLIENT_GATEWAY_INTERNAL_KEY", "内部 key", "网关唯一持有的凭证", secret=True),
    Field("WXCLIENT_GATEWAY_LOCAL_TICKET_FILE", "本地票据文件", "仅联调用，与业务端地址二选一"),
    Field("WXCLIENT_GATEWAY_TASK_TYPE", "任务类型", "默认 wechat_reply"),
    Field("WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS", "轮询间隔（秒）"),
    Field("WXCLIENT_GATEWAY_DEBOUNCE_SECONDS", "去抖窗口（秒）", "对方连发多条时合并成一轮"),
    Field("WXCLIENT_GATEWAY_TURN_TIMEOUT_SECONDS", "单轮上限（秒）"),
    Field("WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR", "每小时发送上限"),
    Field("WXCLIENT_GATEWAY_ACTIVE_HOURS", "工作时间窗", "如 09:00-21:00，可跨午夜"),
    Field("WXCLIENT_UI_ALLOW_REAL_ACTIONS", "真的发消息", "关闭时整条回路照跑但不真发", kind="bool"),
    Field("WXCLIENT_UI_AUTOSTART", "开机即启动服务", kind="bool"),
    Field("WXCLIENT_UI_START_HIDDEN", "启动后缩到托盘", kind="bool"),
)

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
    保存时再把空值写回去，等于悄悄把配置抹了。所以文件缺的键回落到环境变量。
    """
    target = os.environ if environ is None else environ
    values = dict(load(data_dir))
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
