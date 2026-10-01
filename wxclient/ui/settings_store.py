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

from .. import tuning
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
    minimum: float | None = None  # 网关那边的硬地板；填得比它低保存时当场拒绝，见 `validate`
    zero_means_off: bool = False  # 0 是「关掉」不是「0 秒」，不跟地板比
    locked_value: str = ""  # 非空＝只在演示模式开着时能改；关着时灰着显示这个数（正在生效的地板）


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
    # 不在配置中心里渲染，单独一页「极速模式」（原名「演示」，用户 2026-09-30 要求改名）（`window._ensure_demo_page`）。
    Section("demo", "极速开关", "开启后读消息间隔改为 5 秒，关闭后恢复原值"),
    Section("demo_wait", "等待与排队", "", columns=2),
    Section("demo_conn", "连接与超时", "", columns=2),
    Section("demo_keep", "保留期限", "", columns=2),
)

#: 「极速模式」那一页渲染的分段（`window._ensure_demo_page`）。
DEMO_SECTIONS: tuple[str, ...] = ("demo", "demo_wait", "demo_conn", "demo_keep")

#: 配置中心渲染的分段；`demo` 那段在自己的菜单页里。
SETTINGS_SECTIONS: tuple[str, ...] = ("conn", "sched", "boot")
DEMO_TOGGLE_KEY = "WXCLIENT_GATEWAY_DEMO_MODE"
#: 极速模式开着时配置中心间隔的地板；抄自 `gateway/config.py` 的 `DEMO_MIN_SECONDS`（同 default，不 import 网关包）。
DEMO_MIN_SECONDS = 1.0
#: 开启极速模式时配置中心里跟着改掉的格子 → 开启后的值；关掉时恢复开启前的原值（`apply_demo_presets`）。
#: 用户 2026-09-30：「开启极速模式读消息间隔变为 1，框框中的参数也要变」，随后改口「配置 5 秒吧，1 秒太快了」。
DEMO_PRESETS: dict[str, str] = {"WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS": "5"}
#: 极速模式开着时保存检查降到 `DEMO_MIN_SECONDS` 的格子。查新好友那格不在里面（用户 2026-09-30：「查好友不能太快」）。
DEMO_UNLOCKED: frozenset[str] = frozenset({"WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS"})
#: 开启前原值的备份键前缀（存在同一个 gateway.env 里，不上界面）。
DEMO_SAVED_PREFIX = "WXCLIENT_GATEWAY_DEMO_SAVED_"


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
          "默认已填好正式地址",
          default=DEFAULT_BUSINESS_API_URL),
    Field("WXCLIENT_GATEWAY_AI_WS_URL", "AI 端地址", "默认已填好正式地址",
          default=DEFAULT_AI_WS_URL),
    # ★ `minimum` 抄自 `gateway/pacing.py` 的 MIN_READ / MIN_FRIEND 两个常量，**不在这里 import**
    #   （同 default 的理由：`wxclient.gateway` 包一进来就是 structlog + pydantic）。
    #   抄错了由 `tests/test_settings_validation.py` 判死。用户 2026-09-18：原来填低了网关
    #   悄悄抬回地板、只打一条日志，界面上还显示你填的数——「配置改了没反应」。现在保存时就拒。
    Field("WXCLIENT_GATEWAY_POLL_INTERVAL_SECONDS", "读消息间隔",
          "空闲时多久看一次未读；最低 30", section="sched", unit="秒",
          default="180", minimum=30),
    Field("WXCLIENT_GATEWAY_FRIEND_CHECK_INTERVAL_SECONDS", "查新好友间隔",
          "0=不查；最低 300", section="sched", unit="秒", default="900",
          minimum=300, zero_means_off=True),
    # 用户 2026-09-07：说明太长把输入框挤坏了（调度配置一行两组），全部精简成半句。
    # 原来写在说明里的两条限定挪到这里：① 通过检测实际仍受「查好友」那档 ≥300 秒的
    # 整机节拍约束，填更小也不会更快；② 期限到了只是别让他占住名额，不代表判定对方
    # 不会通过。工作时间窗格式形如 09:00-21:00（placeholder/default 已经示范了），全天写 00:00-24:00。
    Field("WXCLIENT_GATEWAY_FRIEND_RECHECK_INTERVAL_SECONDS", "查好友通过间隔",
          "多久搜一次是否通过；0=不搜",
          section="sched", unit="秒", default="900"),
    Field("WXCLIENT_GATEWAY_FRIEND_RECHECK_DEADLINE_HOURS", "查好友通过期限",
          "超过就不再搜；0=一直搜",
          section="sched", unit="小时", default="72"),
    Field("WXCLIENT_GATEWAY_ACTIVE_HOURS", "工作时间窗", "窗外不动微信；全天填 00:00-24:00",
          section="sched", default="09:00-21:00"),
    # 用户 2026-09-15：私聊那档（20/时）也要能在配置中心改。原来界面上只有总闸一格，
    # 标题却叫「每小时发送上限」——专员把它调高，私聊照样被 20 那档拦住，而两边都
    # 「没配错」。总闸改名「发送总上限」，私聊档单独一格，两个数各管各的：
    # 总闸管所有写动作合计，私聊档只管私聊；两道都要过。
    Field("WXCLIENT_GATEWAY_MAX_SENDS_PER_HOUR", "发送总上限", "所有动作合计，每小时；0=不限",
          section="sched", default="0"),
    Field("WXCLIENT_GATEWAY_MAX_MESSAGES_PER_HOUR", "私聊上限", "每小时最多发几条私聊；0=不限",
          section="sched", default="0"),
    # 用户 2026-08-28：加好友的上限和间隔都要能在界面上改（不同账号的承受力不一样）。
    # 两个数管两件事：上限管**一小时总共几个**，间隔管**两次之间隔多久**。
    # 只有上限的话，5 个全挤进一分钟也照样放行——2026-08-27 弹安全验证就是这么来的。
    Field("WXCLIENT_GATEWAY_MAX_FRIEND_ACTIONS_PER_HOUR", "加好友上限",
          "每小时最多加几个；0=不限", section="sched", default="5"),
    Field("WXCLIENT_GATEWAY_FRIEND_INTERVAL_SECONDS", "加好友最小间隔",
          "两次之间至少隔多久", section="sched", unit="秒",
          default="300"),
    # 用户 2026-08-30：「朋友圈在过去 24 小时只能发 3 个呀」——这个数原来只在代码里，
    # 界面上看不见也改不了。它和上面两条不同，管的是**一天**不是一小时（一小时 3 条和
    # 一天 3 条完全是两回事），所以周期写在说明里，别让人按「每小时」去理解。
    Field("WXCLIENT_GATEWAY_MAX_MOMENTS_PER_DAY", "朋友圈上限",
          "每天最多几条", section="sched", default="3"),
    # 用户 2026-09-07：朋友圈自动点赞，一天上限 20。同样按**天**算；间隔管两趟之间隔多久。
    Field("WXCLIENT_GATEWAY_MAX_MOMENT_LIKES_PER_DAY", "朋友圈点赞上限",
          "每天最多点几个赞", section="sched", default="20"),
    Field("WXCLIENT_GATEWAY_MOMENT_LIKE_INTERVAL_SECONDS", "朋友圈点赞最小间隔",
          "两趟之间至少隔多久", section="sched", unit="秒",
          default="600"),
    # 用户 2026-09-07：朋友圈评论由业务端计划触发，一天几条「做成可配置」。评论是公开文字，默认 5。
    Field("WXCLIENT_GATEWAY_MAX_MOMENT_COMMENTS_PER_DAY", "朋友圈评论上限",
          "每天最多评几条", section="sched", default="5"),
    Field("WXCLIENT_GATEWAY_MOMENT_COMMENT_INTERVAL_SECONDS", "朋友圈评论最小间隔",
          "两趟之间至少隔多久", section="sched", unit="秒",
          default="600"),
    # 用户 2026-09-08：本机自己巡看朋友圈，只碰新动态。默认关（每趟开一次朋友圈窗口）。
    Field("WXCLIENT_GATEWAY_MOMENTS_POLL_INTERVAL_SECONDS", "朋友圈巡看间隔",
          "多久开一趟看新动态，0 = 不巡看", section="sched", unit="秒",
          default="0"),
    Field("WXCLIENT_GATEWAY_MOMENTS_AUTO_LIKE", "巡看时自动点赞",
          "只给新动态点，走点赞上限与间隔", kind="bool", section="sched", default="0"),
    Field("WXCLIENT_GATEWAY_MOMENTS_AUTO_COMMENT", "巡看时自动评论",
          "新动态交给劳小智按素材写评论", kind="bool", section="sched", default="0"),
    Field("WXCLIENT_GATEWAY_MOMENTS_COMMENT_MATERIAL", "朋友圈评论素材",
          "想传达什么、什么口吻，清空就不评", section="sched",
          default="岗位、招工、上班生活相关的动态可以接一句；语气自然、简短，不硬推销"),
    # 用户 2026-09-30：「这些原来写死的地板值搞一个专门菜单 演示，开启后就可以无视规则」。
    # 左侧单独一页（不和调度配置混在一起）；关着时三格显示正在生效的地板、不能改（`locked_value`），
    # 开着才能填。开启＝解除 30/300 秒地板并关掉随机抖动，只给演示号用（config.py `demo_mode`）。
    Field("WXCLIENT_GATEWAY_DEMO_MODE", "开启极速模式",
          "解除防风控下限、不加随机抖动；真实账号别开", kind="bool", section="demo", default="0"),
    # 读消息 / 好友 / 朋友圈三个间隔原来这里也各有一格，和配置中心重复（用户 2026-09-30「和配置中心
    # 重复了」）——删了。间隔只在配置中心填；这里的开关只决定配置中心那几格能填多低（`validate`）。
    Field("WXCLIENT_GATEWAY_DEMO_JITTER_PERCENT", "随机抖动",
          "每次在设定值上随机慢多少；关闭时 30", section="demo", unit="%",
          default="0", minimum=0, locked_value="30"),
    # 其余写死的时间项：一张表在 `wxclient/tuning.py`，这里照表生成，不再抄一份。
    *(Field(k.key, k.label, k.help, section=k.section, unit=k.unit, default=k.default,
            minimum=k.minimum, locked_value=k.locked) for k in tuning.KNOBS),
    Field("WXCLIENT_UI_AUTOSTART", "开机即启动服务", "随系统启动自动运行服务",
          kind="bool", section="boot", default="1"),
    Field("WXCLIENT_UI_START_HIDDEN", "启动后缩到托盘", "启动后最小化到系统托盘",
          kind="bool", section="boot", default="1"),
    Field("WXCLIENT_UI_AUTO_UPDATE", "自动检查更新", "有新版本时在左侧提示，点击即可安装",
          kind="bool", section="boot", default="1"),
)


