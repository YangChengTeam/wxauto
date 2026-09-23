"""微信侧的进出口：把队列任务的结果映射成 ``InboundMessage``，把回复投成发送任务。

**所有微信 UI 动作仍走 ``queue -> wechat_api``**（``CLAUDE.md`` §3）——这里只 submit，
一行都不碰窗口。

⚠️ 消息 id 的现实：微信 UI 读不到稳定的消息标识，`read_chat_history` 返回的每条只有
``{class_name, text, sender}``。所以「哪些是新消息」靠**游标签名**判定（见 ``sessions.py``），
``msg_id`` 只是给去重表用的单调序号，不承担判新职责。

⚠️ 本模块的字段映射来自对 `uia.py` 返回结构的静态阅读，**尚未在真机验证**。
真机联调（Phase C）时必须专门验这三条：
  ① 同一个人连发两条一模一样的消息，两条都要被处理；
  ② 第一次读一个会话，只接尾部今天、对方发的那几条，更早的历史不回溯；
  ③ 专员本人手打的消息 `sender == "我"`，要被标成 from_self。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

import structlog

from wxclient.queue import TaskStatus, TaskType

from .attachments import SAVABLE_TYPES as SAVABLE_ATTACHMENT_TYPES
from .inbound import InboundMessage
from .sessions import HUMAN_TAKEOVER, GatewayStore, text_key
from .watchlist import Watchlist

_log = structlog.get_logger(__name__)

SELF_SENDER = "我"

#: 一轮最多读几个会话。见 ``QueueMessageSource._due_peers``——每读一个就要开一次聊天窗口。
MAX_PEERS_PER_POLL = 8

#: 游标丢了时拿最近处理过的几条当锚，一次从记录里取多少条（见 ``split_new_messages`` 的 ``known``）。
#: 要比一窗读取量（``history_limit``，默认 20）大：判「这一窗是不是全是旧的」靠的是窗口和记录重叠。
KNOWN_KEYS_LIMIT = 60

#: 连续几轮读不到才算「这个会话真有问题」（092）。
#: 一轮读不到太常见了——微信卡一下、窗口没起来都会。拿一次失败就去加好友是误加。
UNREACHABLE_STRIKES = 3


def is_self_message(raw: dict[str, Any]) -> bool:
    """这条是不是专员本人发的。

    真机上权威字段是 ``is_self``（`uia.py` 按气泡左右位置判定，逐条都有）；``sender``
    只有部分行会被标注（多数行根本没这个键），所以只能当兜底。
    **判错的代价极大**：网关自己发出去的回复下一轮会被原样读回来，认不出是自己说的就会
    当成新入站再回一轮 —— 自问自答刷屏。
    """
    flag = raw.get("is_self")
    if isinstance(flag, bool):
        return flag
    return str(raw.get("sender") or "") == SELF_SENDER


def is_timestamp_row(raw: dict[str, Any]) -> bool:
    """聊天记录里夹的时间分隔行（「18:28」「昨天」），不是消息。"""
    return bool(raw.get("is_timestamp"))


#: 语音气泡转写还没出来时，最多等几轮轮询。微信的转写是服务端做的，通常一两秒；
#: 等了这么多轮还没有就当它转不出来（没说话、方言、网络），别让后面的消息一直压着。
VOICE_TRANSCRIPT_STRIKES = 3

#: 放弃等转写后喂给 AI 的正文。要让模型知道「对方发了语音但听不到内容」，
#: 它才会请对方打字，而不是对着一句空话接茬。
VOICE_UNTRANSCRIBED_TEXT = "[语音消息，微信没能转成文字]"


def is_pending_voice(raw: dict[str, Any]) -> bool:
    """语音气泡、转写正文还没出来（`uia._annotate_voice_row` 标的）。"""
    return bool(raw.get("transcript_pending"))


def hold_pending_voice(
    raw_messages: Sequence[dict[str, Any]],
    waits: dict[tuple[str, str], int],
    peer_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """转写没出来的语音：从它起整段先不处理，返回 (可处理的前段, 被压住的条数)。

    只能整段压住、不能单独跳过它：游标要是越过它推到后面的消息上，下一轮转写出来了，
    它已经在游标前面——永远不会被当成新消息。压住的代价是它后面的消息晚一两轮，
    轮询本来就是分钟级的。

    连续 ``VOICE_TRANSCRIPT_STRIKES`` 轮还没出来就不等了：正文写成
    ``VOICE_UNTRANSCRIBED_TEXT`` 照常处理。放弃的记录**不清**（计数停在上限）：
    微信转不出来的那条以后每轮都还是「未就绪」，清了就会每三轮压一次后面的消息。
    """
    kept: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_messages):
        if not is_pending_voice(raw):
            kept.append(raw)
            continue
        key = (peer_id, str(raw.get("voice_label") or ""))
        strikes = min(waits.get(key, 0) + 1, VOICE_TRANSCRIPT_STRIKES)
        waits[key] = strikes
        if strikes < VOICE_TRANSCRIPT_STRIKES:
            return kept, len(raw_messages) - index
        raw = dict(raw, text=VOICE_UNTRANSCRIBED_TEXT, transcript_pending=False)
        kept.append(raw)
    return kept, 0


def message_signature(raw: dict[str, Any], dup_index: int = 0) -> str:
    """一条消息的签名（游标用）。没有稳定 id，只能拿文本凑。

    ``dup_index``（104）：这条之前、同一窗口里已有几条**同文**的行。图片气泡读出来
    全是「图片」两个字——对方连发两张身份证正反面，游标停在第一张上，下一轮从后往前找
    会先撞上第二张，第二张就永远不算新的了。把「第几个同文」算进签名，两张就分得开。
    首个（``dup_index=0``）的签名与旧算法一致，库里已有的游标不受影响。

    ★ **绝不能把 `sender` / `is_self` 算进来**（2026-08-13 真机血的教训）：
    `uia.py::read_current_chat` 只给**最后一条**消息补这两个字段（靠会话预览文案 +
    右键菜单探测），其余行压根没有。而游标存的正是最后一条的签名——下一轮它不再是
    最后一条、不再被补字段，同一条消息就算出**另一个**哈希：

        21:25 存游标 aeea6b8f…（那时它是最后一条，sender 被补成「我」）
        21:31 同一条消息再读 → 没人补字段 → 算出别的哈希 → 「游标已滚出读取窗口」

    于是游标**永远**对不回来，`fresh` 永远是空，**自动回复一次都触发不了**
    （真机实证：`gateway_cursors.seq` 一直是 0、`gateway_sessions` 一条没有）。
    文本是唯一两次读取都稳定的字段，就只拿它。同文重复的代价是定位到最后一次出现，
    比"永远定位不到"轻得多。
    """
    label = str(raw.get("voice_label") or "")
    if label:
        # 语音气泡：转写出来之前 text 是空、出来之后是正文，同一条消息两种 text。
        # 只有「语音12"秒」这段前后不变，签它。
        return sha256(f"voice\x00{label}".encode()).hexdigest()[:32]
    text = str(raw.get("text") or "")
    if dup_index > 0:
        return sha256(f"dup{dup_index}\x00{text}".encode()).hexdigest()[:32]
    return sha256(text.encode()).hexdigest()[:32]


def dup_indexes(raw_messages: Sequence[dict[str, Any]]) -> list[int]:
    """每一行在这个窗口里是第几条同文（语音按标签算）。见 `message_signature`。"""
    seen: dict[str, int] = {}
    out: list[int] = []
    for raw in raw_messages:
        key = f"voice\x00{raw.get('voice_label')}" if raw.get("voice_label") else str(raw.get("text") or "")
        index = seen.get(key, 0)
        out.append(index)
        seen[key] = index + 1
    return out


def legacy_message_signature(raw: dict[str, Any]) -> str:
    """旧签名（含 sender/is_self）。**只用于匹配**库里已有的老游标，不再写入。"""
    sender = "self" if is_self_message(raw) else str(raw.get("sender") or "peer")
    text = str(raw.get("text") or "")
    return sha256(f"{sender}\x00{text}".encode()).hexdigest()[:32]


def cursor_candidates(raw: dict[str, Any], dup_index: int = 0) -> set[str]:
    """这一行**可能**被记成游标的所有签名形态。

    老游标是在「它当过最后一条、被补了 `sender='我'` / `is_self=True`」的那一刻算的，
    而现在再读到它时那些字段没了——所以光按当前字段算旧签名也认不回来，
    得把「当作自己发的」那一版也一并认掉。否则换算法当天每个会话都要先白丢一条消息。
    """
    text = str(raw.get("text") or "")
    return {
        message_signature(raw, dup_index),
        # 老游标不带同文序号（104 之前）；同文的后几条也认一次，代价只是多认到一个位置。
        message_signature(raw),
        legacy_message_signature(raw),
        sha256(f"self\x00{text}".encode()).hexdigest()[:32],
    }


def _anchor_index(keys: Sequence[str], anchors: Sequence[str]) -> int:
    """最近处理过的那条（``anchors[-1]``）在这一窗里最后一次出现的位置；找不到 -1。

    有两条锚时要求它前一行也对得上（``anchors[-2]``）：对方或我们后来又说了一句一模一样的话
    （「好的」「收到」），单看一行会对到那条**新**的上，把中间真正没处理的几条跳过去。
    锚正好是窗口第一行时验不了前一行，认它——窗口是往上翻出来的，前面本来就该是更老的。
    """
    if not anchors:
        return -1
    for index in range(len(keys) - 1, -1, -1):
        if keys[index] != anchors[-1]:
            continue
        if len(anchors) >= 2:
            # 前一行要跳过空文本行（没转写出来的语音、空气泡）——它们没被记录过，比不上。
            previous = next((k for k in reversed(keys[:index]) if k), None)
            if previous is not None and previous != anchors[-2]:
                continue
        return index
    return -1


def split_new_messages(
    raw_messages: Sequence[dict[str, Any]],
    last_signature: str | None,
    known: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], str | None]:
    """按游标切出新消息，返回 (新消息, 新游标签名)。

    - 游标为空（第一次读这个会话）→ 只取**尾部今天的、对方发的**那几条，见 ``first_seen_fresh``。
    - 游标找不到 → 先拿 ``known``（这台机器**处理过**的最近几条消息在微信里的内容键，老→新，
      见 `GatewayStore.recent_raw_keys`）当第二道锚：
        · 最近处理的那条在窗口里 → 它之后的才是新的；
        · 它不在，但窗口最后一行是处理过的 → 这一窗是**往上翻出来的旧消息**，一条不接、
          **游标也不动**（动了就是把游标拽回旧位置，下一趟把整段旧对话当新消息重跑一遍——
          2026-09-23 线上：候选人 09:05–09:09 的问题在 09:23 被原样重播、又回了一遍）；
        · 都对不上（历史真的滚出了读取窗口）→ 老规矩：不回溯，只对齐游标。
    """
    if not raw_messages:
        return [], last_signature

    dups = dup_indexes(raw_messages)
    latest_signature = message_signature(raw_messages[-1], dups[-1])
    if last_signature is None:
        return first_seen_fresh(raw_messages), latest_signature

    # 从后往前找游标最后一次出现的位置。先按带同文序号的新签名精确找（104：连发两张
    # 「图片」时停在正确的那一张），找不到再按老签名认（库里 104 之前记的游标）。
    cursor_index = -1
    for index in range(len(raw_messages) - 1, -1, -1):
        if last_signature == message_signature(raw_messages[index], dups[index]):
            cursor_index = index
            break
    if cursor_index < 0:
        for index in range(len(raw_messages) - 1, -1, -1):
            if last_signature in cursor_candidates(raw_messages[index], dups[index]):
                cursor_index = index
                break

    if cursor_index < 0 and known:
        # 两边都过一遍 `text_key`：记录里存的已经是键，但调用方也可能直接给原文（测试、老行）。
        keys = [text_key(raw.get("text")) for raw in raw_messages]
        known = [text_key(k) for k in known if text_key(k)]
        anchor = _anchor_index(keys, known[-2:])
        if anchor >= 0:
            _log.info(
                "游标在刚读到的记录里找不到，按最近处理过的那条对上了位置，接着往后接",
                已读到=len(raw_messages),
                新消息=len(raw_messages) - anchor - 1,
            )
            return list(raw_messages[anchor + 1:]), latest_signature
        if keys and keys[-1] in set(known):
            _log.warning(
                "读到的这一窗全是处理过的旧消息（聊天窗口可能被翻上去了），一条不接、游标不动",
                已读到=len(raw_messages),
            )
            return [], last_signature

    if cursor_index < 0:
        # 这条**不是**日常噪音：正常情况下游标就在刚读到的这批里。连着出现 =
        # 签名不稳定或读取窗口太小，后果是自动回复永远不触发（2026-08-13 真机踩过），
        # 所以报警告级别，别让它混在 info 里没人看见。
        _log.warning(
            "游标在刚读到的记录里找不到，只对齐游标不回溯；连着出现说明游标机制失效了",
            已读到=len(raw_messages),
        )
        return [], latest_signature

    return list(raw_messages[cursor_index + 1:]), latest_signature


#: 第一次读一个会话时，最多把尾部几条当成待处理。够装下「连发三条」那种，
#: 又不至于把一屏历史整个倒给模型。
FIRST_SEEN_TAIL = 5


def first_seen_fresh(raw_messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """第一次读这个会话时，哪些算「要处理的新消息」。

    ★ 原来是**一条都不算**，只对齐游标——理由是「网关刚上线不能对着几个月历史逐条回复」。
    可那是「先扫未读、被动等消息」时代的设定。现在是名单驱动、直接读会话：把一个人
    加进名单的那一刻，他很可能刚问完话正等着，而「首次不回」意味着他永远等不到——
    除非他再发一条（用户 2026-08-14：「没有首次见到这一说」）。

    两边都要：不倒着回历史，也不能把刚问的话咽掉。判据就是**时间 + 谁说的**——
    只认尾部连续的、今天的、对方发的那几条：

    - 尾部是专员自己发的 → 停（他已经在人工接着了，轮不到分身插话）；
    - 遇到不是今天的 → 停（昨天以前的都是历史，不回溯）；
    - 最多 ``FIRST_SEEN_TAIL`` 条。

    没有时间分隔行时 ``timestamp_is_today`` 是 None —— 当今天处理（宁可回一条稍旧的，
    也好过把刚到的消息咽掉；真正的老历史前面必然夹着日期分隔行）。
    """
    fresh: list[dict[str, Any]] = []
    for raw in reversed(raw_messages):
        if is_self_message(raw):
            break
        if raw.get("timestamp_is_today") is False:
            break
        fresh.append(raw)
        if len(fresh) >= FIRST_SEEN_TAIL:
            break
    fresh.reverse()
    return fresh


#: 日志里带上消息内容时截到多长。够看出「是哪条消息」，又不至于把整篇简历刷进实时日志。
PREVIEW_CHARS = 40


def preview(raw: dict[str, Any]) -> str:
    """消息内容的日志摘要。

    ⚠️ 这会把聊天内容写进本机日志文件——是专员自己那台机器上的运维日志，
    也正是用户要的（「最好把最近的消息日志也打印到实时日志中」）。仍然截断：
    日志不是聊天记录归档，`.wxclient/` 整个目录本来就禁止入库（CLAUDE.md §7）。
    """
    text = " ".join(str(raw.get("text") or "").split())
    return text[:PREVIEW_CHARS] + "…" if len(text) > PREVIEW_CHARS else text or "（空）"


#: 表情 / 视频 / 拿不到的图片文件，喂给 AI 端的占位正文（104）。方括号写法与 AI 端
#: `context.mjs` 的 `FILLER_RE` 同口径——表情那条要让它认出「这不是一句话」。
EMOJI_TEXT = "[动画表情]"
VIDEO_TEXT = "[对方发来一段视频，助手看不了视频]"
IMAGE_UNSAVED_TEXT = "[对方发来一张图片，没能保存下来]"
FILE_UNSAVED_TEXT = "[对方发来一个文件{name}，没能保存下来]"
IMAGE_ATTACHED_TEXT = "[图片：{name}（已附上）]"
FILE_ATTACHED_TEXT = "[文件：{name}（已附上）]"


def attachment_text(raw: dict[str, Any], attachment: dict[str, Any] | None) -> str:
    """图片 / 文件 / 表情 / 视频气泡在上下文里的正文。文字消息原样返回。"""
    kind = str(raw.get("message_type") or "text")
    if kind == "emoji":
        return EMOJI_TEXT
    if kind == "video":
        return VIDEO_TEXT
    if kind == "image":
        return IMAGE_ATTACHED_TEXT.format(name=attachment["file_name"]) if attachment else IMAGE_UNSAVED_TEXT
    if kind == "file":
        if attachment:
            return FILE_ATTACHED_TEXT.format(name=attachment["file_name"])
        name = str(raw.get("file_name") or "").strip()
        return FILE_UNSAVED_TEXT.format(name=f"「{name}」" if name else "")
    return str(raw.get("text") or "")


def to_inbound(
    account_id: str,
    peer_id: str,
    raw: dict[str, Any],
    *,
    seq: int,
    at: datetime | None = None,
    attachment: dict[str, Any] | None = None,
) -> InboundMessage:
    return InboundMessage(
        account_id=account_id,
        peer_id=peer_id,
        msg_id=f"{peer_id}#{seq}",
        text=attachment_text(raw, attachment),
        at=at or datetime.now(timezone.utc),
        from_self=is_self_message(raw),
        from_gateway=bool(raw.get("gateway_echo")),
        peer_name=peer_id,
        at_text=str(raw.get("timestamp_text") or "").strip(),
        message_type=str(raw.get("message_type") or "text"),
        attachments=(attachment,) if attachment else (),
        raw_text=str(raw.get("text") or ""),
    )


class QueueMessageSource:
    """入站来源：**名单里的会话** → 逐个读聊天记录 → 按游标切出新消息。

    ``peer_id`` 用的是**微信会话名**——微信 UI 自动化能拿到的就是这个，
    ``wechat_api`` 的所有动作也都按会话名寻址。
    """

    def __init__(
        self,
        *,
        runtime,  # WeChatRuntime 或任何提供 submit(TaskType, payload) 的对象
        store: GatewayStore,
        watchlist: Watchlist | None = None,
        history_limit: int = 20,
        clock=None,
        on_unreachable=None,
        attachment_fetcher=None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._watchlist = watchlist or Watchlist()
        self._history_limit = history_limit
        # 104：对方发来的图片/文件另存到本机的那一手。None = 不另存（只在正文里说「发了张图」）。
        self._attachments = attachment_fetcher
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rotation = 0
        # 092：连续 UNREACHABLE_STRIKES 轮读不到时喊一声。**只是「读不到」，不是「不是好友」**
        # ——判定留给回调那边（`autofriend.py`），这里一行都不管策略。
        self._on_unreachable = on_unreachable
        # 会话名 → 连续读不到了几轮（092：从 set 改成计数）
        self._unreachable: dict[str, int] = {}
        # (会话名, 语音标签) → 转写等了几轮（见 `hold_pending_voice`）
        self._voice_waits: dict[tuple[str, str], int] = {}
        # 上一轮因人工接管被跳过的会话，只为「变了才喊一声」（见 `_drop_handed_off`）
        self._paused_peers: set[str] = set()

    async def fetch(self, account_id: str) -> list[InboundMessage]:
        """读名单里的会话，按游标切出新消息。

        ★ **不看微信的未读**（用户裁决 2026-08-14）。未读这个信号根本不可靠：
        群多半设了消息免打扰、不进未读；专员在手机上瞄过一眼的也不再是未读——
        于是「该回的消息网关压根看不见」。名单本来就是「要盯的会话」清单，
        直接读它、按游标判新，跟「微信觉得你读没读过」彻底脱钩。
        """
        # 名单空着 = 一个会话都不碰。这时候必须喊一嗓子，否则现场表现是
        # 「微信有新消息、网关一声不吭」，看的人只会以为功能坏了（069）。
        self._watchlist.warn_if_idle()
        watched = self._watchlist.watched()
        if not watched:
            return []
        watched = self._drop_handed_off(account_id, watched)
        if not watched:
            return []

        collected: list[InboundMessage] = []
        for peer_id in self._due_peers(watched):
            collected.extend(await self._fetch_peer(account_id, peer_id))
        return collected

    def _drop_handed_off(self, account_id: str, watched: Sequence[str]) -> list[str]:
        """人工接管中的候选人，这一轮连读都不读（用户裁决 2026-08-28）。

        ★ 这是**改过一次口径**的地方，别照着旧注释改回去。原来接管只挂写侧
          （`guard.OutboundGuard._takeover_denial`），读照旧——理由是「人真回了话，
          后端那条『未回复跟进』照样要被取消」。现在按用户的口径改成读也停：
          招聘老师自己坐下来聊的时候，助手不该再去开他那个聊天窗口
          （开窗会抢焦点、清未读，还白吃一个读名额）。

        ★ **代价说清楚**：接管期间不再有 `候选人回话` 那条消息事件上报
          （`runner._note_inbound_messages` 是轮询喂的），后端的「未回复跟进」
          在这段时间里不会被对方的回话取消。这是这次口径变更的已知后果，不是 bug。

        交回分身时游标会被抹掉（`GatewayStore._forget_cursor`），所以恢复之后
        不会把接管期间那一堆当新消息倒给模型。
        """
        kept: list[str] = []
        paused: list[str] = []
        for peer_id in watched:
            if self._store.candidate_mode(account_id, peer_id).mode == HUMAN_TAKEOVER:
                paused.append(peer_id)
            else:
                kept.append(peer_id)
        # 只在名单变化时喊一次：轮询是分钟级的，每轮一条会把实时日志刷满，
        # 而这条信息（谁被接管了、什么时候开始的）只有在变的那一刻才有用。
        if paused and set(paused) != self._paused_peers:
            _log.info("这些会话正在人工接管，这轮不读", 数量=len(paused))
        elif self._paused_peers and not paused:
            _log.info("人工接管都交回来了，恢复读取")
        self._paused_peers = set(paused)
        return kept

    def _due_peers(self, watched: Sequence[str]) -> list[str]:
        """这一轮读哪几个。名单长了就轮着来，一轮最多 ``MAX_PEERS_PER_POLL`` 个。

        读一个会话 = 打开一次聊天窗口。名单有五十个人时，每轮把五十个窗口挨个点一遍
        既慢又正是风控最不喜欢的形状（2026-08-12 自动退登事故）。轮转的代价只是
        「名单很长时，某个人的消息最多晚几轮被看到」——比被封号强得多。
        """
        if len(watched) <= MAX_PEERS_PER_POLL:
            return list(watched)
        start = self._rotation % len(watched)
        self._rotation = start + MAX_PEERS_PER_POLL
        doubled = list(watched) + list(watched)
        due = doubled[start:start + MAX_PEERS_PER_POLL]
        _log.info("名单较长，本轮只读其中一段", 名单=len(watched), 本轮=len(due))
        return due

    def set_unreachable_handler(self, handler) -> None:
        """装上「连续几轮读不到就喊一声」的回调（092）。

        构造时装不上：喊话的对象（`AutoFriendWatcher`）要用到 `CommandExecutor`，
        而那个是 runner 拼起来的，比本对象晚。所以留一个装配口，别让 runner 戳私有属性。
        """
        self._on_unreachable = handler

    def _claim_own_echoes(
        self, account_id: str, peer_id: str, raw_messages: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """把「我们自己刚发过的原文」标成自己发的，不管 UI 怎么判。

        判据是确定性的：文本跟 `GatewayStore.remember_sent_text` 记下的某一条一致，
        且那条还没被认领过（认一次销一条，见 `claim_sent_echo`）。UI 已经判成自己的
        也照样销账——否则那条记录会留到对方哪天复读同一句时把真话吞掉。
        """
        out: list[dict[str, Any]] = []
        for raw in raw_messages:
            if not self._store.claim_sent_echo(account_id, peer_id, str(raw.get("text") or "")):
                out.append(raw)
                continue
            if not is_self_message(raw):
                _log.info(
                    "这条是自己刚发出去的回复被读回来了，按自己说的算（UI 没认出来）",
                    会话=peer_id,
                    内容=preview(raw),
                )
            # `gateway_echo`：这是**网关替分身发出去**的那句（台账认亲），不是专员手打的——
            # AI 端按这个标成「分身」，否则会当成招聘老师插话（2026-09-19）。
            out.append(dict(raw, is_self=True, sender=SELF_SENDER, gateway_echo=True))
        return out

    async def _fetch_peer(self, account_id: str, peer_id: str) -> list[InboundMessage]:
        history = await self._runtime.submit(
            TaskType.READ_CHAT_HISTORY, {"friend": peer_id, "limit": self._history_limit}
        )
        # 读不到要说清楚是**哪个名字**读不到。名单里的名字必须和微信里显示的**一字不差**
        # （网关按会话名寻址），差一个空格就是「查无此人」——而这条错误如果只落在队列日志里，
        # 现场表现就成了「名单加了、可他就是不回」，谁也想不到是名字对不上（2026-08-14 实测）。
        if str((history or {}).get("status") or "") not in {"", "succeeded"}:
            reason = str((history or {}).get("last_error") or "").strip()
            strikes = self._unreachable.get(peer_id, 0) + 1
            self._unreachable[peer_id] = strikes
            # 只在「刚开始读不到」时喊一次：轮询是分钟级的，每轮一条会把日志刷满，
            # 真正有用的信息（第一次是什么时候、为什么）反而被埋掉。
            if strikes == 1:
                _log.warning("名单里的这个会话读不到，先跳过", 会话=peer_id, 原因=reason[:120])
            # 092：连续几轮都读不到才喊。**喊的是「读不到」，不是「不是好友」**——
            # 这条分支任何读失败都会走（名字差一个字、微信卡了、窗口打不开），
            # 拿它直接去加好友就是误加。到底是不是好友由回调那边再确定性判一次。
            if strikes == UNREACHABLE_STRIKES and self._on_unreachable is not None:
                await self._on_unreachable(account_id, peer_id, reason)
            return []
        if self._unreachable.pop(peer_id, 0):
            _log.info("名单里的这个会话又能读了", 会话=peer_id)
        raw_messages = ((history or {}).get("result") or {}).get("messages") or []
        # 时间分隔行必须在切游标**之前**滤掉：它会被当成一条消息发给 AI，还会把游标钉在
        # 一个随时间变化的字符串上（真机读回来过一行「18:28」）。
        raw_messages = [raw for raw in raw_messages if not is_timestamp_row(raw)]
        # 语音气泡转写还没出来：从它起先压住，游标停在它前面，下一轮再看（见 hold_pending_voice）。
        raw_messages, held = hold_pending_voice(raw_messages, self._voice_waits, peer_id)
        if held:
            _log.info("有语音还没转成文字，这段先不处理，下一轮再看", 会话=peer_id, 压住=held)

        # 是不是群只有打开会话才判得出来（未读列表里没有这个信息）。记下来只为诊断和日志——
        # **不再拿它决定处理与否**（069）：群和单聊一样都只看监听名单。
        if any(raw.get("is_group") for raw in raw_messages):
            self._store.mark_peer_kind(account_id, peer_id, is_group=True)

        last_signature, seq = self._store.get_cursor(account_id, peer_id)
        # ★ 自己发过的原文读回来要认亲（2026-09-09 真机：自己的回复被当成对方的新消息，
        #   自问自答）。第一次读这个会话时整段都还没处理过，整段认；有游标时只认游标
        #   之后的那几条——游标之前的早已处理，再拿它们销账会把后面真正的回声漏掉。
        if last_signature is None:
            raw_messages = self._claim_own_echoes(account_id, peer_id, raw_messages)
        # 游标找不到时的第二道锚：这台机器处理过的最近几条在微信里长什么样（见 split_new_messages）。
        known = (
            self._store.recent_raw_keys(account_id, peer_id, limit=KNOWN_KEYS_LIMIT)
            if last_signature is not None else ()
        )
        fresh, new_signature = split_new_messages(raw_messages, last_signature, known)
        if last_signature is not None:
            fresh = self._claim_own_echoes(account_id, peer_id, fresh)
        if last_signature is None and raw_messages:
            # 第一次读这个会话：只接尾部今天、对方发的那几条（见 first_seen_fresh），
            # 更早的算历史不回溯。**必须说出来**——不然日志上就是「读完了 N 条」然后
            # 再无下文，看的人只会以为回复功能坏了（2026-08-13 真机，用户就是这么问的）。
            _log.info(
                "第一次读这个会话，只接尾部今天、对方发的那几条，更早的算历史不回溯",
                account=account_id,
                peer=peer_id,
                已读到=len(raw_messages),
                要处理=len(fresh),
            )
        if new_signature is not None and (new_signature != last_signature or fresh):
            self._store.set_cursor(account_id, peer_id, new_signature, seq + len(fresh))

        # ★ 读完必须说清楚「读到几条、其中几条算新的、新的都是谁说的什么」（用户 2026-08-14：
        # 「我不知道你为什么不回复消息给监听的名单」）。原来读完只有一句「读取完成 N 条」，
        # 之后要么回复、要么无声无息——现场根本分不清是「没有新消息」「新消息是自己发的」
        # 还是「哪一环坏了」。这几行是排查这条链路唯一的抓手。
        _log.info(
            "读完这个会话",
            会话=peer_id,
            读到=len(raw_messages),
            新消息=len(fresh),
            最近一条=preview(raw_messages[-1]) if raw_messages else "（空）",
        )
        for raw in fresh:
            _log.info(
                "新消息",
                会话=peer_id,
                谁=SELF_SENDER if is_self_message(raw) else "对方",
                时间=str(raw.get("timestamp_text") or "").strip() or "-",
                内容=preview(raw),
            )

        now = self._clock()
        inbound: list[InboundMessage] = []
        for offset, raw in enumerate(fresh):
            if not str(raw.get("text") or "").strip():
                continue
            attachment = None
            # 104：对方发来的图片/文件先另存到本机，随这一轮送给 AI 端。只取对方发的——
            # 专员自己发出去的合同、海报不用回头再读一遍。
            if (
                self._attachments is not None
                and str(raw.get("message_type") or "") in SAVABLE_ATTACHMENT_TYPES
                and not is_self_message(raw)
            ):
                attachment = await self._attachments.fetch(account_id, peer_id, raw)
            inbound.append(
                to_inbound(account_id, peer_id, raw, seq=seq + offset + 1, at=now, attachment=attachment)
            )
        return inbound


class SendNotConfirmed(RuntimeError):
    """发送任务没走到 ``succeeded``（失败、被拦、或等超时了）。

    **「没抛异常」不等于「话发出去了」**：`runtime.submit(wait=True)` 只是等到任务进入终态，
    终态里既有 succeeded 也有 failed/blocked/cancelled，超时更是压根没有结论。原来这三种
    一律照常返回 task_id，于是配额按发了记、消息事件也会按发了报——后端从此按一条根本
    没到对方手机上的消息开始算「多久没回」。结论不明就当没发（需求文档 §12.4：
    结果未知先查询或转人工，不盲目重发）。
    """


class QueueMessageSink:
    """出站投递：把一段回复投成 ``SEND_MESSAGE`` 任务，由单消费者 worker 串行发出。

    ``allow_real_actions=False``（默认）时投的是 ``dry_run`` 任务——走完整条链路
    但不真的发出去。联调期就用这个模式。
    """

    def __init__(self, *, runtime, allow_real_actions: bool = False, confirmation: str = "") -> None:
        self._runtime = runtime
        self._allow_real = allow_real_actions
        self._confirmation = confirmation

    async def send(self, account_id: str, peer_id: str, text: str) -> str:
        payload = {"friend": peer_id, "text": text, "dry_run": not self._allow_real}
        result = await self._runtime.submit(
            TaskType.SEND_MESSAGE,
            payload,
            confirmation=self._confirmation if self._allow_real else "",
        )
        task_id = str((result or {}).get("task_id") or "")
        status = str((result or {}).get("status") or "")
        if status != TaskStatus.SUCCEEDED.value:
            _log.warning(
                "发送任务没有确认成功",
                account=account_id,
                status=status or "unknown",
                timed_out=bool((result or {}).get("timed_out")),
                task_id=task_id,
            )
            raise SendNotConfirmed(f"发送任务未确认成功（status={status or 'unknown'}）")
        _log.info(
            "已投递发送任务",
            account=account_id,
            dry_run=not self._allow_real,
            task_id=task_id,
        )
        return task_id
