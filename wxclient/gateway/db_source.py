"""入站来源：**读本地微信库**判新消息，不再逐个开聊天窗口轮询。

这是 `wechat_io.QueueMessageSource`（UIA 开窗轮询）的替身，只在 Windows、密钥扫到、
单账号时启用；扫不到 / 版本不对 / 多账号就回退到那条 UIA 链路（见 `factory`）。

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

from collections.abc import Sequence
from datetime import datetime, timezone

import structlog

from .. import logrole
from .inbound import InboundMessage
from .sessions import HUMAN_TAKEOVER, GatewayStore
from .watchlist import Watchlist
from .wechat_io import QueueMessageSource, to_inbound

_log = structlog.get_logger(__name__)
_local = _log.bind(**{logrole.ROLE_KEY: logrole.LOCAL})

#: `gateway_db_cursors` 里的哨兵：这个会话当前归 UIA 读（有未消化的语音/图片/文件等），
#: 本地库这一侧先不碰它，每轮委回 UIA；等它最后一条又变回文本、UIA 也追平了，再交回本地库。
_UI_OWNED = -1


class DbMessageSource:
    """按本地库判新消息的入站来源，接口与 `QueueMessageSource` 一致（`MessageSource`）。"""

    def __init__(
        self,
        *,
        reader,  # wechat_db.WeChatDbReader（鸭子类型，避免在非 Windows 上导入 ctypes 路径）
        store: GatewayStore,
        ui_source: QueueMessageSource,
        watchlist: Watchlist | None = None,
        clock=None,
        read_unread_strangers: bool = True,
    ) -> None:
        self._reader = reader
        self._store = store
        # UIA 替身：首次读、含媒体那一轮、名字对不上、名单外红点都委给它。
        self._ui = ui_source
        self._watchlist = watchlist or Watchlist()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._read_unread = bool(read_unread_strangers)
        self._round = 0
        self._paused_peers: set[str] = set()

    # MessageSource 里 runner 会装的两个回调，直接透传给 UIA 替身（查无此人 / 读不到要去加好友）。
    def set_unreachable_handler(self, handler) -> None:
        self._ui.set_unreachable_handler(handler)

    def set_not_found_handler(self, handler) -> None:
        self._ui.set_not_found_handler(handler)

    def set_hot_window(self, seconds: float) -> None:
        self._ui.set_hot_window(seconds)

    def set_full_interval(self, seconds: float) -> None:
        self._ui.set_full_interval(seconds)

    async def fetch(self, account_id: str) -> list[InboundMessage]:
        self._watchlist.warn_if_idle()
        listed = self._watchlist.watched()
        self._round += 1

        # session.db 一解就知道每个会话最后一条的 local_id 和类型，不用挨个开消息分片。
        try:
            sessions = self._reader.sessions()
        except Exception as exc:  # noqa: BLE001 —— 本地库这一趟读不了就这轮空手，别拖垮循环
            _local.warning("读本地微信库会话表失败，这一轮不出新消息", error=str(exc))
            return []

        watched = self._drop_handed_off(account_id, listed, sessions)
        _local.info(
            "开始新一轮轮询",
            轮次=self._round,
            来源="本地库",
            名单=len(listed),
            人工接管=len(listed) - len(watched),
        )

        collected: list[InboundMessage] = []
        for peer_id in watched:
            collected.extend(await self._fetch_watched(account_id, peer_id, sessions))

        # 名单外挂红点的单聊仍走 UIA 的窄路（名单只认同步，这里不接管）。
        if self._read_unread:
            try:
                collected.extend(await self._ui.read_unread_strangers(account_id, listed))
            except Exception as exc:  # noqa: BLE001
                _local.warning("名单外红点扫描失败，这一轮跳过", error=str(exc))
        return collected

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
            results = await self._ui.read_peer(account_id, peer_id)
            username = self._resolve(peer_id, sessions)
            newest_type = sessions.get(username, (0, None))[1] if username else None
            if not results and newest_type == "text" and username in sessions:
                self._store.set_db_cursor(account_id, peer_id, sessions[username][0])
            return results

        username = self._resolve(peer_id, sessions)
        if username is None or username not in sessions:
            # 名字对不上 / 会话不在表里：交 UIA（它会走查无此人→加好友那套）。
            return await self._ui.read_peer(account_id, peer_id)

        session_max, _newest = sessions[username]

        # 第一次见这个会话：交 UIA 走「首见只接尾部今天、对方发的」，再把本地库游标
        # 基线到最新，往后归本地库。
        if cursor is None:
            results = await self._ui.read_peer(account_id, peer_id)
            self._store.set_db_cursor(account_id, peer_id, session_max)
            return results

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
            return await self._ui.read_peer(account_id, peer_id)

        # 全是文本：直接从库里出，游标推到这批最后一条 local_id。
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
        self._store.set_db_cursor(account_id, peer_id, ready[-1]["local_id"])
        _local.info(
            "读完这个会话（本地库）",
            会话=peer_id,
            新消息=len(inbound),
            最近一条=(ready[-1]["text"] or "")[:40],
        )
        return inbound


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