def fields_of(section: str) -> tuple[Field, ...]:
    return tuple(field for field in FIELDS if field.section == section)

_TRUE = {"1", "true", "yes", "on", "是"}


def validate(values: dict[str, str]) -> dict[str, str]:
    """保存前的检查：返回 ``{键: 一句人话}``，空字典 = 都能存。

    只管**带 `minimum` 的格子**：填得比网关地板低，网关启动时会 `clamp_interval` 抬回去并只打一条
    日志，界面上仍显示你填的数——人看到的是「改了没反应」。与其事后悄悄改，不如保存时就说清楚。
    空着 = 用默认值，不算错；`zero_means_off` 的格子填 0 是「关掉」，也不算错。
    不是数字的按「填错」报，别让它带着一个字符串进 env 再在网关启动那一刻炸。
    """
    errors: dict[str, str] = {}
    # 极速模式开着：配置中心那几格的地板降到 1 秒，和网关 `GatewaySettings.read_floor_seconds` 等同步。
    demo = str(values.get(DEMO_TOGGLE_KEY, "")).strip().lower() in _TRUE
    for field in FIELDS:
        if field.minimum is None:
            continue
        minimum = field.minimum
        if demo and field.key in DEMO_UNLOCKED:
            minimum = min(minimum, DEMO_MIN_SECONDS)
        raw = str(values.get(field.key, "")).strip()
        if raw == "":
            continue
        try:
            number = float(raw)
        except ValueError:
            errors[field.key] = f"「{field.label}」要填数字，现在是「{raw}」"
            continue
        if number == 0 and field.zero_means_off:
            continue
        if number < minimum:
            floor = f"{minimum:g}"
            errors[field.key] = (
                f"「{field.label}」最低 {floor} {field.unit}，你填的是 {raw}"
                + ("（0 表示关掉）" if field.zero_means_off else "")
            )
    return errors


