"""网关本地状态：会话映射、消息去重游标、黑名单、发送计数。

**必须持久化**：Windows 机重启、进程崩溃之后，如果 (专员微信号 × 对方 wxid) → session_id
这张表没了，所有候选人的对话上下文就断了——AI 会把聊了三天的人当陌生人重新打招呼。

与任务队列分库（``gateway.db``），避免和常驻 worker 抢锁。
"""

from __future__ import annotations

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
CREATE TABLE IF NOT EXISTS gateway_send_log (
  account_id TEXT NOT NULL,
  peer_id    TEXT NOT NULL,
  sent_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_send_log_account_time
  ON gateway_send_log (account_id, sent_at);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GatewayStore:
    """网关本地状态存储。所有方法同步——SQLite 本来就快，别为它引入异步复杂度。"""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
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

    # ────────────────────────── 发送计数（频次守门）──────────────────────────

    def record_send(self, account_id: str, peer_id: str, at: datetime | None = None) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO gateway_send_log (account_id, peer_id, sent_at) VALUES (?,?,?)",
                (account_id, peer_id, (at or _utcnow()).isoformat()),
            )
            conn.commit()

    def sends_since(self, account_id: str, since: datetime) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM gateway_send_log WHERE account_id=? AND sent_at>=?",
                (account_id, since.isoformat()),
            ).fetchone()
        return int(row[0]) if row else 0

    def sends_in_last_hour(self, account_id: str, now: datetime | None = None) -> int:
        return self.sends_since(account_id, (now or _utcnow()) - timedelta(hours=1))
