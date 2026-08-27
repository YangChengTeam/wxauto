"""网关本地状态：会话映射、消息去重游标、黑名单、发送计数。

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
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS gateway_strangers (
  account_id   TEXT NOT NULL,
  session_name TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  PRIMARY KEY (account_id, session_name)
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
  -- 服务端 aiRecruitment.version（乐观锁用，下次 PUT 当 expectedVersion）
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
CREATE TABLE IF NOT EXISTS gateway_read_marks (
  account_id TEXT NOT NULL,
  action     TEXT NOT NULL,
  last_at    TEXT NOT NULL,
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
  text       TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (account_id, peer_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_peer_time
  ON gateway_messages (account_id, peer_id, at);
"""


_log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """幂等加列。SQLite 的 ADD COLUMN 不动既有数据，只给老行填默认值。"""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
        return True

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

    # ───────────── 陌生人主动发起（spec 090 / v1 §9.5）─────────────

    def note_strangers(
        self,
        account_id: str,
        names: Iterable[str],
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[str]:
        """会话列表里出现了、但本机会话名映射里没有的名字 —— 记下来。返回**新**记下的那些。

        为什么要单独一张表：候选人主动加好友、主动发第一句话时，他既不在任何招聘计划里、
        也没有候选人档案，因此 `gateway_contacts` 里没有他 —— 而监听名单正是从那张表派生的
        （spec 085）。没有这一层，**他的消息网关根本读不到**，§9.5 那条「主动推进」无从谈起。

        **有上限**：这是给「刚来的陌生人」用的临时准入，不是第二份名单。满了就不再收新的，
        老的由 `purge_strangers_before` 按保鲜期清掉。
        """
        stamp = (now or _utcnow()).isoformat()
        added: list[str] = []
        with closing(self._connect()) as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    "SELECT session_name FROM gateway_strangers WHERE account_id=?", (account_id,)
                ).fetchall()
            }
            for raw in names:
                name = str(raw or "").strip()
                if not name:
                    continue
                if name in existing:
                    conn.execute(
                        "UPDATE gateway_strangers SET last_seen_at=? WHERE account_id=? AND session_name=?",
                        (stamp, account_id, name),
                    )
                    continue
                if len(existing) >= limit:
                    break
                existing.add(name)
                added.append(name)
                conn.execute(
                    """INSERT INTO gateway_strangers (account_id, session_name, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?)""",
                    (account_id, name, stamp, stamp),
                )
            conn.commit()
        return added

    def stranger_names(self, account_id: str) -> list[str]:
        """当前临时准入的陌生会话名。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT session_name FROM gateway_strangers
                   WHERE account_id=? ORDER BY session_name""",
                (account_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def drop_strangers(self, account_id: str, names: Iterable[str]) -> int:
        """不再当陌生人（已经归位成候选人，或专员说了别理他）。"""
        wanted = [str(n or "").strip() for n in names]
        wanted = [n for n in wanted if n]
        if not wanted:
            return 0
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"""DELETE FROM gateway_strangers WHERE account_id=?
                    AND session_name IN ({",".join("?" * len(wanted))})""",
                (account_id, *wanted),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def purge_strangers_before(self, account_id: str, cutoff: datetime) -> list[str]:
        """过了保鲜期的临时准入清掉。返回清掉的名字。

        没有这一条，一台机器跑上几个月就会攒出一份谁也没审过的长名单 ——
        而名单的每一项都意味着「这个人会被自动回复」。
        """
        stamp = cutoff.isoformat()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT session_name FROM gateway_strangers WHERE account_id=? AND last_seen_at < ?",
                (account_id, stamp),
            ).fetchall()
            conn.execute(
                "DELETE FROM gateway_strangers WHERE account_id=? AND last_seen_at < ?",
                (account_id, stamp),
            )
            conn.commit()
        return [str(row[0]) for row in rows]

    def known_session_names(self, account_id: str) -> set[str]:
        """本机会话名映射里已有的名字（不分 accepted/requested）。

        判「是不是陌生人」用的是**这一份**，不是监听名单：刚发出好友申请、还没通过的那些
        （`requested`）也已经在册，他们通过之后发来的消息不该被当成陌生人二次准入。
        """
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

    def note_friend_request(self, account_id: str, number: str, session_name: str) -> None:
        """记下「我们刚给这个号发了好友申请，备注名是这个」。

        为的是**以后能认出对方通过了**：通过之后我们收到的第一条消息只带会话名，
        而上报要的是 `number`（api2 §4.1 必填）——两者的桥就是这一行。
        重启也要还在，所以落库不放内存。服务端同步回来的行会覆盖它（那边才是权威）。
        """
        number, session_name = (number or "").strip(), (session_name or "").strip()
        if not number or not session_name:
            return
        with closing(self._connect()) as conn:
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
        """这个会话名对应的号，如果我们还在等他通过的话。不等了或不认识 → 空串。"""
        session_name = (session_name or "").strip()
        if not session_name:
            return ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT number FROM gateway_contacts
                   WHERE account_id = ? AND session_name = ? AND status = 'requested' LIMIT 1""",
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

    def mark_contact_status(self, account_id: str, number: str, status: str) -> None:
        """把本机记的好友状态推进一格（只动状态，不碰会话名，也不动同步水位）。"""
        number, status = (number or "").strip(), (status or "").strip()
        if not number or not status:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE gateway_contacts SET status = ? WHERE account_id = ? AND number = ?",
                (status, account_id, number),
            )
            conn.commit()

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
        return (row[0], int(row[1])) if row else (None, 0)

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

    # ────────────────────────── 黑名单 ──────────────────────────

    def is_blacklisted(self, account_id: str, peer_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM gateway_blacklist WHERE account_id=? AND peer_id=?",
                (account_id, peer_id),
            ).fetchone()
        return row is not None

    def blacklist(self, account_id: str, peer_id: str, reason: str = "") -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gateway_blacklist "
                "(account_id, peer_id, reason, created_at) VALUES (?,?,?,?)",
                (account_id, peer_id, reason, _utcnow().isoformat()),
            )
            conn.commit()

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
    # 到点执行仍走 `CommandExecutor`，幂等（gateway_commands）和守门（频次/时间窗/黑名单）
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
        """
        values = [
            (
                account_id,
                peer_id,
                str(row.get("msg_id") or ""),
                str(row.get("at") or ""),
                str(row.get("at_text") or ""),
                1 if row.get("from_self") else 0,
                str(row.get("text") or ""),
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
                       (account_id, peer_id, msg_id, at, at_text, from_self, text)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
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
                """SELECT msg_id, at, at_text, from_self, text FROM gateway_messages
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
                "text": row[4],
            }
            for row in rows
            if row[0] not in skip
        ]
        return list(reversed(out[:limit]))

    def purge_messages_before(self, cutoff: datetime) -> int:
        """清掉太老的聊天记录，返回清了几条。

        这份记录是**给 AI 端当短期背景**用的，不是聊天记录归档——留着不清，一台跑几个月的
        机器会把候选人的对话原文攒成一个越来越大的本地文件（真实 PII）。
        """
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM gateway_messages WHERE at < ?", (cutoff.isoformat(),))
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