def _saved_key(key: str) -> str:
    return DEMO_SAVED_PREFIX + key.removeprefix("WXCLIENT_GATEWAY_")


#: 0.1.103 及以前极速页上那三格的键（和配置中心重复，0.1.104 删了）。老机器文件里还留着，读到就清掉。
_RETIRED_DEMO_KEYS = frozenset({
    "WXCLIENT_GATEWAY_DEMO_POLL_INTERVAL_SECONDS",
    "WXCLIENT_GATEWAY_DEMO_FRIEND_INTERVAL_SECONDS",
    "WXCLIENT_GATEWAY_DEMO_MOMENTS_INTERVAL_SECONDS",
})


def apply_demo_presets(values: dict[str, str]) -> dict[str, str]:
    """按极速开关的**当前状态**对齐 `DEMO_PRESETS` 那几格，返回新的一份（不改入参）。

    - 开着、还没备份过 → 备份原值，那一格改成预设值；
    - 关着、有备份 → 恢复备份（备份是空的就回默认），删掉备份；
    - 其余原样（开着期间在配置中心再改那一格，改多少算多少）。

    ★ 按状态判、不按「这次保存有没有翻面」判（09-30 真机：0.1.103 存下的文件里开关早就是开的，
      升级后保存不翻面，读消息间隔一直停在 180）。改的是配置中心那一格本身，界面上看到的就是生效值。
    """
    out = {key: value for key, value in values.items() if key not in _RETIRED_DEMO_KEYS}
    on = as_bool(out.get(DEMO_TOGGLE_KEY, ""))
    defaults = {field.key: field.default for field in FIELDS}
    for key, preset in DEMO_PRESETS.items():
        saved = _saved_key(key)
        if on and saved not in out:
            out[saved] = out.get(key) or defaults.get(key, "")
            out[key] = preset
        elif not on and saved in out:
            out[key] = out.pop(saved) or defaults.get(key, "")
    return out


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


