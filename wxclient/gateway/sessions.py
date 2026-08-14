"""网关本地状态：会话映射、消息去重游标、黑名单、发送计数。

**必须持久化**：Windows 机重启、进程崩溃之后，如果 (专员微信号 × 对方 wxid) → session_id
这张表没了，所有候选人的对话上下文就断了——AI 会把聊了三天的人当陌生人重新打招呼。

与任务队列分库（``gateway.db``），避免和常驻 worker 抢锁。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS gateway_send_log (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  sent_at    TEXT NOT NULL,
  action     TEXT NOT NULL DEFAULT 'message'
);
CREATE INDEX IF NOT EXISTS idx_send_log_account_time
  ON gateway_send_log (account_id, sent_at);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """幂等加列。SQLite 的 ADD COLUMN 不动既有数据，只给老行填默认值。"""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

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
                f"SELECT msg_id FROM gateway_seen_messages "  # noqa: S608 —— 占位符个数由 len(ids) 决定，值仍走参数绑定
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
