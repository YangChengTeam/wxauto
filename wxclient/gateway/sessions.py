"""网关本地状态：会话映射、消息去重游标、候选人接管档位、发送计数。

**必须持久化**：Windows 机重启、进程崩溃之后，如果 (专员微信号 × 对方 wxid) → session_id
这张表没了，所有候选人的对话上下文就断了——AI 会把聊了三天的人当陌生人重新打招呼。

与任务队列分库（``gateway.db``），避免和常驻 worker 抢锁。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import structlog

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_sessions (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_id, peer_id)
);
CREATE TABLE IF NOT EXISTS gateway_seen_messages (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  msg_id     TEXT NOT NULL,
  seen_at    TEXT NOT NULL,
  PRIMARY KEY (account_id, peer_id, msg_id)
);
-- 旧本机黑名单：2026-10-09 起只读不写，启动时迁成人工接管后删行（见 `legacy_blacklist`）。
CREATE TABLE IF NOT EXISTS gateway_blacklist (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  reason     TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (account_id, peer_id)
);
CREATE TABLE IF NOT EXISTS gateway_cursors (
  account_id     TEXT NOT NULL,
  peer_id        TEXT NOT NULL,
  last_signature TEXT NOT NULL,
  seq            INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (account_id, peer_id)
);
CREATE TABLE IF NOT EXISTS gateway_peer_kinds (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  is_group   INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_id, peer_id)
);
CREATE TABLE IF NOT EXISTS gateway_commands (
  account_id TEXT NOT NULL,
  command_id TEXT NOT NULL,
  status     TEXT NOT NULL,
  reason     TEXT NOT NULL DEFAULT '',
  result     TEXT,
  done_at    TEXT NOT NULL,
  PRIMARY KEY (account_id, command_id)
);
CREATE TABLE IF NOT EXISTS gateway_receipt_outbox (
  account_id TEXT NOT NULL,
  command_id TEXT NOT NULL,
  payload    TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_id, command_id)
);
CREATE INDEX IF NOT EXISTS idx_receipt_outbox_created
  ON gateway_receipt_outbox (created_at);
CREATE TABLE IF NOT EXISTS gateway_scheduled_commands (
  command_id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  payload    TEXT NOT NULL,
  due_at     TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_due ON gateway_scheduled_commands (due_at);
CREATE TABLE IF NOT EXISTS gateway_watchlist (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  payload    TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_pending_inbound (
  batch_id   TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  payload    TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_inbound_account
  ON gateway_pending_inbound (account_id, created_at);
CREATE TABLE IF NOT EXISTS gateway_message_events (
  event_id   TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  payload    TEXT NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_events_created
  ON gateway_message_events (created_at);
CREATE TABLE IF NOT EXISTS gateway_send_log (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  sent_at    TEXT NOT NULL,
  action     TEXT NOT NULL DEFAULT 'message'
);
CREATE INDEX IF NOT EXISTS idx_send_log_account_time
  ON gateway_send_log (account_id, sent_at);
CREATE TABLE IF NOT EXISTS gateway_contacts (
  account_id   TEXT NOT NULL,
  number       TEXT NOT NULL,
  session_name TEXT NOT NULL DEFAULT '',
  profile_name TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT '',
  worker_id    TEXT,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (account_id, number)
);
CREATE TABLE IF NOT EXISTS gateway_candidate_mode (
  account_id   TEXT NOT NULL,
  -- 本机寻址用的**会话名**（微信备注名）。主键用它而不是 candidate_id：
  -- 闸要在「给这个会话发消息」那一刻判，那一刻手上只有会话名（091 / docs/19 v2 §1）
  session_name TEXT NOT NULL,
  -- 业务端的 candidateId（= 同步名单 `GET /client/contacts` 的 worker_id，同一个 UUID 空间）。
  -- 空 = 还没对上号，本机闸照样成立，但按钮按不了（PUT 的 URL 里就是它）
  candidate_id TEXT NOT NULL DEFAULT '',
  -- human_takeover | ai_managed —— **直接用业务端 v1.5 的词**，本机不再翻译一遍
  mode         TEXT NOT NULL,
  reason       TEXT NOT NULL DEFAULT '',
  -- 服务端 aiRecruitment.version（只记录；0.1.113 起 PUT 不再带 expectedVersion，见 runner）
  version      INTEGER NOT NULL DEFAULT 0,
  -- 服务端 modeVersion + triggerId：`action=report` 要原样回传，客户端不解析
  mode_version INTEGER NOT NULL DEFAULT 0,
  trigger_id   TEXT NOT NULL DEFAULT '',
  updated_at   TEXT NOT NULL,
  -- 历史列：曾经的 `action=report` 时间。业务端 2026-08-26 改成「request 即生效」后
  -- 不再需要回报那一腿，这一列只留着不删（删列不可逆），新写入一律给当下时间
  reported_at  TEXT,
  -- 档位是谁定的：`client_candidate_mode` / `enterprise_candidate_mode` / `system`
  source       TEXT NOT NULL DEFAULT '',
  -- 服务端 `conversation_mode_updated_at` 原样存着（**服务端时钟**，只用于展示和排查，
  -- 不参与「谁更新」的判断——本机时钟和它不是一个钟，跨钟比较是错的）
  server_updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (account_id, session_name)
);
CREATE TABLE IF NOT EXISTS gateway_auto_friend (
  -- 「这台机器**自作主张**给这个号发过好友申请」的台账（092）。
  -- ★ 为什么不复用 gateway_contacts.status='requested'：那张表**同步名单会覆盖**
  --   （服务端才是权威，见 note_friend_request 的说明）。拿它当去重键，
  --   同步一次就会把同一个人再加一遍——加好友重复执行就是风控事故。
  -- ★ 这张表**只由本机写**，同步名单一个字都不碰它。
  account_id   TEXT NOT NULL,
  number       TEXT NOT NULL,
  session_name TEXT NOT NULL DEFAULT '',
  at           TEXT NOT NULL,
  PRIMARY KEY (account_id, number)
);
CREATE TABLE IF NOT EXISTS gateway_friend_checks (
  -- 「**这台机器真的发出过**好友申请、还在等对方通过」的台账（093 → 2026-09-01 改口径）。
  --
  -- ★★ 为什么这张表是权威、而不是 `gateway_contacts.status='requested'`：
  --   `requested` 有**两个来源**，只有一个能算数——
  --     ① 我们 `add_friend` 真的把申请发出去了（这里记的就是它）；
  --     ② 业务后端推计划时自己写的一条「计划点名要加谁」，那个人**我们根本还没加**。
  --   搜索复查和放行进监听名单都只认 ①。认了 ② 就是把没申请过的人放进 fail-closed
  --   名单（085），有可能给一个我们从没打过招呼的人自动回复。
  -- ★ 只由本机写，同步名单一个字都不碰它——所以后端把 `contact_status` 回成空串
  --   （`save_contacts` 无条件覆盖 status）也冲不掉这条链路。
  account_id   TEXT NOT NULL,
  number       TEXT NOT NULL,
  -- 申请发出去时我们报给业务端的那个名称（= 备注优先、没备注才退回昵称）。
  -- 复查搜的就是它：和 `POST /client/contacts` 报上去的是同一个字符串。
  session_name TEXT NOT NULL DEFAULT '',
  -- 申请发出去的时刻。**搜索期限从它算起**（配置中心「查多久」），不是从上次查算起。
  requested_at TEXT NOT NULL DEFAULT '',
  -- 上一次**真去微信里查过**他的时间（查失败也算，否则下一轮还挑中同一个人）
  last_at      TEXT NOT NULL DEFAULT '',
  -- 历史列：曾经的「确认过还没通过」次数上限（MAX_CHECKS=96）。2026-09-01 用户改成
  -- 按**期限**放弃，这一列不再参与判断，只留着不删（删列不可逆）。
  checks       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, number)
);
CREATE TABLE IF NOT EXISTS gateway_moment_comments (
  account_id  TEXT NOT NULL,
  content_key TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (account_id, content_key)
);
CREATE TABLE IF NOT EXISTS gateway_moment_cursor (
  account_id TEXT PRIMARY KEY,
  first_key  TEXT NOT NULL,
  seen_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_read_marks (
  account_id  TEXT NOT NULL,
  action      TEXT NOT NULL,
  last_at     TEXT NOT NULL,
  PRIMARY KEY (account_id, action)
);
CREATE TABLE IF NOT EXISTS gateway_contact_sync (
  account_id   TEXT PRIMARY KEY,
  synced_until TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_messages (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  msg_id     TEXT NOT NULL,
  at         TEXT NOT NULL,
  at_text    TEXT NOT NULL DEFAULT '',
  from_self  INTEGER NOT NULL DEFAULT 0,
  from_gateway INTEGER NOT NULL DEFAULT 0,
  text       TEXT NOT NULL DEFAULT '',
  raw_key    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (account_id, peer_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_peer_time
  ON gateway_messages (account_id, peer_id, at);
CREATE TABLE IF NOT EXISTS gateway_sent_echo (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  text_key   TEXT NOT NULL,
  sent_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_echo_peer
  ON gateway_sent_echo (account_id, peer_id, text_key);
-- 138：好友关系周期。一个会话只留**当前**这一次加上好友的记录；删了又加才换新的 id。
-- AI 端拿 id 当「这次加好友」的开场名额键的一部分，所以重读、重连、重启都必须是同一个值。
CREATE TABLE IF NOT EXISTS gateway_relationships (
  account_id      TEXT NOT NULL,
  peer_id         TEXT NOT NULL,
  relationship_id TEXT NOT NULL,
  established_at  TEXT NOT NULL,
  announced       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, peer_id)
);
"""


_log = structlog.get_logger(__name__)


def text_key(text: str) -> str:
    """一段聊天文字 → 可比对的键：去首尾空白、把所有空白折成一个空格。

    微信把一段文字显示成气泡时会吞掉首尾换行、UIA 读到的 name 再被 `strip()`；
    多行文本的换行在不同版本里读回来可能是换行也可能是空格。按原文逐字比对就会漏认。
    `[微笑]` 这类内置表情代码**不抹**：线上实测（2026-09-23，微信 4.1.x）UIA 把渲染成黄脸的表情
    仍按 `[微笑]` 原文读回来，两边本来就对得上；抹了反而会把对方恰好说的同一句无表情原话认成回声。
    出站原文认亲（`remember_sent_text`）和聊天记录的内容键（`gateway_messages.raw_key`）
    都用这一个函数——两边算法一分岔，读回来的行就对不上记录。
    """
    return " ".join(str(text or "").split())