def load_into_environ(data_dir: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """启动 / 重启前把**配置中心那份**灌进环境变量，文件里有的键**盖过**进程里已有的值。

    ★ 为什么必须 force（2026-09-22 真机：「发送总上限」填 0 存了，跑起来还是老默认 30）：
    自动更新是「旧版起安装程序 → 安装程序拉起新版」一条进程链，新版**继承了旧版的环境变量**，
    而旧版启动时早把它那一版的默认值（≤0.1.66 是 30/时）写进了 `os.environ`。原来这里
    「真实环境变量优先」，于是文件里明明是 0，网关读到的永远是链上游留下的 30——界面显示 0、
    日志按 30 拦，两边都「没配错」。文件缺的键仍回落环境变量再回落默认（`effective`），
    命令行 `set` 出来的值只在**文件没写这个键**时才算数：配置中心保存过就以配置中心为准。
    返回实际灌进去的那份，便于日志/测试核对。
    """
    values = effective(data_dir, environ)
    # 极速开关和读消息间隔对齐后落盘：升级上来的老文件（开关开着、间隔还是 180）启动一次就对上，
    # 配置中心进页重读磁盘看到的也是这个数。
    synced = apply_demo_presets(values)
    if synced != values and settings_path(data_dir).exists():
        save(data_dir, synced)
    apply_to_environ(synced, environ, force=True)
    return synced


def as_bool(value: str, default: bool = False) -> bool:
    text = str(value).strip().lower()
    if not text:
        return default
    return text in _TRUE
