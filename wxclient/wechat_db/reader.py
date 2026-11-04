"""微信 4.x 本地库的**只读**读取：换钥、开连接、按会话名切出新消息。

这一层只回答入站需要的三个问题，全部只读：

- 哪些会话有动静（``poll_changes`` 看 ``.db`` / ``-wal`` 的 mtime+size，不解密）；
- 某个会话名之后又来了哪几条（``new_messages``，按 ``local_id`` 游标，**稳定自增**，
  不像 UI 那条文本签名游标那样会漂）；
- 会话名 ↔ username 怎么对（``resolve_username`` / ``display_name``，走 contact.db 的
  remark / nick_name —— 网关一向按微信里显示的那个名字寻址）。

★ **只读、不写盘、不进日志**：密钥和聊天正文绝不落盘、绝不写进日志，日志只有计数。
  开连接默认用 `crypto.decrypt_database_bytes` 解到内存里的 SQLite 镜像；测试注入
  ``open_connection`` 直接开一个明文 fixture，不碰解密，也就能在 Linux 上跑。

真机上微信没登录 / 版本不对 / 本地库与当前登录账号对不上时 `attach` 抛错，上层据此回退到 UIA 轮询
（AGENTS.md §4.3）。
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import structlog

from . import crypto

_log = structlog.get_logger(__name__)

#: 一条消息 `local_type` 的低 32 位才是真类型；高位是 sub_type（真机上见过
#: 25769803825 = (6<<32)|49 这种复合值）。
_TYPE_MASK = 0xFFFFFFFF

#: `local_type` → 入站用的 message_type。只列我们会处理的；其余（系统提示 10000、
#: 语音通话 50、名片/链接等）要么过滤、要么交给上层按「非文本」兜底。
_TEXT_TYPE = 1
_TYPE_NAMES = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    49: "file",  # 49 其实是 appmsg（文件 / 链接 / 引用），具体靠 XML 再分，这里统一当附件交 UIA
}

#: 居中灰字系统提示：「你已添加了 XX，现在可以开始聊天了」等。入站不当消息处理。
_SYSTEM_TYPE = 10000

#: zstd 魔数（小端）。message_content / compress_content 要么是明文 str，要么是这个压的 bytes。
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _message_table(username: str) -> str:
    """会话的消息表名：``Msg_<md5(username)>``（真机实证，见 dbprobe 结果）。"""
    return "Msg_" + hashlib.md5(username.encode()).hexdigest()


def _decode_content(value: Any) -> str:
    """消息正文解码：zstd 压的解开，明文 str 原样，其它返回空串。

    真机上 1:1 文本多是明文 str；群聊常是 zstd（`compress_content`）。调用方优先给
    `compress_content`、再给 `message_content`（同 yingshen `decodeMessagePayload`）。
    """
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        if data[:4] == _ZSTD_MAGIC:
            try:
                import zstandard

                data = zstandard.ZstdDecompressor().decompress(data, max_output_size=1 << 20)
            except Exception:  # noqa: BLE001 —— 解不开就当读不到正文，交上层兜底，别炸整轮
                return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return ""
    return str(value or "")


def _strip_group_prefix(text: str) -> str:
    """群消息正文前面常带「wxid_xxx:\\n」发送者前缀（真机 str_with_sender_prefix）。切掉。"""
    head, sep, tail = text.partition(":\n")
    if sep and head and "\n" not in head and len(head) <= 64:
        return tail
    return text


class WeChatDbReader:
    """一个已登录账号的本地库只读句柄。线程不安全，单消费者使用。"""

    def __init__(
        self,
        storage_dir: str | Path,
        *,
        open_connection: Callable[[Path], sqlite3.Connection] | None = None,
    ) -> None:
        self._storage = Path(storage_dir)
        # 测试注入明文 opener；生产用解密 opener（attach 时装上，拿到密钥后才有）。
        self._open = open_connection
        self._keys: dict[bytes, bytes] = {}
        self._databases: dict[Path, bytes] = {}
        self._connections: dict[Path, sqlite3.Connection] = {}
        self._versions: dict[Path, tuple] = {}
        self._observed: dict[Path, tuple] = {}
        # 本账号「自己」的 username：目录名，以及去掉本地 _xxxx 后缀那个（同 yingshen）。
        account = self._storage.parent.name
        self._self_names = {account, re.sub(r"_[a-zA-Z0-9]{4}$", "", account)}

    # ── 生命周期 ─────────────────────────────────────────────

    def attach(self) -> None:
        """扫钥、认账号、建好 db→salt 映射。失败抛错（上层据此回退 UIA）。

        只在 Windows 上真的扫内存；测试走注入的 opener，不调用 attach。
        """
        databases, pages = {}, {}
        for path in self._storage.rglob("*.db"):
            with path.open("rb") as reader:
                page = reader.read(crypto.PAGE)
            if len(page) == crypto.PAGE and not page.startswith(b"SQLite format 3"):
                databases[path] = page[:16]
                pages[page[:16]] = page
        if not databases:
            raise ValueError("所选目录未找到加密的微信数据库")
        keys = crypto.scan_keys(pages)
        session = next((p for p in databases if p.name == "session.db"), None)
        if session is None or databases[session] not in keys:
            raise ValueError("当前登录微信与本地库不匹配，或此微信版本尚不支持密钥读取")
        self._keys = {salt: keys[salt] for salt in databases.values() if salt in keys}
        self._databases = databases
        self._open = self._decrypt_open
        _log.info("已连上本地微信库", 加密库=len(databases), 验证通过=len(self._keys))

    def close(self) -> None:
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()
        self._versions.clear()
        self._keys.clear()

    def _decrypt_open(self, path: Path) -> sqlite3.Connection:
        salt = self._databases[path]
        image = crypto.decrypt_database_bytes(path, self._keys[salt])
        image = bytearray(image)
        image[18:20] = bytes([1, 1])  # 已把提交的 WAL 并进镜像，内存里不再需要 sidecar
        connection = sqlite3.connect(":memory:")
        connection.deserialize(bytes(image))
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        return connection

    def _connect(self, path: Path) -> sqlite3.Connection:
        """开（或复用）一个连接；文件变了就重开，拿到最新快照。"""
        assert self._open is not None, "reader 未 attach，也没注入 open_connection"
        version = _file_version(path)
        if path not in self._connections or self._versions.get(path) != version:
            previous = self._connections.pop(path, None)
            if previous is not None:
                previous.close()
            self._connections[path] = self._open(path)
            self._versions[path] = version
        return self._connections[path]

    # ── 变化探测 ─────────────────────────────────────────────

    def _message_dbs(self) -> list[Path]:
        return sorted(p for p in self._storage.rglob("message_*.db") if re.fullmatch(r"message_\d+\.db", p.name))

    def poll_changes(self) -> bool:
        """消息库有没有动静（看 mtime+size，不解密、不开连接）。

        第一次调用建立基线、返回 False；之后任一消息库 / 其 ``-wal`` 变了就 True。
        """
        current = {path: _file_version(path) for path in self._message_dbs()}
        changed = bool(self._observed) and current != self._observed
        self._observed = current
        return changed

    # ── 名字映射 ─────────────────────────────────────────────

    def _contact_db(self) -> Path:
        path = self._storage / "contact" / "contact.db"
        if not path.exists():
            raise FileNotFoundError("contact.db 不存在")
        return path

    def _query(self, path: Path, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._connect(path).execute(sql, params))

    def display_name(self, username: str) -> str:
        """username → 微信里显示的名字（remark 优先，再 nick_name，兜底 username）。"""
        rows = self._query(
            self._contact_db(),
            "SELECT remark, nick_name FROM contact WHERE username=?",
            (username,),
        )
        if rows:
            return rows[0]["remark"] or rows[0]["nick_name"] or username
        return username

    def resolve_username(self, display_name: str) -> str | None:
        """微信里显示的名字 → username。对不上或撞名（歧义）都返回 None。

        网关的名单是显示名；歧义时宁可这一轮交回 UIA（它会再精确核对），也不猜。
        """
        rows = self._query(
            self._contact_db(),
            "SELECT username FROM contact WHERE remark=? OR (COALESCE(remark,'')='' AND nick_name=?)",
            (display_name, display_name),
        )
        usernames = {row["username"] for row in rows}
        return next(iter(usernames)) if len(usernames) == 1 else None

    def sessions(self) -> dict[str, tuple[int, str | None]]:
        """{username: (最后一条消息的 local_id, 最后一条的 message_type)}。

        只解一个很小的 session.db，不碰各个消息分片：``last_msg_local_id`` 跟会话的
        ``MAX(local_id)`` 一致，拿它和各会话游标比就知道谁来了新动静；``last_msg_type``
        给上层判「最后一条是不是文本」用（决定这个会话归本地库读还是归 UIA 读）。
        """
        path = self._storage / "session" / "session.db"
        if not path.exists():
            raise FileNotFoundError("session.db 不存在")
        # 真机列名是 last_msg_locald_id（微信自己拼错的），容错两种。
        columns = {row["name"] for row in self._query(path, "PRAGMA table_info(SessionTable)")}
        local_id_col = "last_msg_locald_id" if "last_msg_locald_id" in columns else "last_msg_local_id"
        if local_id_col not in columns:
            return {}
        type_sql = ", last_msg_type" if "last_msg_type" in columns else ""
        rows = self._query(
            path,
            f'SELECT username, "{local_id_col}" AS local_id{type_sql} '
            f"FROM SessionTable WHERE is_hidden=0",
        )
        out: dict[str, tuple[int, str | None]] = {}
        for row in rows:
            raw_type = int((row["last_msg_type"] if type_sql else 0) or 0) & _TYPE_MASK
            out[row["username"]] = (int(row["local_id"] or 0), _TYPE_NAMES.get(raw_type))
        return out

    # ── 取新消息 ─────────────────────────────────────────────

    def _tables_for(self, username: str) -> list[tuple[Path, str]]:
        """这个会话的消息表散落在哪些 message_*.db 里。"""
        table = _message_table(username)
        found = []
        for path in self._message_dbs():
            if self._query(path, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
                found.append((path, table))
        return found

    def session_cursor(self, username: str) -> int:
        """会话当前最大 ``local_id``（跨分片取最大）。接入名单时拿它当起点，不回溯历史。"""
        high = 0
        for path, table in self._tables_for(username):
            row = self._query(path, f'SELECT COALESCE(MAX(local_id), 0) AS id FROM "{table}"')
            high = max(high, int(row[0]["id"]))
        return high

    def new_messages(self, username: str, after_local_id: int) -> list[dict[str, Any]]:
        """``local_id`` 在 ``after_local_id`` 之后的消息，按 (create_time, sort_seq, local_id) 升序。

        返回的 raw dict 跟 UIA 那条链路同形（text / is_self / message_type / local_id /
        timestamp_text …），好让 `to_inbound` / 去重 / 落盘一视同仁。非文本只带类型，
        正文交由上层按 message_type 兜底（图片写占位句、语音转文字走 UIA）。
        """
        rows: list[dict[str, Any]] = []
        for path, table in self._tables_for(username):
            name_by_id = {
                r["rowid"]: r["user_name"]
                for r in self._query(path, "SELECT rowid, user_name FROM Name2Id")
            }
            records = self._query(
                path,
                f'SELECT local_id, server_id, local_type, sort_seq, real_sender_id, '
                f'create_time, message_content, compress_content '
                f'FROM "{table}" WHERE local_id > ? ORDER BY local_id ASC',
                (after_local_id,),
            )
            for record in records:
                raw_type = int(record["local_type"] or 0) & _TYPE_MASK
                if raw_type == _SYSTEM_TYPE:
                    continue
                sender = name_by_id.get(record["real_sender_id"])
                is_self = sender in self._self_names if sender else False
                message_type = _TYPE_NAMES.get(raw_type)
                if message_type is None:
                    continue  # 通话 / 名片 / 撤回等：入站不处理
                text = ""
                if raw_type == _TEXT_TYPE:
                    text = _decode_content(record["compress_content"]) or _decode_content(
                        record["message_content"]
                    )
                    text = _strip_group_prefix(text)
                # 文本解不出正文（如 zstd 解压失败）也交 UIA 读，别把候选人的话悄悄丢掉。
                needs_ui_read = raw_type != _TEXT_TYPE or not text.strip()
                rows.append(
                    {
                        "local_id": int(record["local_id"]),
                        "create_time": int(record["create_time"] or 0),
                        "sort_seq": int(record["sort_seq"] or 0),
                        "text": text,
                        "is_self": is_self,
                        "sender_username": sender,
                        "message_type": message_type,
                        "needs_ui_read": needs_ui_read,
                    }
                )
        rows.sort(key=lambda row: (row["create_time"], row["sort_seq"], row["local_id"]))
        return rows


def locate_storages() -> list[Path]:
    """列出本机各账号的 ``db_storage`` 目录（Windows，微信 4.x），最近活跃的排前面。

    走微信自己写的配置：``%APPDATA%\\Tencent\\xwechat\\config\\*.ini`` 里存着数据根，
    根下 ``xwechat_files\\<account>\\db_storage``。换过号的机器上旧账号目录还留着
    （真机 2026-11-04 就是一旧一新两个），所以不能要求「正好一个」：按 session.db
    （含 -wal）最近写入时间排序，由 `build_reader` 挨个 attach，密钥对得上的才是当前登录的。
    """
    if os.name != "nt":
        raise ValueError("本地库读取只在 Windows 上可用")
    configs = glob.glob(os.path.expandvars(r"%APPDATA%\Tencent\xwechat\config\*.ini"))
    if not configs:
        raise ValueError("未找到微信配置，请先登录电脑版微信")
    with open(configs[0], encoding="utf-8", errors="ignore") as handle:
        root = Path(handle.read().strip())
    base = root / "xwechat_files"
    storages = [p / "db_storage" for p in base.iterdir() if (p / "db_storage").is_dir()] if base.is_dir() else []
    if not storages:
        raise ValueError("未找到已登录账号的本地库")
    return sorted(storages, key=_last_write, reverse=True)


def build_reader() -> WeChatDbReader | None:
    """定位 + 扫钥 + 认账号，成了返回可用的 reader，任何一步失败返回 None（上层回退 UIA）。

    多个账号目录时从最近活跃的试起；attach 会核对 session.db 的密钥是否出自正在运行的
    微信，旧账号对不上就抛错、换下一个，全都对不上才回退。
    """
    try:
        storages = locate_storages()
    except Exception as exc:  # noqa: BLE001 —— 失败就回退 UIA 轮询，不让它拦住启动
        _log.info("本地库入站不可用，回退到 UIA 轮询", 原因=str(exc)[:160])
        return None
    reason = ""
    for storage in storages:
        try:
            reader = WeChatDbReader(storage)
            reader.attach()
            return reader
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)[:160]
    _log.info("本地库入站不可用，回退到 UIA 轮询", 原因=reason, 账号目录=len(storages))
    return None


def _last_write(storage: Path) -> int:
    """账号目录的活跃度：session.db 及其 -wal 里较新的 mtime；读不到算 0。"""
    session = storage / "session" / "session.db"
    return max(_file_version(session)[0::2])


def _file_version(path: Path) -> tuple:
    """(db 的 mtime_ns, size, wal 的 mtime_ns, wal size)，探测变化用；文件不在算 0。"""
    out: list[int] = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stat = candidate.stat()
            out.extend((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            out.extend((0, 0))
    return tuple(out)