_echo_key = text_key


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """幂等加列。SQLite 的 ADD COLUMN 不动既有数据，只给老行填默认值。"""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _retire_strangers(conn: sqlite3.Connection) -> None:
    """拆掉「临时准入」（2026-09-24）：监听名单只认 `GET /client/contacts` 同步来的。

    用户：「监听逻辑是同步的，不应该自己添加」。090 扫会话列表 / 通过好友申请后本机自己往监听里
    加人，0.1.80 让扫描真跑起来后把「腾讯新闻」、自己的群都收了进去，界面上还删不掉。

    老库要收拾两处：那张表本身，以及**落盘的生效名单**（`gateway_watchlist`）里经它加进去的名字——
    后者不清的话，派生名单为空的机器（按 085 沿用上一份）会一直盯着它们。同步名单里真有的名字留着。
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gateway_strangers'"
    ).fetchone()
    if not exists:
        return
    added = {str(r[0]).strip() for r in conn.execute("SELECT session_name FROM gateway_strangers").fetchall()}
    synced = {
        str(r[0]).strip()
        for r in conn.execute(
            "SELECT session_name FROM gateway_contacts WHERE status='accepted' AND TRIM(session_name)<>''"
        ).fetchall()
    }
    stale = added - synced
    row = conn.execute("SELECT payload FROM gateway_watchlist WHERE id=1").fetchone()
    if stale and row:
        try:
            payload = json.loads(row[0])
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("only"), list):
            kept = [n for n in payload["only"] if str(n).strip() not in stale]
            if len(kept) != len(payload["only"]):
                payload["only"] = kept
                conn.execute(
                    "UPDATE gateway_watchlist SET payload=?, updated_at=? WHERE id=1",
                    (json.dumps(payload, ensure_ascii=False), _utcnow().isoformat()),
                )
    conn.execute("DROP TABLE gateway_strangers")
    conn.commit()


#: 候选人级接管/托管的两个词（业务端 v1.5 §4，本机原样沿用，不再翻译一遍）。
HUMAN_TAKEOVER = "human_takeover"
AI_MANAGED = "ai_managed"
CANDIDATE_MODES = (HUMAN_TAKEOVER, AI_MANAGED)


@dataclass(frozen=True)
class CandidateModeRow:
    """本机那份「这个候选人现在由谁跟」（091 / docs/19 v2 §1）。

    ★ 与已拆除的分身级那张表最大的不同：**它是每个人一行**，而且默认那一行
      **不存在**——「没记录」= 这个人由分身跟（`ai_managed`），和「服务端说他是
      `ai_managed`」看起来一样但来路不同，所以 `known` 分得开：界面上「不知道当前档」
      不能画成「确定是托管」。
    """

    session_name: str
    candidate_id: str
    mode: str
    reason: str
    version: int
    mode_version: int
    trigger_id: str
    updated_at: str
    """历史字段（回报那一腿已取消）；本机写入时一律给当下时间。"""
    reported_at: str | None
    """档位是谁定的：`client_candidate_mode` / `enterprise_candidate_mode` / `system` / 空。"""
    source: str = ""
    """本机真有这一行吗。False = 从没设过，按默认「分身在跟」处理。"""
    known: bool = True

    @property
    def taken_over(self) -> bool:
        return self.mode == HUMAN_TAKEOVER


class GatewayStore:
    """网关本地状态存储。所有方法同步——SQLite 本来就快，别为它引入异步复杂度。"""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            # 老库补列：CREATE TABLE IF NOT EXISTS 不会动既有表，不补的话
            # 分动作频控（072）在升级过的机器上会直接 "no such column" 崩掉。
            _ensure_column(conn, "gateway_send_log", "action", "TEXT NOT NULL DEFAULT 'message'")
            _ensure_column(conn, "gateway_candidate_mode", "source", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(conn, "gateway_candidate_mode", "server_updated_at", "TEXT NOT NULL DEFAULT ''")
            # 093 改口径（2026-09-01）：复查台账从「进度」升级成「我们真发过申请」的权威，
            # 老库那两列不补就是 "no such column"。
            _ensure_column(conn, "gateway_friend_checks", "session_name", "TEXT NOT NULL DEFAULT ''")
            _retire_strangers(conn)
            _ensure_column(conn, "gateway_friend_checks", "requested_at", "TEXT NOT NULL DEFAULT ''")
            # 2026-09-19：历史里分身自己的回声要标「分身」而不是「专员本人」（AI 端否则当成老师插话）。
            _ensure_column(conn, "gateway_messages", "from_gateway", "INTEGER NOT NULL DEFAULT 0")
            # 2026-09-23：记下每条消息在微信里读到的**原始文字**的键，游标丢了时按它认「这段早就处理过」
            # （见 `recent_text_keys`）。老行留空，查询时退回按 `text` 算。
            _ensure_column(conn, "gateway_messages", "raw_key", "TEXT NOT NULL DEFAULT ''")
            # 138：微信系统提示行（不是谁说的话）。开场指令执行前判「对方开口没有」要把它们排除。
            _ensure_column(conn, "gateway_messages", "system_tip", "INTEGER NOT NULL DEFAULT 0")
            # ★ 补列之后还要**回填**：老库里这两列是空的，而复查按 `session_name` 搜、
            #   按 `requested_at` 算期限——不回填，升级那一刻**所有在途的人当场消失**，
            #   一个都不会再被查（正是这次要治的病，不能靠升级又制造一遍）。
            #   只 UPDATE 已有行、不 INSERT 新行：已有行是老复查循环真的在跟的人；
            #   凭 `gateway_contacts.status='requested'` 新造行会把后端写的
            #   「计划点名要加谁」也拉进来，那些人我们根本没加过（见建表注释）。
            #   `requested_at` 补成当下：老库没记过申请时刻，给它一个完整的期限窗，
            #   宁可多查几天，也好过升级即丢。
            conn.execute(
                """UPDATE gateway_friend_checks
                      SET session_name = COALESCE((
                            SELECT c.session_name FROM gateway_contacts AS c
                             WHERE c.account_id = gateway_friend_checks.account_id
                               AND c.number = gateway_friend_checks.number
                               AND TRIM(c.session_name) <> ''), ''),
                          requested_at = ?
                    WHERE TRIM(session_name) = '' OR TRIM(requested_at) = ''""",
                (_utcnow().isoformat(),),
            )
            # 连续失败次数（加好友退避用）。老库没有这一列，不补就 "no such column"。
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None = 关掉 sqlite3 的隐式事务：预留额度那段要自己 `BEGIN IMMEDIATE`
        # （见 reserve_sends），隐式事务会跟它打架。其余写法照旧显式 commit。
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ────────────────────────── 候选人会话名（api2 §4） ──────────────────────────

    def save_contacts(self, account_id: str, rows: Iterable[dict]) -> int:
        """落一批从后端同步回来的会话名映射（按 number 覆盖）。返回落了几条。

        为什么要在本机留一份：换机/重装之后，同一个候选人如果被起了第二个备注名，
        微信里就成了「两个人」——逐字寻址会找错窗口，聊天上下文也就断了。

        ★ 091：同一条同步里带的 `conversation_mode`（候选人级接管/托管）顺手对齐——
          **同步名单就是候选人状态的来源**（用户 2026-08-26 定）。写成**带了才更新**：
          没带（老后端）就一个字都不动，绝不拿「没带」当成「他是托管」。

        ★★ 水位与隔离窗的**咬合**：被隔离窗挡下的那一行（见 `_absorb_conversation_mode`），
          水位**不许越过它**。这是增量同步的陷阱——水位一旦推过去，那一行除非在服务端
          再变一次，否则**永远不会再发回来**，本机就会一直停在接管上，谁也解不开。
          挡下就把水位压回它自己那一刻（`updated_since` 是 `>=`，下一轮它会再来一次）。
        """
        saved = 0
        watermark = ""
        deferred = ""  # 被隔离窗挡下的那些行里**最早**的服务端时间
        with closing(self._connect()) as conn:
            for row in rows:
                number = str(row.get("number") or "").strip()
                if not number:
                    continue  # 没有 number 就归不了位，留着也没用
                conn.execute(
                    """INSERT INTO gateway_contacts
                         (account_id, number, session_name, profile_name, status, worker_id, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(account_id, number) DO UPDATE SET
                         session_name = excluded.session_name,
                         profile_name = excluded.profile_name,
                         status       = excluded.status,
                         worker_id    = excluded.worker_id,
                         updated_at   = excluded.updated_at""",
                    (account_id, number,
                     str(row.get("session_name") or ""),
                     str(row.get("profile_name") or ""),
                     str(row.get("contact_status") or ""),
                     row.get("worker_id"),
                     str(row.get("updated_at") or "")),
                )
                saved += 1
                row_at = str(row.get("updated_at") or "")
                if not self._absorb_conversation_mode(conn, account_id, row) and row_at:
                    deferred = min(deferred, row_at) if deferred else row_at
                # ★ 水位只认**服务端给的**时间。原来缺 updated_at 时拿本机时钟顶替，
                # 那个值一旦被当成水位，比它早的行以后永远同步不回来（时钟还不一定准）。
                watermark = max(watermark, str(row.get("updated_at") or ""))
            if watermark:
                conn.execute(
                    """INSERT INTO gateway_contact_sync (account_id, synced_until) VALUES (?, ?)
                       ON CONFLICT(account_id) DO UPDATE SET
                         synced_until = MAX(excluded.synced_until, gateway_contact_sync.synced_until)""",
                    (account_id, watermark),
                )
            if deferred:
                # ★ 有行被隔离窗挡下 → **故意把水位往回压**到它自己那一刻。
                #   上面那句是 MAX（防乱序批次把水位冲低），这句是 MIN（只往回退这一步），
                #   两句分开写，谁也别顶掉谁。`updated_since` 是 `>=`，下一轮它会再来。
                #   隔离窗最多 180 秒、同步 120 秒一轮，所以最多多拉两轮就自愈。
                conn.execute(
                    """UPDATE gateway_contact_sync SET synced_until = MIN(synced_until, ?)
                       WHERE account_id = ?""",
                    (deferred, account_id),
                )
            conn.commit()
        return saved

    def replace_accepted_contacts(self, account_id: str, rows: Iterable[dict]) -> int:
        """全量同步落库：先按 `save_contacts` 落，再删掉本机多出来的 `accepted` 行。返回删了几条。

        ★ 为什么要删：增量同步（`updated_since`）只会带回**变了**的行，后端删掉/不再给的行
        永远不会再来，本机那一行就一直挂在监听名单里、界面上还删不掉（2026-09-24 用户：
        「监听计划已经没有监听了，但是监听名单里面还有……点击同步应该能重新同步」）。
        只删 `accepted`：`requested` 里有本机自己记的「刚发出的好友申请」，那是复查用的台账，
        不影响名单，也不该被一次同步抹掉。调用方必须保证 `rows` 是**拉全了的**。
        """
        rows = list(rows)
        self.save_contacts(account_id, rows)
        keep = {str(row.get("number") or "").strip() for row in rows} - {""}
        with closing(self._connect()) as conn:
            stale = [
                str(r[0]) for r in conn.execute(
                    "SELECT number FROM gateway_contacts WHERE account_id=? AND status='accepted'",
                    (account_id,),
                ).fetchall()
                if str(r[0]) not in keep
            ]
            conn.executemany(
                "DELETE FROM gateway_contacts WHERE account_id=? AND number=?",
                [(account_id, number) for number in stale],
            )
            conn.commit()
        return len(stale)

    # 本机刚按下的接管，多久之内不许被同步名单里的 `ai_managed` 盖回去（秒）。
    # 见 `_absorb_conversation_mode` 的说明。
    _LOCAL_TAKEOVER_QUARANTINE = 180.0

    @classmethod
    def _absorb_conversation_mode(cls, conn: sqlite3.Connection, account_id: str, row: dict) -> bool:
        """同步名单里那份候选人档位 → 本机 `gateway_candidate_mode`（091）。

        业务端 2026-08-26 给 `GET /client/contacts` 每一行加了
        `conversation_mode` / `conversation_mode_source` / `conversation_mode_updated_at`。
        **这是换机 / 重装 / 别人在企业端改了之后，本机唯一能知道真相的路。**

        ★ **方向不对称**，这是本方法最要紧的一条：

        - 服务端说 `human_takeover` → **无条件收下**。往「别发消息」那一侧倒是安全的。
        - 服务端说 `ai_managed` → 只有在本机那一行**不是刚按下的接管**时才收下。

          为什么要这道闸：招聘老师在 T 时刻按下接管，客户端先本机生效再 PUT；
          一条**在 T 之前就发出去**的名单请求可能在 T 之后才回来，里面带的还是
          `ai_managed`。照单全收就等于「明明点了接管，机器还在发消息」——
          091 里「只有 409 才回滚」防的就是这件事，而这是它的**第二条路**。

          闸用本机时钟量本机自己的写入时间（180 秒），**不拿服务端时间戳跟本机比**：
          那是两个钟，跨钟比大小是错的。窗口一过服务端照样说了算，所以它自愈，
          不会把一条真的「已交回托管」永久挡在外面。

        返回值：`True` = 这一行处理完了（收下了，或本来就没什么可收的）；
        `False` = **被隔离窗挡下了，还得再来一次**，调用方据此把水位压回去。
        """
        mode = str(row.get("conversation_mode") or "").strip()
        session_name = str(row.get("session_name") or "").strip()
        if not session_name or mode not in CANDIDATE_MODES:
            # 字段没来（老后端）、会话名为空、档位不认识 —— 都当没这回事，本机不动。
            return True
        if mode == AI_MANAGED and cls._is_fresh_local_takeover(conn, account_id, session_name):
            _log.info("同步名单说托管，但本机刚按下接管，这一轮不改", session=session_name)
            return False
        # 交回分身的那一刻要把游标抹掉——接管期间没轮询，续上就是把老师自己打的话
        # 当新消息接过来（见 `_forget_cursor`）。判要在写之前，写完就看不出来了。
        resumed = mode == AI_MANAGED and cls._mode_of(conn, account_id, session_name) == HUMAN_TAKEOVER
        now = _utcnow().isoformat()
        conn.execute(
            """INSERT INTO gateway_candidate_mode
                 (account_id, session_name, candidate_id, mode, reason,
                  version, mode_version, trigger_id, updated_at, reported_at,
                  source, server_updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id, session_name) DO UPDATE SET
                 candidate_id=excluded.candidate_id,
                 mode=excluded.mode,
                 updated_at=excluded.updated_at,
                 reported_at=excluded.reported_at,
                 source=excluded.source,
                 server_updated_at=excluded.server_updated_at""",
            (
                account_id, session_name, str(row.get("worker_id") or ""), mode, "",
                0, 0, "", now, now,
                str(row.get("conversation_mode_source") or "")[:40],
                str(row.get("conversation_mode_updated_at") or "")[:64],
            ),
        )
        if resumed:
            cls._forget_cursor(conn, account_id, session_name)
            _log.info("交回分身，这个会话按「第一次读」重新接上", session=session_name)
        return True

    @staticmethod
    def _mode_of(conn: sqlite3.Connection, account_id: str, session_name: str) -> str:
        """本机这一行现在记着的档位。没这一行 → 空串（不是 `ai_managed`：
        「没记录」和「确认托管」是两件事，见 `candidate_mode` 的 `known`）。"""
        row = conn.execute(
            "SELECT mode FROM gateway_candidate_mode WHERE account_id=? AND session_name=?",
            (account_id, session_name),
        ).fetchone()
        return str(row[0]) if row else ""

    @classmethod
    def _is_fresh_local_takeover(cls, conn: sqlite3.Connection, account_id: str, session_name: str) -> bool:
        """本机这一行是不是「刚刚按下的接管」（见 `_absorb_conversation_mode`）。"""
        found = conn.execute(
            "SELECT mode, updated_at FROM gateway_candidate_mode "
            "WHERE account_id=? AND session_name=?",
            (account_id, session_name),
        ).fetchone()
        if not found or str(found[0]) != HUMAN_TAKEOVER:
            return False
        try:
            written = datetime.fromisoformat(str(found[1]))
        except ValueError:
            return False
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return (_utcnow() - written).total_seconds() < cls._LOCAL_TAKEOVER_QUARANTINE

    def contact_session_name(self, account_id: str, number: str) -> str:
        """这台机器（或换机前那台）给这个人用的备注名。没有就返回空串。"""
        number = (number or "").strip()
        if not number:
            return ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT session_name FROM gateway_contacts WHERE account_id = ? AND number = ?",
                (account_id, number),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def contact_worker_id(self, account_id: str, session_name: str) -> str:
        """按**会话名**反查这个人在业务库里的候选人 ID。没有就返回空串。

        入站对话只知道「对面这个微信会话叫什么」——`worker_id` 是上报会话名时后端归位后回给
        我们的（`contacts.report()` 的 `matched/worker_id`），本机存着就是为了这一刻：
        AI 端判定「这个人该转人工了」，而它不知道、也不该知道对方是哪个候选人
        （081 裁决 2：手机号↔会话名的关联全程不经 AI 端）。

        会话名不是主键（一个人可能换过备注），取最近更新的那条。
        """
        session_name = (session_name or "").strip()
        if not session_name:
            return ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT worker_id FROM gateway_contacts
                    WHERE account_id = ? AND session_name = ? AND worker_id IS NOT NULL
                    ORDER BY updated_at DESC LIMIT 1""",
                (account_id, session_name),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def watched_session_names(self, account_id: str) -> list[str]:
        """监听名单：这台机器该盯哪些会话（spec 085，用户 2026-08-20 改口径）。

        **就从会话名映射里派生，不另开接口**——`GET /client/contacts` 回来的就是
        「这位专员该管的那些人在他微信里叫什么」，那本身就是名单。

        两条过滤，都不是可有可无的：
        - **只要 `accepted`**：`requested` 是「申请发出去了、对方还没通过」。好友没通过时
          微信里根本没有那个会话窗口，放进名单只会让每轮白开一次、白吃一个读名额；
          而这类行还有第二个来源——业务后端推计划时会自己写一条 `requested`，
          那是「计划点名要加谁」，更不是「可以自动回复」。
        - **会话名非空**：名单是按微信里显示的名字**逐字**寻址的，空名字查无此人。
          `TRIM` 一道是必要的——`save_contacts` 原样存后端给的字符串，只判 `<> ''`
          放得过去一个全是空格的名字，那种名字每轮都会去开一次窗口、每轮都开不到。

        ★ 派生读的是**本机这张表**，不是每次去打接口。所以「这一趟没拉到」天然等于
        「沿用上一份」——`_sync_contacts_once` 拉失败时压根不写表。
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT DISTINCT session_name FROM gateway_contacts
                   WHERE account_id=? AND status=? AND TRIM(session_name)<>''
                   ORDER BY session_name""",
                (account_id, "accepted"),
            ).fetchall()
        return [row[0].strip() for row in rows]

    def known_session_names(self, account_id: str) -> set[str]:
        """本机会话名映射里已有的名字（不分 accepted/requested）。派生名单为空时拿来分诊。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT session_name FROM gateway_contacts WHERE account_id=? AND TRIM(session_name)<>''",
                (account_id,),
            ).fetchall()
        return {str(row[0]).strip() for row in rows}

    def contacts_synced_until(self, account_id: str) -> str:
        """本机已同步到的最新 `updated_at`（增量同步的水位）。空 = 没同步过，要全量。

        **单独存，不从数据行里推**：这张表里现在也有本机自己记的行（刚发出的好友申请，
        见 `note_friend_request`），它们没有服务端时间；拿 `MAX(updated_at)` 当水位就会
        把本机写的东西算进去，从此漏数据。
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT synced_until FROM gateway_contact_sync WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    # ────────────────────── 自动补加好友台账（092） ──────────────────────

    def auto_friend_tried(self, account_id: str, number: str) -> str:
        """这台机器自作主张给这个号发过好友申请吗。发过返回那一刻，没发过返回空串。

        ★ **同一个号只自动加一次**，之后永不自动重加。对方一直不通过也不再补——
          自动重加是风控最敏感的一件事，而「没通过」本来就该由人去看。
          人工或 AI 明确下发的 `add_friend` 指令**不看这张表**：那是别人要求的，
          不是本机自作主张。
        """
        number = (number or "").strip()
        if not number:
            return ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT at FROM gateway_auto_friend WHERE account_id=? AND number=?",
                (account_id, number),
            ).fetchone()
        return str(row[0]) if row else ""

    def note_auto_friend(self, account_id: str, number: str, session_name: str, at: str) -> bool:
        """记下「本机要给这个号自动发好友申请了」。**先记账再动手。**

        返回 `True` = 这一笔是新记上的，可以动手；`False` = 已经有了，**别加**。
        顺序不能反：先动手后记账的话，动手成功而记账失败（磁盘满、进程被杀）
        下一轮就会再加一次。宁可漏一次，不可重一次。
        """
        number, session_name = (number or "").strip(), (session_name or "").strip()
        if not number:
            return False
        with closing(self._connect()) as conn:
            changed = conn.execute(
                """INSERT INTO gateway_auto_friend (account_id, number, session_name, at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(account_id, number) DO NOTHING""",
                (account_id, number, session_name, at),
            ).rowcount
            conn.commit()
        return changed > 0

    def last_auto_friend_at(self, account_id: str) -> str:
        """本机上一次自动加好友是什么时候（节拍用）。没加过返回空串。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MAX(at) FROM gateway_auto_friend WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    # ─────────── 已申请、还没通过的好友：复查台账（093）───────────

    def next_friend_check(self, account_id: str, *, not_before: str) -> tuple[str, str]:
        """下一个该复查的「我们真发过申请、还在等通过」的人：``(number, session_name)``。

        取的是**本机台账**（见建表注释），不是 `gateway_contacts.status='requested'`——
        后者混着业务后端写的「计划点名要加谁」，那些人我们根本还没加，不能拿去搜、
        更不能据此认定谁通过了。

        ``not_before`` = 搜索期限的起点（配置中心「查多久」换算出来的时刻）：
        申请发出时间早于它的就不再查了。**期限从申请那一刻算起**，不是从上次查算起——
        否则一个查得慢的人可以无限续期。2026-09-01 用户改口径，取代原来按次数的 MAX_CHECKS。

        排序「最久没查过的优先」：没查过的 `last_at` 是空串，天然排最前。
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT number, session_name
                     FROM gateway_friend_checks
                    WHERE account_id = ?
                      AND TRIM(session_name) <> ''
                      AND requested_at >= ?
                 ORDER BY last_at, number
                    LIMIT 1""",
                (account_id, not_before),
            ).fetchone()
        return (str(row[0]), str(row[1]).strip()) if row else ("", "")

    def note_friend_check(self, account_id: str, number: str, at: str) -> None:
        """记下「这一趟真去微信里查过他了」。查成功查失败都记。

        不记的话下一轮还会挑中同一个人（排序按 `last_at`），后面排队的谁也轮不上。
        原来还按「确认过还没通过」计次、到 96 次放弃，2026-09-01 改成按期限放弃，
        计次那一列留着不删但不再参与判断。
        """
        number = (number or "").strip()
        if not number:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_friend_checks SET last_at = ? WHERE account_id = ? AND number = ?",
                (at, account_id, number),
            )
            conn.commit()

    def drop_friend_request(self, account_id: str, number: str) -> None:
        """销账：他通过了（或已经不用再等了）。销掉之后复查循环就不会再挑中他。"""
        number = (number or "").strip()
        if not number:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM gateway_friend_checks WHERE account_id = ? AND number = ?",
                (account_id, number),
            )
            conn.commit()


    def friend_check_mark(self, account_id: str, number: str) -> str:
        """上一次**真去微信里查过他**的时间；没查过是空串。排查和测试用。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT last_at FROM gateway_friend_checks WHERE account_id=? AND number=?",
                (account_id, (number or "").strip()),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def note_friend_request(
        self, account_id: str, number: str, session_name: str, at: str = "",
    ) -> None:
        """记下「我们刚给这个号发了好友申请，报给业务端的名称是这个」。

        为的是**以后能认出对方通过了**：通过之后我们收到的第一条消息只带会话名，
        而上报要的是 `number`（api2 §4.1 必填）——两者的桥就是这一行。
        重启也要还在，所以落库不放内存。

        ★ 写**两张表**，各有各的用处，别合并：
          · `gateway_contacts` —— 给别的链路看的那份，服务端同步回来会覆盖它（那边是权威）。
          · `gateway_friend_checks` —— **本机台账**，同步一个字都不碰。复查搜谁、
            以及「发消息发成功了说明他通过了」查谁，都只认这一份。
            2026-09-01 之前只写前者，于是后端把 `contact_status` 回成空串就能把
            `status='requested'` 冲掉，这个人从此既不被复查、也认不出通过——一声不吭。
        """
        number, session_name = (number or "").strip(), (session_name or "").strip()
        if not number or not session_name:
            return
        at = at or _utcnow().isoformat()
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO gateway_friend_checks
                     (account_id, number, session_name, requested_at, last_at, checks)
                   VALUES (?, ?, ?, ?, '', 0)
                   ON CONFLICT(account_id, number) DO UPDATE SET
                     session_name = excluded.session_name,
                     requested_at = excluded.requested_at,
                     last_at      = ''""",
                (account_id, number, session_name, at),
            )
            conn.execute(
                """INSERT INTO gateway_contacts
                     (account_id, number, session_name, profile_name, status, worker_id, updated_at)
                   VALUES (?, ?, ?, '', 'requested', NULL, '')
                   ON CONFLICT(account_id, number) DO UPDATE SET
                     session_name = excluded.session_name,
                     status       = CASE WHEN gateway_contacts.status = 'accepted'
                                         THEN gateway_contacts.status ELSE 'requested' END""",
                (account_id, number, session_name),
            )
            conn.commit()

    def pending_friend_request(self, account_id: str, session_name: str) -> str:
        """这个会话名对应的号，如果我们还在等他通过的话。不等了或不认识 → 空串。

        ★ 查的是**本机台账**，不是 `gateway_contacts.status='requested'`：
          后者被同步名单无条件覆盖（`save_contacts`，后端没回 `contact_status` 就是空串），
          一冲掉这里就再也认不出「他通过了」。台账只由本机写，冲不掉。
        """
        session_name = (session_name or "").strip()
        if not session_name:
            return ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT number FROM gateway_friend_checks
                   WHERE account_id = ? AND session_name = ? LIMIT 1""",
                (account_id, session_name),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    def contact_number_for(self, account_id: str, session_name: str) -> str:
        """按会话名反查号（不限状态）。我方通过对方申请时只拿得到名字，靠它补 `number`。"""
        session_name = (session_name or "").strip()
        if not session_name:
            return ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT number FROM gateway_contacts
                   WHERE account_id = ? AND session_name = ? LIMIT 1""",
                (account_id, session_name),
            ).fetchone()
        return str(row[0]) if row and row[0] else ""

    # ────────────────────────── 会话映射 ──────────────────────────

    def get_session(self, account_id: str, peer_id: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT session_id FROM gateway_sessions WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            ).fetchone()
        return row[0] if row else None

    def put_session(self, account_id: str, peer_id: str, session_id: str) -> None:
        now = _utcnow().isoformat()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_sessions (account_id, peer_id, session_id, created_at, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(account_id, peer_id)
                DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at
                """,
                (account_id, peer_id, session_id, now, now),
            )
            conn.commit()

    def drop_session(self, account_id: str, peer_id: str) -> None:
        """AI 端说这个 session 接不回来了（被驱逐/越权）时清掉，下次重新建。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM gateway_sessions WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            )
            conn.commit()

    def forget_peer(self, account_id: str, peer_id: str) -> int:
        """名单上的「清除」在本机这一半：丢掉会话映射和本机记下的聊天记录，返回删了几条记录。

        聊天记录（``gateway_messages``）每轮当 ``history`` 送给 AI 端——AI 端那边删干净了、这里留着，
        下一轮照样把旧事讲回去，等于没清。

        ★ **不动**的：已读去重（``gateway_seen_messages``）和读取游标——动了，微信里那些旧消息
          会被当成新消息再读一遍、再回一遍；名单、接管档、好友账本——那是配置，不是会话。
        """
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM gateway_sessions WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            )
            cur = conn.execute(
                "DELETE FROM gateway_messages WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            )
            conn.commit()
            return cur.rowcount

    # ────────────────────────── 消息去重 ──────────────────────────

    def filter_unseen(
        self, account_id: str, peer_id: str, msg_ids: Iterable[str]
    ) -> list[str]:
        """返回其中**尚未处理过**的 msg_id，顺序保持不变。"""
        ids = list(msg_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT msg_id FROM gateway_seen_messages "
                f"WHERE account_id=? AND peer_id=? AND msg_id IN ({placeholders})",
                (account_id, peer_id, *ids),
            ).fetchall()
        seen = {row[0] for row in rows}
        return [msg_id for msg_id in ids if msg_id not in seen]

    def mark_seen(self, account_id: str, peer_id: str, msg_ids: Iterable[str]) -> None:
        now = _utcnow().isoformat()
        rows = [(account_id, peer_id, msg_id, now) for msg_id in msg_ids]
        if not rows:
            return
        with closing(self._connect()) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO gateway_seen_messages "
                "(account_id, peer_id, msg_id, seen_at) VALUES (?,?,?,?)",
                rows,
            )
            conn.commit()

    # ────────────────────────── 读取游标 ──────────────────────────
    #
    # 微信 UI 里读不到稳定的消息 id，所以用「上一条已处理消息的签名」当游标：
    # 每次读到聊天记录，找到该签名最后出现的位置，取它之后的才算新消息。
    # ★ 首次见到某个联系人时游标为空 —— 此时**一条都不处理**，只把游标对齐到最新，
    #   否则网关一上线就会对着几个月的历史记录逐条回复。

    # ────────────────────────── 会话类型 ──────────────────────────
    #
    # 未读列表里读不出「这是不是群」——那个判断要打开会话才做得了。所以判过一次就记下来，
    # 之后连开都不开：开群窗口本身就是打扰（窗口会弹到前台）。

    def mark_peer_kind(self, account_id: str, peer_id: str, *, is_group: bool) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_peer_kinds (account_id, peer_id, is_group, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(account_id, peer_id) DO UPDATE SET
                  is_group=excluded.is_group,
                  updated_at=excluded.updated_at
                """,
                (account_id, peer_id, 1 if is_group else 0, _utcnow().isoformat()),
            )
            conn.commit()

    def is_known_group(self, account_id: str, peer_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT is_group FROM gateway_peer_kinds WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            ).fetchone()
        return bool(row and row[0])

    def get_cursor(self, account_id: str, peer_id: str) -> tuple[str | None, int]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT last_signature, seq FROM gateway_cursors WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            ).fetchone()
        if not row:
            return None, 0
        # 空签名 = 交回分身时抹掉了（见 `_forget_cursor`），按第一次读算，序号接着用。
        return (row[0] or None), int(row[1])

    @staticmethod
    def _forget_cursor(conn: sqlite3.Connection, account_id: str, peer_id: str) -> None:
        """把这个会话的游标抹掉，下次读它按「第一次读」算（`first_seen_fresh`）。

        ★ 用在**人工接管交回分身**的那一刻。接管期间不轮询（用户 2026-08-28），
        游标停在接管前那条；直接续上的话，分身一睁眼就会拿到招聘老师自己打的那一串
        当「新消息」，还可能去回一句人家已经回过的话。抹掉之后走 `first_seen_fresh`：
        只认尾部今天、对方发的那几条——老师最后说话就什么都不接，候选人最后说话才接上。

        ★ 只抹签名、**序号留着**（2026-09-29）。消息 id 是 ``会话#序号``，去重表
        （``gateway_seen_messages``）永远记着用过的 id；原来整行删掉，序号从 0 重来，
        交回之后候选人说的头几句拿到的正是 ``#1`` ``#2``——早就「见过」了，入站那边
        一声不吭丢掉，下一趟游标又已经翻过去：「读到 5 条，没有新消息」，就是不回。
        """
        conn.execute(
            "UPDATE gateway_cursors SET last_signature='', updated_at=? "
            "WHERE account_id=? AND peer_id=?",
            (_utcnow().isoformat(), account_id, peer_id),
        )

    def clear_cursor(self, account_id: str, peer_id: str) -> None:
        """公开版的 `_forget_cursor`（自己开连接）。"""
        with closing(self._connect()) as conn:
            self._forget_cursor(conn, account_id, peer_id)
            conn.commit()

    def set_cursor(self, account_id: str, peer_id: str, signature: str, seq: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_cursors (account_id, peer_id, last_signature, seq, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(account_id, peer_id) DO UPDATE SET
                  last_signature=excluded.last_signature,
                  seq=excluded.seq,
                  updated_at=excluded.updated_at
                """,
                (account_id, peer_id, signature, seq, _utcnow().isoformat()),
            )
            conn.commit()

    # ────────── 候选人级人工接管（091 / docs/19 v2 §1 / 业务端 v1.5 §4） ──────────

    @staticmethod
    def _mode_row(session_name: str, row: tuple) -> CandidateModeRow:
        mode = str(row[1])
        return CandidateModeRow(
            session_name=session_name,
            candidate_id=str(row[0] or ""),
            mode=mode if mode in CANDIDATE_MODES else AI_MANAGED,
            reason=str(row[2] or ""),
            version=int(row[3] or 0),
            mode_version=int(row[4] or 0),
            trigger_id=str(row[5] or ""),
            updated_at=str(row[6] or ""),
            reported_at=str(row[7]) if row[7] else None,
            source=str(row[8] or ""),
        )

    _MODE_COLS = (
        "candidate_id, mode, reason, version, mode_version, trigger_id, "
        "updated_at, reported_at, source"
    )

    def candidate_mode(self, account_id: str, session_name: str) -> CandidateModeRow:
        """这个候选人现在由谁跟。**重启读回**是这张表存在的唯一理由。

        ★ 从来没设过 → `ai_managed` 且 `known=False`。默认必须是「分身在跟」：
          接管是**例外状态**，默认接管等于装上就不干活。`known=False` 是给界面用的——
          「本机没记录」不能在界面上画成「服务端确认他是托管」。
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {self._MODE_COLS} FROM gateway_candidate_mode "
                "WHERE account_id=? AND session_name=?",
                (account_id, session_name),
            ).fetchone()
        if row is None:
            return CandidateModeRow(session_name, "", AI_MANAGED, "", 0, 0, "", "", None, "", known=False)
        return self._mode_row(session_name, row)

    def candidate_modes(self, account_id: str) -> dict[str, CandidateModeRow]:
        """这台机器上所有有记录的候选人档位，按会话名索引（界面一次画一屏用）。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT session_name, {self._MODE_COLS} FROM gateway_candidate_mode WHERE account_id=?",
                (account_id,),
            ).fetchall()
        return {str(r[0]): self._mode_row(str(r[0]), tuple(r[1:])) for r in rows}

    def set_candidate_mode(
        self,
        account_id: str,
        session_name: str,
        mode: str,
        *,
        candidate_id: str = "",
        reason: str = "",
        version: int = 0,
        mode_version: int = 0,
        trigger_id: str = "",
        reported_at: str | None = None,
    ) -> CandidateModeRow:
        """落本机。**先落这里再谈上报**——即时执行权在这台电脑上。

        `reported_at=None` = 这一次还没 `action=report` 回给业务后端，欠一次回报。
        """
        now = _utcnow().isoformat()
        with closing(self._connect()) as conn:
            # 和同步那条路同一个理由：交回分身时抹游标（见 `_forget_cursor`）。
            resumed = mode == AI_MANAGED and self._mode_of(conn, account_id, session_name) == HUMAN_TAKEOVER
            conn.execute(
                """
                INSERT INTO gateway_candidate_mode
                  (account_id, session_name, candidate_id, mode, reason,
                   version, mode_version, trigger_id, updated_at, reported_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account_id, session_name) DO UPDATE SET
                  candidate_id=excluded.candidate_id,
                  mode=excluded.mode,
                  reason=excluded.reason,
                  version=excluded.version,
                  mode_version=excluded.mode_version,
                  trigger_id=excluded.trigger_id,
                  updated_at=excluded.updated_at,
                  reported_at=excluded.reported_at
                """,
                (
                    account_id, session_name, candidate_id, mode, reason,
                    int(version), int(mode_version), trigger_id, now, reported_at,
                ),
            )
            if resumed:
                self._forget_cursor(conn, account_id, session_name)
                _log.info("交回分身，这个会话按「第一次读」重新接上", session=session_name)
            conn.commit()
        return CandidateModeRow(
            session_name, candidate_id, mode, reason,
            int(version), int(mode_version), trigger_id, now, reported_at,
        )

    def mark_candidate_mode_reported(
        self, account_id: str, session_name: str, at: str | None = None,
    ) -> None:
        """回报成功了。只动 `reported_at`——状态本身不许在这里被改写。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_candidate_mode SET reported_at=? WHERE account_id=? AND session_name=?",
                (at or _utcnow().isoformat(), account_id, session_name),
            )
            conn.commit()

    # ────────────────────────── 旧黑名单（只剩迁移）──────────────────────────
    #
    # ★ 本机拉黑 2026-10-09 整个拆掉（用户：「不应该有拉黑的情况」）。原来 AI 端判对方拒绝 →
    #   这里永久记一行 → 此后他的消息连收都不收、界面上也看不见、没有任何按钮能解开。
    #   于是一句「不想找工作了」就让这个人从此石沉大海。现在同一件事改成**候选人级人工接管**
    #   （`outbound` 收到提案走招聘老师那个按钮的同一条路），看得见、交回分身就能恢复。
    #   表留着只为把老版本写下的那几行迁成接管（`GatewayRunner._migrate_legacy_blacklist`），
    #   迁一行删一行；不再有任何写入方。

    def legacy_blacklist(self, account_id: str) -> list[tuple[str, str]]:
        """老版本本机拉黑的那几行：``[(会话名, 当时的原因)]``。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT peer_id, reason FROM gateway_blacklist WHERE account_id=? ORDER BY created_at",
                (account_id,),
            ).fetchall()
        return [(str(r[0]), str(r[1] or "")) for r in rows]

    def drop_legacy_blacklist(self, account_id: str, peer_id: str) -> None:
        """迁完一行删一行。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM gateway_blacklist WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            )
            conn.commit()

    # ────────────────────────── 好友关系周期（138）──────────────────────────
    #
    # 「这个人刚加上好友」是本机自己确定的事实（我们发的申请被通过 / 我们通过了他的申请），
    # 不是从聊天正文里猜的。AI 端据此把「好友建立」当系统事件，和计划里的主动开场共用一个名额。

    def open_relationship(self, account_id: str, peer_id: str, *, at: datetime | None = None) -> str:
        """记下一次新加上的好友关系，返回它的 id。**每调一次就是新的一次**——只在真加上时调。"""
        when = (at or _utcnow()).isoformat()
        relationship_id = "rel-" + sha256(f"{account_id}\x00{peer_id}\x00{when}\x00{uuid4().hex}".encode()).hexdigest()[:32]
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO gateway_relationships (account_id, peer_id, relationship_id, established_at, announced)
                   VALUES (?, ?, ?, ?, 0)
                   ON CONFLICT(account_id, peer_id) DO UPDATE SET
                     relationship_id = excluded.relationship_id,
                     established_at  = excluded.established_at,
                     announced       = 0""",
                (account_id, peer_id, relationship_id, when),
            )
            conn.commit()
        return relationship_id

    def pending_relationship(
        self, account_id: str, peer_id: str, *, now: datetime | None = None, within: timedelta = timedelta(days=3),
    ) -> tuple[str, datetime] | None:
        """还没告诉过 AI 端的那次加好友（太久以前的不算——那早就不是「刚加上」了）。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT relationship_id, established_at FROM gateway_relationships
                   WHERE account_id=? AND peer_id=? AND announced=0""",
                (account_id, peer_id),
            ).fetchone()
        if not row:
            return None
        try:
            established = datetime.fromisoformat(row[1])
        except ValueError:
            return None
        if established.tzinfo is None:
            established = established.replace(tzinfo=timezone.utc)
        if (now or _utcnow()) - established > within:
            return None
        return str(row[0]), established

    def mark_relationship_announced(self, account_id: str, peer_id: str, relationship_id: str) -> None:
        """AI 端已收到这次加好友的事件。只认同一个 id——中间又删了重加的那次不能被顺手销掉。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_relationships SET announced=1 WHERE account_id=? AND peer_id=? AND relationship_id=?",
                (account_id, peer_id, relationship_id),
            )
            conn.commit()

    def last_inbound_at(
        self, account_id: str, peer_id: str, *, ignore_texts: Iterable[str] = (),
    ) -> datetime | None:
        """对方最近一条消息被本机读到的时刻。开场指令执行前拿它判「他是不是已经开口了」。

        不算的：专员和分身自己的、微信系统提示行、以及 ``ignore_texts``（对方通过申请时微信替他发的
        那句固定话——那不是他开口，算进去会把刚派下来的开场误撤掉）。
        """
        ignored = sorted({text_key(t) for t in ignore_texts})
        marks = ",".join("?" for _ in ignored)
        clause = f" AND raw_key NOT IN ({marks})" if ignored else ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MAX(at) FROM gateway_messages WHERE account_id=? AND peer_id=? AND from_self=0 AND system_tip=0" + clause,
                (account_id, peer_id, *ignored),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            value = datetime.fromisoformat(row[0])
        except ValueError:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    # ────────────────────────── 指令幂等（066）──────────────────────────
    #
    # AI 端重推同一条指令（网络重试、网关重连后补推）**不能再发一遍消息**。
    # 结果落库而不是只放内存：进程重启后重推的那条，仍要认得出是老指令。

    def get_command_result(self, account_id: str, command_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT status, reason, result FROM gateway_commands "
                "WHERE account_id=? AND command_id=?",
                (account_id, command_id),
            ).fetchone()
        if not row:
            return None
        return {
            "status": row[0],
            "reason": row[1],
            "result": json.loads(row[2]) if row[2] else None,
        }

    def put_command_result(self, account_id: str, command_id: str, outcome: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_commands (account_id, command_id, status, reason, result, done_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(account_id, command_id) DO UPDATE SET
                  status=excluded.status, reason=excluded.reason,
                  result=excluded.result, done_at=excluded.done_at
                """,
                (
                    account_id, command_id,
                    str(outcome.get("status") or ""), str(outcome.get("reason") or ""),
                    json.dumps(outcome.get("result"), ensure_ascii=False)
                    if outcome.get("result") is not None else None,
                    _utcnow().isoformat(),
                ),
            )
            conn.commit()

    # ────────────────────────── 定时指令（077）──────────────────────────
    #
    # `schedule_at` 早就在契约里透传到网关了，但一直没人落表执行——于是需求文档 §6.19
    # 的定时/周期/条件触发全都不可用，面试前提醒、未回复跟进、内容日历都无从谈起。
    #
    # 到点执行仍走 `CommandExecutor`，幂等（gateway_commands）和守门（频次/时间窗/人工接管）
    # 一并复用，不另开一套。

    def put_scheduled_command(
        self, account_id: str, command_id: str, payload: dict, due_at: datetime,
        at: datetime | None = None,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_scheduled_commands
                  (command_id, account_id, payload, due_at, created_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(command_id) DO UPDATE SET
                  payload=excluded.payload, due_at=excluded.due_at
                """,
                (
                    command_id, account_id,
                    json.dumps(payload, ensure_ascii=False),
                    due_at.isoformat(), (at or _utcnow()).isoformat(),
                ),
            )
            conn.commit()

    def due_scheduled_commands(self, account_id: str, now: datetime) -> list[dict]:
        """已经到点的那些，最早的排前面。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT command_id, payload, due_at FROM gateway_scheduled_commands "
                "WHERE account_id=? AND due_at<=? ORDER BY due_at",
                (account_id, now.isoformat()),
            ).fetchall()
        out = []
        for row in rows:
            try:
                payload = json.loads(row[1])
            except ValueError:
                continue
            out.append({"command_id": row[0], "payload": payload, "due_at": row[2]})
        return out

    def drop_scheduled_command(self, command_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM gateway_scheduled_commands WHERE command_id=?", (command_id,)
            )
            conn.commit()

    def count_scheduled_commands(self) -> int:
        """还排着几条。界面每秒都要问一次，走 COUNT。"""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) FROM gateway_scheduled_commands").fetchone()
        return int(row[0]) if row else 0

    # ────────────────────────── 监听名单（069）──────────────────────────
    #
    # 名单的权威在 AI 端，但**必须落盘**：进程重启后如果要等 AI 端重推才恢复，
    # 中间那段网关是全哑的（名单为空 = 一个会话都不碰）。存一行覆盖一行。

    def get_watchlist(self) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT payload FROM gateway_watchlist WHERE id=1").fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def put_watchlist(self, payload: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_watchlist (id, payload, updated_at) VALUES (1,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (json.dumps(payload, ensure_ascii=False), _utcnow().isoformat()),
            )
            conn.commit()

    # ────────────────────────── 入站待处理队列（070）──────────────────────────
    #
    # ★ 为什么必须落盘：游标是在**读到消息的那一刻**推进的（`wechat_io._fetch_peer`），
    #   而这批消息要经过去抖缓冲、再交给 AI 端跑一轮才算处理完。中间任何一步断掉——
    #   AI 端连不上、票据过期、进程被关——这批消息就**同时**满足「游标已经翻过去了」
    #   和「谁也没处理」，于是候选人永远等不到回复，日志上只有一行 warning
    #   （2026-08-13 真机：票据过期那段窗口就是这么静悄悄丢掉的）。
    #
    #   所以：入缓冲的同时落一行，处理成功才删。进程重启、AI 端恢复之后照原样重投。
    #   这就是需求文档 §2.4 要的「网络中断保留短期加密队列，恢复后幂等回传」——
    #   幂等由 `gateway_seen_messages` 保证（重投的还是同一批 msg_id）。

    def put_pending_inbound(
        self,
        account_id: str,
        peer_id: str,
        batch_id: str,
        payload: list[dict],
        at: datetime | None = None,
    ) -> None:
        """``at`` 由调用方传入循环自己的时钟——保鲜期是拿同一个时钟量的，
        用 `_utcnow()` 会让两个时钟对不上（测试里立刻暴露，生产上则是悄悄失准）。"""
        now = (at or _utcnow()).isoformat()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_pending_inbound
                  (batch_id, account_id, peer_id, payload, attempts, created_at, updated_at)
                VALUES (?,?,?,?,0,?,?)
                ON CONFLICT(batch_id) DO UPDATE SET
                  payload=excluded.payload, updated_at=excluded.updated_at
                """,
                (batch_id, account_id, peer_id, json.dumps(payload, ensure_ascii=False), now, now),
            )
            conn.commit()

    def list_pending_inbound(self, account_id: str | None = None) -> list[dict]:
        """待处理批次，最早的排前面（先来的先补上）。"""
        sql = (
            "SELECT batch_id, account_id, peer_id, payload, attempts, created_at "
            "FROM gateway_pending_inbound"
        )
        params: tuple = ()
        if account_id is not None:
            sql += " WHERE account_id=?"
            params = (account_id,)
        sql += " ORDER BY created_at"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "batch_id": row[0],
                "account_id": row[1],
                "peer_id": row[2],
                "payload": json.loads(row[3]),
                "attempts": int(row[4]),
                "created_at": row[5],
            }
            for row in rows
        ]

    def count_pending_inbound(self) -> int:
        """还欠着几批。界面每秒都要问一次，所以走 COUNT 而不是把整张表反序列化出来。"""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) FROM gateway_pending_inbound").fetchone()
        return int(row[0]) if row else 0

    def drop_pending_inbound(self, batch_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM gateway_pending_inbound WHERE batch_id=?", (batch_id,))
            conn.commit()

    def bump_pending_attempts(self, batch_id: str) -> int:
        """记一次失败，返回累计失败次数。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_pending_inbound SET attempts=attempts+1, updated_at=? "
                "WHERE batch_id=?",
                (_utcnow().isoformat(), batch_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT attempts FROM gateway_pending_inbound WHERE batch_id=?", (batch_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def purge_pending_inbound_before(self, cutoff: datetime) -> list[dict]:
        """清掉过期的待处理批次，返回被清掉的那些（调用方要把它们记进日志）。

        队列是**短期**的：隔了一天还没送出去的消息，这时候再回过去反而更奇怪。
        但清掉这件事必须留痕——静悄悄丢消息正是这一整块要根治的毛病。
        """
        stale = [
            row for row in self.list_pending_inbound()
            if row["created_at"] < cutoff.isoformat()
        ]
        if not stale:
            return []
        with closing(self._connect()) as conn:
            conn.executemany(
                "DELETE FROM gateway_pending_inbound WHERE batch_id=?",
                [(row["batch_id"],) for row in stale],
            )
            conn.commit()
        return stale

    # ──────────────── 微信消息事件发件箱（未回复跟进的事实来源）────────────────

    def put_message_event(
        self, account_id: str, event_id: str, payload: dict, at: datetime | None = None
    ) -> bool:
        """把一条待上报的消息事件排进发件箱。已经排过（或已发过又重排）返回 ``False``。

        **为什么要落盘**：这条事件是业务后端计算「多久没回」的唯一输入。掉一条的代价不是
        少一行日志，而是候选人明明回了话、四小时后还被追一条（或者我方发了消息、跟进却
        永远不排期）。网络抖一下就丢，正是这一整块要根治的毛病。

        ``event_id`` 是**幂等键**（由内容派生，见 ``message_events.event_id_for``）：
        同一条微信消息重排多少次都只有一行，断网重试时后端也据它去重。
        """
        now = (at or _utcnow()).isoformat()
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO gateway_message_events
                  (event_id, account_id, payload, attempts, created_at, updated_at)
                VALUES (?,?,?,0,?,?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, account_id, json.dumps(payload, ensure_ascii=False), now, now),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_message_events(self, limit: int = 50) -> list[dict]:
        """待上报的事件，最早的排前面（先发生的先报）。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT event_id, account_id, payload, attempts, created_at "
                "FROM gateway_message_events ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "event_id": row[0],
                "account_id": row[1],
                "payload": json.loads(row[2]),
                "attempts": int(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]

    def count_message_events(self) -> int:
        """发件箱里还压着几条。只读，给日志/界面报数用。"""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) FROM gateway_message_events").fetchone()
        return int(row[0]) if row else 0

    def drop_message_event(self, event_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM gateway_message_events WHERE event_id=?", (event_id,))
            conn.commit()

    def bump_message_event_attempts(self, event_id: str) -> int:
        """记一次上报失败，返回累计失败次数。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_message_events SET attempts=attempts+1, updated_at=? "
                "WHERE event_id=?",
                (_utcnow().isoformat(), event_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT attempts FROM gateway_message_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def purge_message_events_before(self, cutoff: datetime) -> list[dict]:
        """清掉过期的待上报事件，返回被清掉的那些（调用方必须记进日志）。

        隔了一天还没报出去的事件，后端拿它算出来的跟进时点早就过了。但清掉必须留痕——
        静悄悄丢事件和静悄悄丢消息是同一种病。
        """
        stale = [
            row for row in self.list_message_events(limit=10_000)
            if row["created_at"] < cutoff.isoformat()
        ]
        if not stale:
            return []
        with closing(self._connect()) as conn:
            conn.executemany(
                "DELETE FROM gateway_message_events WHERE event_id=?",
                [(row["event_id"],) for row in stale],
            )
            conn.commit()
        return stale

    # ────────────────────────── 指令回执发件箱 ──────────────────────────

    def queue_receipt(
        self, account_id: str, command_id: str, payload: dict, at: datetime | None = None
    ) -> None:
        """把一条**终态**回执排进发件箱（同一条指令覆盖，终态只有一个）。

        **为什么要落盘**：这条回执是 AI 端「这条计划项到底发出去没有」的唯一凭据
        （`docs/17 §5`：不要拿 `done` 当凭据，等 `wechat_command_result`）。原来发不出去
        就只写一行日志、没人再补——那一条计划项在 AI 端会一直挂在在途表里，业务后端
        等到超时重投又被幂等挡掉，于是**永远发不出去且被记成已处理**。

        排进来时先写盘再发：发到一半进程被杀，重启后这条还在。
        """
        now = (at or _utcnow()).isoformat()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO gateway_receipt_outbox
                  (account_id, command_id, payload, attempts, created_at, updated_at)
                VALUES (?,?,?,0,?,?)
                ON CONFLICT(account_id, command_id) DO UPDATE SET
                  payload = excluded.payload,
                  updated_at = excluded.updated_at
                """,
                (account_id, command_id, json.dumps(payload, ensure_ascii=False), now, now),
            )
            conn.commit()

    def list_receipts(self, limit: int = 50) -> list[dict]:
        """待补发的回执，最早的排前面。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT account_id, command_id, payload, attempts, created_at "
                "FROM gateway_receipt_outbox ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            {
                "account_id": row[0],
                "command_id": row[1],
                "payload": json.loads(row[2]),
                "attempts": int(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]

    def count_receipts(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) FROM gateway_receipt_outbox").fetchone()
        return int(row[0]) if row else 0

    def drop_receipt(self, account_id: str, command_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM gateway_receipt_outbox WHERE account_id=? AND command_id=?",
                (account_id, command_id),
            )
            conn.commit()

    def bump_receipt_attempts(self, account_id: str, command_id: str) -> int:
        """记一次补发失败，返回累计失败次数。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_receipt_outbox SET attempts=attempts+1, updated_at=? "
                "WHERE account_id=? AND command_id=?",
                (_utcnow().isoformat(), account_id, command_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT attempts FROM gateway_receipt_outbox WHERE account_id=? AND command_id=?",
                (account_id, command_id),
            ).fetchone()
        return int(row[0]) if row else 0

    def purge_receipts_before(self, cutoff: datetime) -> list[dict]:
        """清掉过期的回执，返回被清掉的那些（调用方必须记进日志）。

        ★ 截止时间要跟 AI 端在途表的存活期对齐（`wechatChannel.ts` 的 `PENDING_TTL_MS`，24h）：
        那边已经忘了这条指令，再补发只会落在「wechat result for unknown command」上。
        但清掉必须留痕——静悄悄丢回执和静悄悄丢消息是同一种病。
        """
        stale = [
            row for row in self.list_receipts(limit=10_000)
            if row["created_at"] < cutoff.isoformat()
        ]
        if not stale:
            return []
        with closing(self._connect()) as conn:
            conn.executemany(
                "DELETE FROM gateway_receipt_outbox WHERE account_id=? AND command_id=?",
                [(row["account_id"], row["command_id"]) for row in stale],
            )
            conn.commit()
        return stale

    # ────────────────────────── 聊天记录（喂给 AI 端判进度）──────────────────────────

    def record_messages(self, account_id: str, peer_id: str, rows: Iterable[dict]) -> int:
        """把这个会话里**看到过**的消息记下来（双方都记），返回真正新增了几条。

        为什么要记：AI 端每一轮只收得到「这一趟去抖攒下的那几条」，而它要判的是
        「这一步走完没有」——对方上周答应过来看看、专员前天已经问过工种，这些都在更早的
        轮次里。不留一份，换个会话或者隔一天，那些事就等于没发生过。

        只记**这台机器亲眼读到的**：微信里更早的聊天记录我们没读过，也不去翻。
        重复推同一条不会记两遍（PK 就是 msg_id）。

        ``raw_key`` 是这条在微信里**读到的原始文字**的键（图片气泡是「图片」两个字，不是我们
        写进 ``text`` 的占位句）：下一次从微信读回同一条时按它认得出来（`recent_text_keys`）。
        """
        values = [
            (
                account_id,
                peer_id,
                str(row.get("msg_id") or ""),
                str(row.get("at") or ""),
                str(row.get("at_text") or ""),
                1 if row.get("from_self") else 0,
                1 if row.get("from_gateway") else 0,
                str(row.get("text") or ""),
                text_key(row.get("raw_text") or row.get("text") or ""),
                1 if row.get("system_tip") else 0,
            )
            for row in rows
            if row.get("msg_id")
        ]
        if not values:
            return 0
        with closing(self._connect()) as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT OR IGNORE INTO gateway_messages
                       (account_id, peer_id, msg_id, at, at_text, from_self, from_gateway, text, raw_key, system_tip)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            conn.commit()
            return conn.total_changes - before

    def recent_messages(
        self,
        account_id: str,
        peer_id: str,
        *,
        limit: int = 40,
        exclude: Iterable[str] = (),
    ) -> list[dict]:
        """这个会话最近的几条，**时间正序**（老→新）。

        ``exclude`` 用来把「这一轮本来就要发过去的那几条」摘掉——它们已经在本轮上下文里，
        再当历史发一遍，AI 端会看到同一句话出现两次。
        """
        skip = {str(x) for x in exclude}
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT msg_id, at, at_text, from_self, from_gateway, text FROM gateway_messages
                   WHERE account_id=? AND peer_id=?
                   ORDER BY at DESC, rowid DESC LIMIT ?""",
                (account_id, peer_id, max(1, limit) + len(skip)),
            ).fetchall()
        out = [
            {
                "msg_id": row[0],
                "at": row[1],
                "at_text": row[2],
                "from_self": bool(row[3]),
                "from_gateway": bool(row[4]),
                "text": row[5],
            }
            for row in rows
            if row[0] not in skip
        ]
        return list(reversed(out[:limit]))

    def recent_raw_keys(self, account_id: str, peer_id: str, *, limit: int = 2) -> list[str]:
        """这个会话最近处理过的几条消息**在微信里长什么样**（内容键，老→新）。

        给 `wechat_io.split_new_messages` 当第二道锚：游标是按「上一窗最后一条」的签名存的，
        聊天窗口一旦被人翻上去，读到的那一窗里没有它，游标就对不回来。而**这台机器处理过哪几条
        我们是确定知道的**——最后处理的那两条在刚读到的窗口里出现在哪儿，那儿之前的就都不是新的
        （2026-09-23 线上事故：一窗旧消息把游标带回 09:03，下一趟把 09:03–09:09 整段当新消息
        重跑了一遍，候选人的问题被回了两遍）。

        老行（升级前记的）没有 ``raw_key``，退回按 ``text`` 算——文字消息两者本来就相同。
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT raw_key, text FROM gateway_messages
                   WHERE account_id=? AND peer_id=?
                   ORDER BY at DESC, rowid DESC LIMIT ?""",
                (account_id, peer_id, max(1, limit)),
            ).fetchall()
        keys = [str(raw_key or "") or text_key(text) for raw_key, text in rows]
        return [key for key in reversed(keys) if key]

    def said_by_self(self, account_id: str, peer_id: str, text: str, *, limit: int = 10) -> bool:
        """这句话在本机最近记下的几条里最近一次出现，是不是**自己**说的（专员或网关发的）。

        给「最后一条是对方的就补一轮」（`wechat_io._owed_tail`）挡自问自答：UI 偶尔把
        我们自己的气泡判成对方的，认亲台账又早在上一趟销过了。
        """
        key = text_key(text)
        if not key:
            return False
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT raw_key, text, from_self FROM gateway_messages
                   WHERE account_id=? AND peer_id=?
                   ORDER BY at DESC, rowid DESC LIMIT ?""",
                (account_id, peer_id, max(1, limit)),
            ).fetchall()
        # 看**最近一次**出现：专员先说「你好」、候选人再回一句「你好」，后一句是对方的。
        for raw_key, row_text, from_self in rows:
            if (str(raw_key or "") or text_key(row_text)) == key:
                return bool(from_self)
        return False

    def recent_peer_activity(self, account_id: str, *, since: datetime) -> dict[str, datetime]:
        """``since`` 之后**这台机器跟哪些会话有过来往**：会话名 → 最近一次来往的时刻（UTC）。

        给 `wechat_io.QueueMessageSource._due_peers` 判「谁正热着」：名单一长，每轮只读得起
        几个窗口，得把有限的窗口先给正在聊的人——对方刚说过话、或我们刚回过话正等他回，
        这两种都算来往。读到的消息在 ``gateway_messages``（双方都记）；刚发出去、还没从微信
        读回来的那句在 ``gateway_sent_echo``（读回即销），两张表取最近的那一笔。

        只按时间筒选，不管内容；``since`` 之前的会话一律不出现在结果里。
        """
        edge = since.isoformat()
        activity: dict[str, datetime] = {}

        def note(peer_id: str, raw: str) -> None:
            try:
                at = datetime.fromisoformat(str(raw or ""))
            except ValueError:
                return
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if at < since:
                return
            current = activity.get(peer_id)
            if current is None or at > current:
                activity[peer_id] = at

        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT peer_id, MAX(at) FROM gateway_messages
                   WHERE account_id=? AND at >= ? GROUP BY peer_id""",
                (account_id, edge),
            ).fetchall()
            echoes = conn.execute(
                """SELECT peer_id, MAX(sent_at) FROM gateway_sent_echo
                   WHERE account_id=? AND sent_at >= ? GROUP BY peer_id""",
                (account_id, edge),
            ).fetchall()
        for peer_id, raw in list(rows) + list(echoes):
            note(str(peer_id), raw)
        return activity

    def purge_messages_before(self, cutoff: datetime) -> int:
        """清掉太老的聊天记录，返回清了几条。

        这份记录是**给 AI 端当短期背景**用的，不是聊天记录归档——留着不清，一台跑几个月的
        机器会把候选人的对话原文攒成一个越来越大的本地文件（真实 PII）。
        """
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM gateway_messages WHERE at < ?", (cutoff.isoformat(),))
            conn.commit()
            return cur.rowcount or 0

    # ────────────────────────── 自己发过的原文（读回来认亲）──────────────────────────

    def remember_sent_text(
        self, account_id: str, peer_id: str, text: str, at: datetime | None = None
    ) -> None:
        """记下「我们刚给这个会话发了这句原文」，等它被读回来时认亲（`claim_sent_echo`）。

        ★ 为什么要有这张表（2026-09-09 真机事故）：网关发出去的回复，下一轮从微信读回来时
        「是不是我发的」全靠 `uia.py` 的启发式（气泡左右位置 / 截图 / 右键菜单），任何一条
        判错就成了「对方的新消息」——网关对着自己的话再回一轮，自问自答刷屏。
        而**我们自己发了什么，我们是确定知道的**，不该把这件事交给 UI 去猜。
        只记归一化后的文本（`_echo_key`）：微信显示时会吞掉首尾空白、折叠换行。
        """
        key = _echo_key(text)
        if not key:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO gateway_sent_echo (account_id, peer_id, text_key, sent_at) "
                "VALUES (?,?,?,?)",
                (account_id, peer_id, key, (at or _utcnow()).isoformat()),
            )
            conn.commit()

    def claim_sent_echo(self, account_id: str, peer_id: str, text: str) -> bool:
        """读回来的这一条是不是我们自己刚发的原文。**认一次销一条**。

        一条记录只能认一次：对方要是原样复读了我们那句（「好的」「收到」），
        第一次匹配的是我们的回声（它在时间线上一定排在前面），第二次就是对方真说的，
        不能再被吞掉。所以匹配到就删掉最早那一条。
        """
        key = _echo_key(text)
        if not key:
            return False
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT rowid FROM gateway_sent_echo
                   WHERE account_id=? AND peer_id=? AND text_key=?
                   ORDER BY sent_at ASC, rowid ASC LIMIT 1""",
                (account_id, peer_id, key),
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM gateway_sent_echo WHERE rowid=?", (row[0],))
            conn.commit()
            return True

    def purge_sent_echo_before(self, cutoff: datetime) -> int:
        """清掉太久没被读回来的记录（发送成功但微信没显示、读取窗口滚过去了）。

        留着不清，几天后对方恰好说了同一句就会被当成我们的回声吞掉一次。
        """
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM gateway_sent_echo WHERE sent_at < ?", (cutoff.isoformat(),))
            conn.commit()
            return cur.rowcount or 0

    # ────────────────────────── 发送计数（频次守门）──────────────────────────

    def record_send(
        self,
        account_id: str,
        peer_id: str,
        at: datetime | None = None,
        action: str = "message",
    ) -> None:
        """记一次写动作。``action`` 决定它计进哪一档配额（072）。"""
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO gateway_send_log (account_id, peer_id, sent_at, action) "
                "VALUES (?,?,?,?)",
                (account_id, peer_id, (at or _utcnow()).isoformat(), action),
            )
            conn.commit()

    def reserve_sends(
        self,
        account_id: str,
        peer_id: str,
        at: datetime,
        action: str,
        count: int,
        checks: Sequence[tuple[str, str | None, datetime, int]],
        *,
        clamp: bool = False,
    ) -> tuple[list[int], int, tuple[str, int, int] | None]:
        """**先预留后执行**：在一个事务里把额度数清楚并当场占住，返回占住的行号。

        为什么必须是一个事务：原来是「先 check 再 submit 再 record_send」，两次数之间
        隔着一整个微信动作。并发下发时一批指令全读到同一个旧计数，然后集体放行——
        实测并发 20 条加好友 done=20/rejected=0，而串行是 5/15。守门在真实形态下等于没有。

        `checks` = 若干 `(标签, action|None, since, max_count)`；`action=None` 表示总闸（不分档）。
        `clamp=True` 时额度不够就**减量放行**（`accept_new_friends` 那种「最多 N 个」），
        否则要么全额占住、要么一个都不占（回复分段：发一半比不发更糟）。

        返回 `(rowids, granted, 卡在哪一档)`。`granted == 0` = 没占到，调用方按拒绝处理；
        第三项是 `(标签, 已用, 上限)`，用来把拒绝理由说具体。
        """
        count = max(int(count), 0)
        if count == 0:
            return [], 0, None
        stamp = at.isoformat()
        with closing(self._connect()) as conn:
            # IMMEDIATE：一上来就拿写锁，别的连接只能等这条提交完再数——
            # 这正是「判一次记一次」缺的那个原子性。
            conn.execute("BEGIN IMMEDIATE")
            try:
                granted = count
                binding: tuple[str, int, int] | None = None
                for label, scoped, since, max_count in checks:
                    sql = "SELECT COUNT(*) FROM gateway_send_log WHERE account_id=? AND sent_at>=?"
                    params: tuple = (account_id, since.isoformat())
                    if scoped is not None:
                        sql += " AND action=?"
                        params = (*params, scoped)
                    used = int(conn.execute(sql, params).fetchone()[0])
                    room = max(max_count - used, 0)
                    if room < granted:
                        granted, binding = room, (label, used, max_count)
                if granted <= 0 or (granted < count and not clamp):
                    conn.rollback()
                    return [], 0, binding
                rowids = []
                for _ in range(granted):
                    cur = conn.execute(
                        "INSERT INTO gateway_send_log (account_id, peer_id, sent_at, action) "
                        "VALUES (?,?,?,?)",
                        (account_id, peer_id, stamp, action),
                    )
                    rowids.append(int(cur.lastrowid or 0))
                conn.commit()
                return rowids, granted, binding
            except Exception:
                conn.rollback()
                raise

    def release_sends(self, rowids: Sequence[int]) -> None:
        """把没用上的预留还回去（动作没做成 / 做得比预留的少）。"""
        ids = [int(r) for r in rowids if r]
        if not ids:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                f"DELETE FROM gateway_send_log WHERE rowid IN ({','.join('?' * len(ids))})",
                ids,
            )
            conn.commit()

    def try_touch_read(
        self, account_id: str, action: str, at: datetime, min_interval_seconds: float
    ) -> float:
        """读类动作的**最小间隔**闸。返回还要等几秒；`0` = 现在可以，并已记下这一次。

        读不打扰任何人，但一样是「机器在动微信」——半夜每秒翻一次联系人，风控看的就是
        这个。所以读也要有节拍，只是它管的是**间隔**而不是次数（`gateway_send_log` 那边
        是写动作的额度，读不进那本账，免得把写的总闸挤掉）。
        """
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT last_at FROM gateway_read_marks WHERE account_id=? AND action=?",
                    (account_id, action),
                ).fetchone()
                if row and row[0]:
                    try:
                        last = datetime.fromisoformat(str(row[0]))
                    except ValueError:
                        last = None
                    if last is not None:
                        waited = (at - last).total_seconds()
                        if waited < min_interval_seconds:
                            conn.rollback()
                            return round(min_interval_seconds - waited, 1)
                conn.execute(
                    """INSERT INTO gateway_read_marks (account_id, action, last_at) VALUES (?,?,?)
                       ON CONFLICT(account_id, action) DO UPDATE SET last_at = excluded.last_at""",
                    (account_id, action, at.isoformat()),
                )
                conn.commit()
                return 0.0
            except Exception:
                conn.rollback()
                raise

    def seconds_since_read(self, account_id: str, action: str, at: datetime) -> float | None:
        """这一档上次动微信是多久以前（秒）。没记录过 → None。**只看不记**。

        `try_touch_read` 是「问一句顺手就占住」，谁问谁就把节拍推后了。想让 A 给 B 让路，
        A 只能用这个：占住的事得留给真要动微信的那一方。
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT last_at FROM gateway_read_marks WHERE account_id=? AND action=?",
                (account_id, action),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            last = datetime.fromisoformat(str(row[0]))
        except ValueError:
            return None
        return (at - last).total_seconds()

    # ────────────────────────── 朋友圈巡看游标 ──────────────────────────
    # 上一趟看到的第一条动态的键。下一趟翻到它就停 = 只碰新出的。

    def moment_cursor(self, account_id: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT first_key FROM gateway_moment_cursor WHERE account_id=?", (account_id,),
            ).fetchone()
        return str(row[0]) if row else ""

    def set_moment_cursor(self, account_id: str, first_key: str, at: datetime) -> None:
        if not first_key:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gateway_moment_cursor (account_id, first_key, seen_at) VALUES (?,?,?)",
                (account_id, first_key, at.isoformat()),
            )
            conn.commit()

    # ────────────────────────── 朋友圈评论台账 ──────────────────────────
    # 同一条动态只评一次。计划重投、AI 端读回同一批动态再写一遍，都靠这本账挡：
    # 微信上看不出「我们评过没有」（评论区只显示昵称，本机不可靠地知道自己叫什么）。

    def commented_moment_keys(self, account_id: str, keys: Iterable[str], since: datetime) -> set[str]:
        wanted = [k for k in keys if k]
        if not wanted:
            return set()
        marks = ",".join("?" * len(wanted))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT content_key FROM gateway_moment_comments "
                f"WHERE account_id=? AND created_at>=? AND content_key IN ({marks})",
                (account_id, since.isoformat(), *wanted),
            ).fetchall()
        return {str(r[0]) for r in rows}

    def record_moment_comments(self, account_id: str, keys: Iterable[str], at: datetime) -> None:
        rows = [(account_id, k, at.isoformat()) for k in keys if k]
        if not rows:
            return
        with closing(self._connect()) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO gateway_moment_comments (account_id, content_key, created_at) "
                "VALUES (?,?,?)",
                rows,
            )
            # 顺手清掉早于 30 天的：只认「今天」的动态，台账不用留更久。
            conn.execute(
                "DELETE FROM gateway_moment_comments WHERE account_id=? AND created_at<?",
                (account_id, (at - timedelta(days=30)).isoformat()),
            )
            conn.commit()

    def sends_since(self, account_id: str, since: datetime, action: str | None = None) -> int:
        """``action=None`` = 所有写动作（总闸）；给了就只数那一档。"""
        sql = "SELECT COUNT(*) FROM gateway_send_log WHERE account_id=? AND sent_at>=?"
        params: tuple = (account_id, since.isoformat())
        if action is not None:
            sql += " AND action=?"
            params = (*params, action)
        with closing(self._connect()) as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def sends_in_last_hour(self, account_id: str, now: datetime | None = None) -> int:
        return self.sends_since(account_id, (now or _utcnow()) - timedelta(hours=1))
