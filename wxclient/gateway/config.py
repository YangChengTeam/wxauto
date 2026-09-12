"""网关配置。

秘密只从环境变量读，绝不写进仓库（``CLAUDE.md`` §5.1）。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..endpoints import DEFAULT_AI_WS_URL, DEFAULT_BUSINESS_API_URL
from .pacing import MIN_FRIEND_INTERVAL_SECONDS


class GatewaySettings(BaseSettings):
    """网关运行参数，env 前缀 ``WXCLIENT_GATEWAY_``。"""

    model_config = SettingsConfigDict(env_prefix="WXCLIENT_GATEWAY_", extra="ignore")

    # ── 对端地址 ──
    ai_ws_url: str = DEFAULT_AI_WS_URL
    """劳小智 AI 服务的 WS 门，如 ``wss://host/v1/ws``。

    ★ 和 ``business_api_url`` 一样带默认值：干净机器上装完就能用。没有默认值那一版，
    装完第一次登录直接是「网关启动失败：WXCLIENT_GATEWAY_AI_WS_URL 必填」。
    """

    business_api_url: str = DEFAULT_BUSINESS_API_URL
    """业务后端基址。登录（`ui/client_api.py`）和换票（`ticket.py`）都用它。

    ★ 默认值在这里，不在配置中心的字段表里：不然「有没有默认值」就取决于是谁启动的
    ——从界面进来的有，`python -m wxclient.gateway` 直接起的没有。
    """

    # ── 联调用的本地票据来源（生产必须留空；界面上没有这一项，只认环境变量）──
    local_ticket_file: Path | None = None

    # ── 对话 ──
    task_type: str = "wechat_reply"

    debug_task_type: str = "wechat_ops"
    """「调试会话」那一页用的 task_type（066 的人话→指令卡）。

    和入站回复分开是因为两者要的是不同的东西：入站给的是一段聊天记录、要的是一句回复；
    调试会话给的是一句指令（「给张三发条消息」「发朋友圈」）、要的是一张指令卡。
    拿 ``wechat_reply`` 去说「发朋友圈」，AI 端只会**当成候选人在说话**并拟一句回复。"""

    turn_timeout_seconds: float = 180.0
    """一轮对话的上限。AI 端整轮常在 20–30s，留足余量。"""

    ticket_timeout_seconds: float = 3.0
    """换票超时。超时即 fail-closed，不重试绕过。"""

    # ── 节拍：读消息与加好友各走各的（用户 2026-08-13 要求；防风控见 pacing.py）──
    poll_interval_seconds: float = 180.0
    """多久读一次未读消息（**没人在聊的时候**）。默认 3 分钟（用户 2026-08-16 定）。

    这个数只往大了调、不往小了调：原来的 5 秒是「一秒不差地翻聊天列表」，正是
    2026-08-12 那次自动退登（判定触发风控）的可疑行为之一。之后先改成 60 秒，现在
    再放宽到 180 秒。**正聊着的时候不受它影响**——对方刚说过话就切到
    ``active_poll_interval_seconds``（60 秒），聊完自动回到这个节拍，所以放慢它
    不会让人干等。实际间隔还会带随机抖动。"""

    active_poll_interval_seconds: float = 60.0
    """**正聊着的时候**多久读一次。对方刚说过话 → 接下来 ``hot_window_seconds`` 内按这个
    节拍读，聊完自动回到 ``poll_interval_seconds``。人的节奏本来就是「在聊就盯着，聊完放下」，
    固定 5 分钟既让对方干等，又在没人说话时白翻微信。
    ⚠️ 仍然被 ``pacing.MIN_READ_INTERVAL_SECONDS``（30 秒）把住下限，抖动和工作时间窗不变。"""

    hot_window_seconds: float = 600.0
    """对方最后一次说话之后，还按快档读多久。0 = 关掉自适应，永远用 ``poll_interval_seconds``。"""

    friend_check_interval_seconds: float = 900.0
    """多久查一次新好友申请。加好友是风控最敏感的一类动作，节拍要比读消息慢一个量级。
    ``0`` = 完全不查。查到之后通不通过由下面的 ``auto_accept_friend`` 定（见 ``friends.py``）。"""

    auto_accept_friend: bool = True
    """别人发来的好友申请（候选人扫招聘老师的名片主动加的）要不要自动点「通过」。

    **默认开**（用户 2026-09-12 裁决：「要改成自动通过」）。之前一直只看不通过，理由是
    「通过是写动作、该由 AI 端判这个人是不是我们要加的候选人」——可扫名片加进来的人本来就没有
    任何名单可对，能判的只有「他找上门了」；卡着不通过的结果是候选人加了好友一直没人理。

    通过走的仍是指令那条路（``CommandExecutor`` → ``accept_new_friends``）：能力闸、加好友配额
    （每小时 5 次）、工作时间窗一道不绕；一趟最多通过 5 个，剩下的下一趟。通过之后这个人先临时
    纳入监听（``strangers.py`` 的 ``friend_request`` 来源，不过期），第一轮由 AI 端接话并建档。
    留这个开关是因为它是自主发起的对外动作——出事要能一键停掉。"""

    # ── 名单里还不是好友的人，自动补加（092）──
    auto_add_friend: bool = True
    """监听名单里连续几轮读不到的会话，确认「确实不在通讯录」之后要不要自动补一条好友申请。

    **默认开**（用户 2026-08-27 要求的就是这个行为）。留这个开关是因为它是一个
    **自主发起的对外动作**，而加好友是微信风控最敏感的一类——出事要能一键停掉，
    不用改代码、不用重新打包。关掉之后连判都不判（连通讯录都不去搜）。

    判定与去重见 ``autofriend.py``：确认不是好友才加、同一个号只自动加一次、
    验证语不填（微信用它自己预填的那句）。
    """

    # ── 已申请、还没通过的好友：隔一阵复查一次（093）──
    friend_recheck_interval_seconds: float = 900.0
    """多久去微信里复查一个「申请发出去了、还没通过」的人。``0`` = 完全不复查。

    为什么非有不可：`add_friend` 成功只代表**申请发出去了**，对方哪天点通过没人知道；
    而这一档人**不在监听名单里**（名单只要 `accepted`），他通过之后发的消息网关也读不到。
    没有这条复查，我们主动加的人一旦通过，系统里没有任何一处会发现（见 ``friendcheck.py``）。

    只读、不发申请，但仍在动微信搜索，所以走的是和「查好友申请」**同一档共享节拍**
    （``guard.READ_FRIENDS_ACTION``，≥300s，整台机器共用），一轮最多查一个人。
    另外它还要给**主动加好友**让路：刚加过（``guard.FRIEND_WRITE_ACTION`` 那本账，只看不占）
    就跳过这轮——连着两次搜人正是 2026-08-27 弹出安全验证的形状。
    """

    friend_recheck_deadline_hours: float = 72.0
    """一个人**申请发出去之后**，最多还去微信里搜多久就不再搜了。``0`` = 不设期限，一直搜。

    2026-09-01 用户改口径，取代原来按次数的 `friendcheck.MAX_CHECKS`（96 次）：次数是个
    换算不出来的数——每轮最多查一个人、还要过 ≥300s 共享闸和工作时间窗，96 次实际横跨
    好几天，谁也说不准是几天。期限是看得懂、改得动的一个数。

    期限**从申请那一刻算起**，不是从上次查算起，否则一个查得慢的人可以无限续期。
    到期不是「判定他不会通过」，只是**别让他一直占住每轮那一个名额**——名额被占住，
    后面新申请的人就一直排不上。"""

    # ── 陌生人主动发起（090 / 接管托管同步接口 v1 §9.5）──
    stranger_intake: bool = True
    """要不要接待**主动找上门、劳小智库里还没有**的人。**默认开**（用户裁决 2026-08-29）。

    开着时：会话列表里今天有动静、而我们不认识的名字会被**临时**纳入监听
    （有上限、有保鲜期，见 ``strangers.py``），第一轮带 ``first_contact`` 交给 AI 端判——
    **判不出对方在求职就一条都不发**。所以最坏情况是白读一次会话。

    关掉时：只有监听名单（= 后端已知的候选人）里的会话才会被读、被回复，
    也就是 069 那套 fail-closed 语义。

    ★ 默认值改过一次。原来默认关，理由是「装上不会自动回复专员的家人朋友」
    （需求文档 §13.1）。改成默认开的理由：主动来问的人正是要接的那一类，
    默认关等于每台机器装完都得有人记得来打开一次。**真正兜住误伤的是后面那几道
    确定性闸**（临时准入有上限有保鲜期、第一轮判不出求职就不发话、建档四条闸），
    不是这个开关的默认值。不想接的专员在配置中心关掉即可。"""

    stranger_scan_interval_seconds: float = 600.0
    """多久扫一遍会话列表找陌生人。只读列表、不开聊天窗口，但仍在动微信主窗口，
    下限按「查好友」那一档（``strangers.MIN_SCAN_INTERVAL_SECONDS`` = 300s）把住。
    ``0`` = 不扫。"""

    debounce_seconds: float = 3.0

    # ── 处理范围：哪些会话才轮到网关管 ──
    #
    # 权威名单由 AI 端通过 WS 推下来（见 watchlist.py），界面上不再让人手抄一份。
    # 下面三个只是**环境变量应急开关**：出事要立刻掐掉某个会话时不用等 AI 端改数据。
    peer_allowlist: str = ""
    """只处理这些会话（微信里显示的名字，逗号分隔）。**群名和人名放同一份名单**。

    留空 = 等 AI 端推名单下来；在那之前一个会话都不碰（069 fail-closed）。
    原来「留空 = 不限制」意味着装上就自动回复所有人，包括专员的家人朋友。"""

    peer_denylist: str = ""
    """永远不处理这些会话（逗号分隔）。优先级高于白名单。"""

    # ── 守门（Phase B 使用）──
    max_sends_per_hour: int = 30
    """所有写动作合计的总闸。分档各自没超，加起来也不能没边。"""

    # 分动作配额（072，需求文档 §13.3）。只有一个总闸的话，「今天一条消息没发」
    # 就能换来「连加 30 个好友」——而加好友恰恰是风控最敏感的动作。
    max_messages_per_hour: int = 20
    """私聊一个人，每小时上限。"""

    max_group_messages_per_hour: int = 6
    """发到群里，每小时上限。比私聊严——群的误伤面大得多。"""

    max_friend_actions_per_hour: int = 5
    """加好友 + 通过好友申请，每小时上限。最招风控的一类，5 个已经不算保守。"""

    friend_interval_seconds: float = MIN_FRIEND_INTERVAL_SECONDS
    """两次**主动加好友**之间至少隔多久（秒）。配额之外的第二道闸：配额管总量、它管节奏。

    ★ 为什么要有它：2026-08-27 真机弹了微信安全验证，那天加好友一共才两次——
    15:22:36 和 15:23:57，隔 81 秒，两次都是「没搜到这个号」。只有配额的话，
    「5 次/小时」允许 5 次全挤进一分钟，而连着搜不到的陌生号正是枚举的形状。

    ★ 默认 300 秒就是那条线。**调小是往风险那侧走**，调小到 81 秒就是复现事故的节奏；
    真要调，自己清楚在做什么。`0` = 这道闸关掉，只剩配额。
    只管主动加（`add_friend`），不管通过别人发来的申请——那不用搜号。"""

    max_moments_per_day: int = 3
    """发朋友圈，每 **天** 上限——一小时发 3 条和一天发 3 条完全是两回事。"""

    max_moment_likes_per_day: int = 20
    """给朋友圈点赞，每 **天** 上限（用户 2026-09-07 定的 20）。只刷自己的时间线、只点今天的。"""

    moment_like_interval_seconds: float = 600.0
    """两趟朋友圈点赞之间至少隔多久（秒）。一趟 = 打开朋友圈刷一遍；连着开关窗口不像人。
    `0` = 这道闸关掉，只剩配额。"""

    max_moment_comments_per_day: int = 5
    """给朋友圈评论，每 **天** 上限（用户 2026-09-07：「做成可配置」，默认 5）。
    评论是公开文字，比点赞重一个量级，默认值刻意小。"""

    moment_comment_interval_seconds: float = 600.0
    """两趟朋友圈评论之间至少隔多久（秒）。`0` = 这道闸关掉，只剩配额。"""

    # ── 本机朋友圈巡看（用户 2026-09-08：「本地做一个，和轮询消息一样，只对最新的朋友圈进行」）──
    moments_poll_interval_seconds: float = 0.0
    """多久开一趟朋友圈看**新**动态（秒）。`0` = 不巡看（默认关：每趟都要开一次朋友圈窗口，
    是风控看得见的动作，开不开是专员的知情选择）。只碰上次看过之后新出的、今天的动态。"""

    moments_auto_like: bool = False
    """巡看时顺手给新动态点赞。配额与节拍走「朋友圈点赞」那两档。"""

    moments_auto_comment: bool = False
    """巡看到新动态时交给 AI 端按素材写评论（本机自己触发 AI 端，不经业务端计划）。
    配额与节拍走「朋友圈评论」那两档；同一条只评一次的台账照旧。"""

    moments_comment_material: str = "岗位、招工、上班生活相关的动态可以接一句；语气自然、简短，不硬推销"
    """自动评论的素材：想传达什么、什么口吻。清空 = 自动评论不生效（没素材 AI 端写不出有分寸的话）。
    给一句缺省口吻是为了界面上不出现空格子；真正的开关是 `moments_auto_comment`。"""

    # ── 对方发来的图片 / 文件（104）──
    inbound_attachments: bool = True
    """对方在微信里发来的图片、文件要不要另存到本机、随对话轮送给 AI 端（简历解析、证件识别）。

    关掉时只在上下文里告诉模型「对方发了一张图片」，不另存、不上传。另存要在聊天窗口里
    右键一次、过一个系统保存对话框，是风控看得见的动作之一；另存下来的文件是真实 PII，
    只在本机留 24 小时（`attachments.RETENTION_HOURS`）。"""

    active_hours: str = "09:00-21:00"

    allow_insecure_transport: bool = False
    """允许 ``http://`` / ``ws://`` 明文。默认拒绝——Bearer 凭证不得明文过网。"""

    def validate_for_start(self) -> None:
        if not self.ai_ws_url:
            raise ValueError("WXCLIENT_GATEWAY_AI_WS_URL 必填")
        if not self.ai_ws_url.startswith(("ws://", "wss://")):
            raise ValueError("AI_WS_URL 必须是 ws:// 或 wss://")
        if self.turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds 必须为正")
        if self.ticket_timeout_seconds <= 0:
            raise ValueError("ticket_timeout_seconds 必须为正")

        # 票据来源至少要有一个。业务端那条用的是**登录拿到的客户端 Token**，这里只校验
        # 地址——Token 不进配置（它在 DPAPI 里，见 `ui/token_store.py`）。
        #
        # ★ 2026-08-15：原来这里还要求两者**不能同时**配置（怕「票到底从哪来」有两个答案）。
        # 现在 business_api_url 有默认值、而且登录本身就走它，硬碰硬只会让所有配过联调
        # 票据文件的机器起不来。改成**明确的优先级**：联调文件在场就用它，
        # `build_ticket_provider` 那里会喊一句「仅供联调，生产禁用」。
        if not self.business_api_url and self.local_ticket_file is None:
            raise ValueError("必须配置 business_api_url 或 local_ticket_file 之一")

        if not self.allow_insecure_transport:
            if self.ai_ws_url.startswith("ws://") and not _is_loopback_url(self.ai_ws_url):
                raise ValueError("非回环的 AI_WS_URL 必须用 wss://（凭证不得明文过网）")
            if self.business_api_url.startswith("http://") and not _is_loopback_url(
                self.business_api_url
            ):
                raise ValueError("非回环的 BUSINESS_API_URL 必须用 https://")


def _is_loopback_url(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host.strip("[]").lower() in {"127.0.0.1", "localhost", "::1"}
