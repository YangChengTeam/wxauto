"""入站来源：**读本地微信库**判新消息，不再逐个开聊天窗口轮询。

这是 `wechat_io.QueueMessageSource`（UIA 开窗轮询）的替身，只在 Windows、密钥扫到、
单账号时启用；扫不到 / 版本不对 / 账号对不上就回退到那条 UIA 链路（见 `factory`）。

**只换「怎么发现新消息」，不动节拍**（用户 2026-11-03）：去抖、工作时间窗、发送频次、
抖动全在下游，一点没碰。省掉的只是「为了看有没有新消息而开一堆窗口」——本地库 1 秒就
知道谁来了消息，省下来的窗口留给真要动微信的动作（发送、加好友）。

## 一个会话，同一时刻只归一条链路（不混读，避免重复或漏回）

- **文本**：直接从库里读，游标是 `local_id`（稳定自增，不像 UI 那条文本签名游标会漂），
  自己/对方靠 `Name2Id` 认（真机权威），所以 UI 那边「自己发的被当成对方新消息」的
  自问自答在这条链路上根本不存在。
- **语音 / 图片 / 文件 / 视频**：库里只有类型、没有转写和附件原文。这类消息一出现，
  **这个会话这一轮整段委回 UIA**（`QueueMessageSource.read_peer`）——语音转写、附件
  另存、查无此人计数都照 UIA 那套走。委回时 `local_id` 游标基线前移，两条链路不会
  同时对同一条消息出手。
- **首次接入 / 名字对不上 / 名单外红点**：一律交 UIA（首见只认尾部今天、对方发的那几条；
  名字对不上触发加好友；红点扫描是名单外的窄路）。

## 待真机验收

文本直读、`local_id` 游标、委回边界已有 Linux 离线测试覆盖（fixtures 明文库）。
**真实微信的解密读取、语音/图片委回、风控表现仍需 Windows 真机联调**，离线测试不等于真机可用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone

import structlog

from .. import logrole
from .inbound import InboundMessage
from .sessions import HUMAN_TAKEOVER, GatewayStore
from .watchlist import Watchlist
from .wechat_io import FIRST_SEEN_TAIL, QueueMessageSource, preview, to_inbound

_log = structlog.get_logger(__name__)
_local = _log.bind(**{logrole.ROLE_KEY: logrole.LOCAL})

#: 多久看一次本地库（秒）。读库微信看不见，不受开窗轮询的 30/180 秒地板管；界面上的「读消息
#: 间隔」0.1.128 起删了。库没变时 `sessions()` 复用上次的连接，一轮只是 stat 几个文件。
DB_POLL_INTERVAL_SECONDS = 3.0

#: 本地库没连上时多久再试一次（秒）。扫钥在客户端里偶尔会被别的启动线程拖过时限（真机
#: 2026-11-04 21:32：单跑 4 秒，客户端里超了 60 秒），一次失败不能整场都退回 UIA。
ATTACH_RETRY_SECONDS = 60.0

#: 第一轮等本地库连上最多多久（秒）。子进程扫钥一般十几秒；先等一等，别一启动就让 UIA
#: 顶一轮「汇总未读消息」——那一轮要开窗、拉前台、跑一分钟（真机 2026-11-04 22:04）。
FIRST_ATTACH_WAIT_SECONDS = 30.0

#: 一个会话一轮最多往日志里打几条新消息（突然冒出一大段时别把日志刷爆）。
LOG_MESSAGES_PER_SESSION = 10

_TYPE_LABELS = {"text": "文字", "image": "图片", "voice": "语音", "video": "视频", "emoji": "表情", "file": "文件/链接"}

#: `gateway_db_cursors` 里的哨兵：这个会话当前归 UIA 读（有未消化的语音/图片/文件等），
#: 本地库这一侧先不碰它，每轮委回 UIA；等它最后一条又变回文本、UIA 也追平了，再交回本地库。
_UI_OWNED = -1


class DbMessageSource:
    """按本地库判新消息的入站来源，接口与 `QueueMessageSource` 一致（`MessageSource`）。"""

    def __init__(
        self,
        *,
        reader=None,  # wechat_db.WeChatDbReader（鸭子类型，避免在非 Windows 上导入 ctypes 路径）
        attach=None,  # () -> reader | None，阻塞（扫钥几秒到几十秒），放线程里跑；reader 为 None 时用
        store: GatewayStore,
        ui_source: QueueMessageSource,
        watchlist: Watchlist | None = None,
        clock=None,
        read_unread_strangers: bool = True,
    ) -> None:
        self._reader = reader
        self._attach = attach
        self._attach_task: asyncio.Task | None = None
        # 本地库连上之前 UIA 已经替它跑过几轮：连上后第一轮把名单里各会话的游标直接基线到
        # 最新，不走首见切尾部——那几条 UIA 已经读过、可能也回过了，再出一遍就是重复回复。
        self._ui_rounds_before_attach = False
        self._store = store
        # UIA 替身：首次读、含媒体那一轮、名字对不上、名单外红点都委给它。
        self._ui = ui_source
        self._watchlist = watchlist or Watchlist()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._read_unread = bool(read_unread_strangers)
        self._round = 0
        self._paused_peers: set[str] = set()
        # 上一轮各会话最后一条的 local_id；名单外单聊涨了才去 UIA 扫红点（None = 还没建基线）。
        self._seen_sessions: dict[str, int] | None = None
        # 归 UIA 的会话上次委回时库里最后一条的 local_id：没涨就不再开窗（3 秒一轮，
        # 不能为一个停在图片上的会话每轮开一次窗）。只在内存里，重启后至多多委回一次。
        self._ui_owned_seen: dict[str, int] = {}

    # MessageSource 里 runner 会装的两个回调，直接透传给 UIA 替身（查无此人 / 读不到要去加好友）。
    def set_unreachable_handler(self, handler) -> None:
        self._ui.set_unreachable_handler(handler)

    def set_not_found_handler(self, handler) -> None:
        self._ui.set_not_found_handler(handler)

    def set_hot_window(self, seconds: float) -> None:
        self._ui.set_hot_window(seconds)

    def set_full_interval(self, seconds: float) -> None:
        self._ui.set_full_interval(seconds)

    @property
    def fixed_poll_interval_seconds(self) -> float | None:
        """`InboundLoop` 认这个：连上本地库就按固定节拍跑，不走快慢档和 AI 端下发的节拍；
        还没连上（暂由 UIA 顶着）就返回 None，走 UIA 的节拍——别让开窗轮询跟着 3 秒一轮。"""
        return DB_POLL_INTERVAL_SECONDS if self._reader is not None else None

    def _ensure_attaching(self) -> None:
        """后台一直试着连本地库，连上为止（每 `ATTACH_RETRY_SECONDS` 一次，扫钥在线程里跑）。"""
        if self._attach is None or (self._attach_task is not None and not self._attach_task.done()):
            return

        async def keep_trying() -> None:
            attempt = 0
            while self._reader is None:
                attempt += 1
                try:
                    reader = await asyncio.to_thread(self._attach)
                except Exception as exc:  # noqa: BLE001
                    reader = None
                    _local.warning("连本地微信库出错", error=str(exc)[:160])
                if reader is not None:
                    self._reader = reader
                    _local.info("本地库已连上，入站切到本地库监听", 第几次=attempt)
                    return
                _local.info("本地库还没连上，暂由 UIA 轮询顶着，稍后再试", 第几次=attempt, 秒后重试=ATTACH_RETRY_SECONDS)
                await asyncio.sleep(ATTACH_RETRY_SECONDS)

        self._attach_task = asyncio.get_running_loop().create_task(keep_trying())

    async def fetch(self, account_id: str) -> list[InboundMessage]:
        if self._reader is not None and getattr(self._reader, "alive", True) is False:
            # 子进程卡死被杀 / 自己退了：丢掉它，后台重连，期间 UIA 顶着；连回来重建基线。
            _local.warning("本地库子进程没了，暂由 UIA 顶着，后台重连")
            self._reader = None
            self._seen_sessions = None
        if self._reader is None and self._attach_task is None and self._attach is not None:
            self._ensure_attaching()
            try:
                await asyncio.wait_for(asyncio.shield(self._attach_task), FIRST_ATTACH_WAIT_SECONDS)
            except (TimeoutError, asyncio.TimeoutError):
                pass  # 还没连上：这一轮照旧由 UIA 顶，后台继续试
        if self._reader is None:
            self._ensure_attaching()
            self._ui_rounds_before_attach = True
            return await self._ui.fetch(account_id)
        return await self._fetch_db(account_id)

    async def observe(self) -> None:
        """工作时间窗外只看不收：库里有变化照样打「本地库读到新消息」，但不推游标、不出消息、
        不开窗（`InboundLoop.tick` 窗外调它）。还没连上就只在后台连，不让 UIA 顶。"""
        if self._reader is not None and getattr(self._reader, "alive", True) is False:
            self._reader = None
            self._seen_sessions = None
        if self._reader is None:
            self._ensure_attaching()
            return
        sessions = self._reader.sessions()
        previous = self._seen_sessions
        self._seen_sessions = {name: value[0] for name, value in sessions.items()}
        if previous is not None:
            self._log_new_messages(previous, sessions, self._watchlist.watched())

    async def _fetch_db(self, account_id: str) -> list[InboundMessage]:
        self._watchlist.warn_if_idle()
        listed = self._watchlist.watched()
        self._round += 1

        # session.db 一解就知道每个会话最后一条的 local_id 和类型，不用挨个开消息分片。
        try:
            sessions = self._reader.sessions()
        except Exception as exc:  # noqa: BLE001 —— 本地库这一趟读不了就这轮空手，别拖垮循环
            _local.warning("读本地微信库会话表失败，这一轮不出新消息", error=str(exc))
            return []

        if self._ui_rounds_before_attach:
            self._ui_rounds_before_attach = False
            for peer_id in listed:
                username = self._resolve(peer_id, sessions)
                if username in sessions and self._store.db_cursor(account_id, peer_id) is None:
                    self._store.set_db_cursor(account_id, peer_id, sessions[username][0])

        watched = self._drop_handed_off(account_id, listed, sessions)
        _local.info(
            "开始新一轮轮询",
            轮次=self._round,
            来源="本地库",
            名单=len(listed),
            人工接管=len(listed) - len(watched),
        )

        # 库里每个会话的新消息都先打进日志，不管在不在名单（用户 2026-11-04：「读到数据库的新消息
        # 把日志打出来，回复不回复是监听名单的事」）。首轮只建基线，不把历史倒进日志。
        previous = self._seen_sessions
        self._seen_sessions = {name: value[0] for name, value in sessions.items()}
        if previous is not None:
            self._log_new_messages(previous, sessions, listed)

        collected: list[InboundMessage] = []
        for peer_id in watched:
            collected.extend(await self._fetch_watched(account_id, peer_id, sessions))

        # 名单外挂红点的单聊仍走 UIA 的窄路（名单只认同步，这里不接管）。但只在本地库看到
        # 名单外单聊真挂着红点、且是新来的时才开：每轮都扫就是每轮把微信拉到前台，省掉开窗
        # 等于白省（真机 2026-11-04：5 秒一轮「汇总未读消息」+「已把微信窗口拉回前台」）。
        if self._read_unread and self._stranger_activity(previous, listed, sessions):
            try:
                collected.extend(await self._ui.read_unread_strangers(account_id, listed))
            except Exception as exc:  # noqa: BLE001
                _local.warning("名单外红点扫描失败，这一轮跳过", error=str(exc))
        return collected

    def _log_new_messages(self, previous: dict[str, int], sessions: dict, listed: Sequence[str]) -> None:
        """这一轮库里有变化的会话**全部**打进日志（用户 2026-11-04：「只要读取到数据变化就显示打印到日志」）。

        逐条打「本地库读到新消息」：会话、会话类型（单聊/群聊/公众号/系统）、名单内否、发送方、
        消息类型、内容摘要（同 UIA 的 `preview`，截 40 字）。涨了但切不出可读消息的（撤回、系统提示、
        通话等）打一条「本地库会话有变化」。只打日志，不决定回不回——那是名单和下游的事。
        一个会话出错不影响别的会话。
        """
        try:
            listed_users = {self._resolve(peer_id, sessions): peer_id for peer_id in listed}
        except Exception:  # noqa: BLE001
            listed_users = {}
        for name, (local_id, newest_type) in sessions.items():
            before = previous.get(name)
            if before is not None and local_id <= before:
                continue
            kind = _chat_kind(name)
            try:
                if before is None:  # 新冒出来的会话：只看最后几条，别把整段历史打出来
                    before = max(local_id - LOG_MESSAGES_PER_SESSION, 0)
                rows = self._reader.new_messages(name, before)
                chat = listed_users.get(name) or self._reader.display_name(name) or name
                common = {"会话": chat, "会话类型": kind, "名单内": "是" if name in listed_users else "否"}
                if not rows:
                    _local.info("本地库会话有变化", **common, 最新一条类型=_TYPE_LABELS.get(newest_type, "其他"),
                                local_id=local_id)
                    continue
                for row in rows[-LOG_MESSAGES_PER_SESSION:]:
                    who = "自己" if row["is_self"] else (
                        self._reader.display_name(row["sender_username"])
                        if kind == "群聊" and row["sender_username"] else "对方"
                    )
                    label = _TYPE_LABELS.get(row["message_type"], row["message_type"])
                    _local.info(
                        "本地库读到新消息",
                        **common,
                        发送方=who,
                        类型=label,
                        内容=preview(row) if row["message_type"] == "text" else f"[{label}]",
                    )
                if len(rows) > LOG_MESSAGES_PER_SESSION:
                    _local.info("本地库这个会话一次来了很多条，日志只打最后几条", 会话=chat, 共=len(rows))
            except Exception as exc:  # noqa: BLE001 —— 打日志失败不能拖垮这一轮
                _local.warning("本地库会话有变化，但没读出内容", 会话=name, 会话类型=kind, error=str(exc)[:160])

    def _stranger_activity(self, previous: dict[str, int] | None, listed: Sequence[str], sessions: dict) -> bool:
        """名单外的单聊有没有「新来的红点」：未读 > 0，且这个会话是新冒出来的或 local_id 涨了。

        首轮只建基线、不扫：启动时就挂着的红点多半是放了很久的服务号/企业微信（真机
        2026-11-04：招行、微信团队、一个 @openim，各挂着几条），为它们开一次窗就是一次
        拉前台。读不到未读数也不扫——入站发现新消息只靠本地库，不拿 UIA 兜底轮询。
        """
        if previous is None:
            return False
        try:
            unread = self._reader.unread_counts()
        except Exception as exc:  # noqa: BLE001
            _local.warning("读本地库未读数失败，这一轮不扫名单外红点", error=str(exc))
            return False
        listed_users = {self._resolve(peer_id, sessions) for peer_id in listed}
        return any(
            sessions.get(name, (0, None))[0] > previous.get(name, 0)
            for name in unread
            if name not in listed_users and _is_direct_chat(name)
        )

    def _drop_handed_off(
        self, account_id: str, watched: Sequence[str], sessions: dict
    ) -> list[str]:
        """人工接管中的会话这一轮不读，并把它们的本地库游标推到最新——交回分身时
        才不会把接管期间招聘老师自己打的那串当「新消息」倒给模型（对齐 UIA 侧抹游标）。"""
        kept, paused = [], []
        for peer_id in watched:
            if self._store.candidate_mode(account_id, peer_id).mode == HUMAN_TAKEOVER:
                paused.append(peer_id)
                username = self._resolve(peer_id, sessions)
                if username and username in sessions:
                    self._store.set_db_cursor(account_id, peer_id, sessions[username][0])
            else:
                kept.append(peer_id)
        if paused and set(paused) != self._paused_peers:
            _local.info("这些会话正在人工接管，这轮不读", 数量=len(paused))
        elif self._paused_peers and not paused:
            _local.info("人工接管都交回来了，恢复读取")
        self._paused_peers = set(paused)
        return kept

    def _resolve(self, peer_id: str, sessions: dict) -> str | None:
        """会话名 → username；对不上 / 撞名返回 None（交 UIA 精确核对）。"""
        try:
            return self._reader.resolve_username(peer_id)
        except Exception:  # noqa: BLE001 —— contact.db 读不了就当对不上，交 UIA
            return None

    async def _fetch_watched(
        self, account_id: str, peer_id: str, sessions: dict
    ) -> list[InboundMessage]:
        cursor = self._store.db_cursor(account_id, peer_id)

        # 这个会话当前归 UIA（有没消化的媒体）：每轮委回 UIA；最后一条又变回文本、
        # 且 UIA 这轮没再吐出东西 = 追平了，交回本地库（基线推到最新，不回溯）。
        if cursor == _UI_OWNED:
            username = self._resolve(peer_id, sessions)
            latest, newest_type = sessions.get(username, (0, None)) if username else (0, None)
            if username in sessions and latest <= self._ui_owned_seen.get(peer_id, -1):
                return []  # 委回之后库里没新东西：不开窗
            results = await self._ui.read_peer(account_id, peer_id)
            self._ui_owned_seen[peer_id] = latest
            if newest_type == "text" and username in sessions:
                # UIA 已经读到最后一条，而它是文本：交回本地库，基线推到最新。
                self._store.set_db_cursor(account_id, peer_id, latest)
                self._ui_owned_seen.pop(peer_id, None)
            return results

        username = self._resolve(peer_id, sessions)
        if username is None or username not in sessions:
            # 名字对不上 / 会话不在表里：交 UIA（它会走查无此人→加好友那套）。
            return await self._ui.read_peer(account_id, peer_id)

        session_max, _newest = sessions[username]

        # 第一次见这个会话：同 UIA 的 `first_seen_fresh`，只接尾部连续、今天、对方发的
        # （最多 FIRST_SEEN_TAIL 条），直接从库里切，不开窗；这几条里有媒体才交 UIA。
        # 之后游标基线到最新，往后归本地库。
        if cursor is None:
            try:
                tail = _first_seen_tail(self._reader.new_messages(username, max(session_max - 50, 0)))
            except Exception as exc:  # noqa: BLE001 —— 读不了交 UIA 兜底
                _local.warning("读本地库消息失败，这个会话交 UIA", 会话=peer_id, error=str(exc))
                tail = None
            if tail is None or any(row["needs_ui_read"] for row in tail):
                results = await self._ui.read_peer(account_id, peer_id)
                self._store.set_db_cursor(account_id, peer_id, session_max)
                return results
            self._store.set_db_cursor(account_id, peer_id, session_max)
            return self._emit_text(account_id, peer_id, tail, first_seen=True)

        if session_max <= cursor:
            return []  # 没新消息

        try:
            rows = self._reader.new_messages(username, cursor)
        except Exception as exc:  # noqa: BLE001 —— 单会话读失败交 UIA 兜底，别炸整轮
            _local.warning("读本地库消息失败，这个会话交 UIA", 会话=peer_id, error=str(exc))
            return await self._ui.read_peer(account_id, peer_id)
        if not rows:
            return []

        # 结尾连着的语音先压住（转写可能还没出来），其余按就绪处理。
        ready, held = _hold_trailing_voice(rows)
        if held:
            _local.info("有语音还没转成文字，这段先不处理，下一轮再看", 会话=peer_id, 压住=held)
        if not ready:
            return []

        if any(row["needs_ui_read"] for row in ready):
            # 这一轮含媒体：整段委回 UIA（语音转写 / 附件另存），本地库游标标成 UI 持有，
            # 基线推到这批就绪的最后一条，之后每轮委回直到它追平。
            self._store.clear_cursor(account_id, peer_id)  # 让 UIA 走首见、只读尾部今天，别回放
            self._store.set_db_cursor(account_id, peer_id, _UI_OWNED)
            self._ui_owned_seen[peer_id] = session_max
            return await self._ui.read_peer(account_id, peer_id)

        # 全是文本：直接从库里出，游标推到这批最后一条 local_id。
        self._store.set_db_cursor(account_id, peer_id, ready[-1]["local_id"])
        return self._emit_text(account_id, peer_id, ready)

    def _emit_text(
        self, account_id: str, peer_id: str, ready: list[dict], *, first_seen: bool = False
    ) -> list[InboundMessage]:
        now = self._clock()
        inbound: list[InboundMessage] = []
        for row in ready:
            raw = {
                "text": row["text"],
                "is_self": row["is_self"],
                "message_type": "text",
                "timestamp_text": _clock_text(row["create_time"]),
            }
            inbound.append(
                to_inbound(account_id, peer_id, raw, seq=row["local_id"], at=now)
            )
        _local.info(
            "读完这个会话（本地库）",
            会话=peer_id,
            首见=first_seen,
            新消息=len(inbound),
            字数=sum(len(row["text"] or "") for row in ready),  # 逐条内容已在「本地库读到新消息」里
        )
        return inbound


def _first_seen_tail(rows: list[dict]) -> list[dict]:
    """首见只接的那几条：倒着数，遇到自己发的 / 不是今天的就停，最多 FIRST_SEEN_TAIL 条。"""
    today = datetime.now().date()
    tail: list[dict] = []
    for row in reversed(rows):
        if row["is_self"]:
            break
        if row["create_time"] and datetime.fromtimestamp(row["create_time"]).date() != today:
            break
        tail.append(row)
        if len(tail) >= FIRST_SEEN_TAIL:
            break
    tail.reverse()
    return tail


def _chat_kind(username: str) -> str:
    if username.endswith("@chatroom"):
        return "群聊"
    if username.startswith(("gh_", "brandsessionholder", "brandservicesessionholder")):
        return "公众号"
    return "单聊" if _is_direct_chat(username) else "系统"


def _is_direct_chat(username: str) -> bool:
    """单聊才可能是候选人：群、公众号、文件传输助手、折叠群入口这些都不算。"""
    return not (
        username.endswith("@chatroom")
        or username.startswith(("gh_", "@", "brandsessionholder", "brandservicesessionholder"))
        or username
        in {"filehelper", "weixin", "notifymessage", "fmessage", "medianote", "floatbottle", "mphelper"}
    )


def _hold_trailing_voice(rows: list[dict]) -> tuple[list[dict], int]:
    """结尾连着的语音先不处理（转写可能还没落库）：返回 (就绪部分, 压住几条)。

    只压**结尾连续**的语音：中间夹着的语音后面既然还有别的消息，说明它早已定型，照常委回
    UIA 转写；只有最末尾那串语音可能正在转。图片/文件/视频不压，一出现就是就绪的。
    """
    cut = len(rows)
    while cut > 0 and rows[cut - 1]["message_type"] == "voice":
        cut -= 1
    return rows[:cut], len(rows) - cut


def _clock_text(create_time: int) -> str:
    """消息 `create_time`（epoch 秒）→ 本机「HH:MM」，给模型当「微信标的时间」。"""
    if not create_time:
        return ""
    try:
        return datetime.fromtimestamp(create_time).strftime("%H:%M")
    except (ValueError, OSError):
        return ""
