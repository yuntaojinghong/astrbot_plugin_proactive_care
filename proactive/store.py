"""SQLite 持久化。

为什么不用 JSON 文件：消息上下文、长期记忆、发送历史都是**持续增长**的数据，
JSON 方案每写一条都要整体重写一遍，既慢又容易在异常退出时写坏整个文件。
SQLite 提供原子写入、索引与「只更新某一列」的能力，而且 ``sqlite3`` 是标准库，
不引入任何第三方依赖。

并发模型：持有一个 ``check_same_thread=False`` 的连接，所有操作都在
``asyncio.to_thread`` 里串行执行，并用 ``RLock`` 保护。SQLite 本身很快，
插件场景的写入频率也低，这个模型既简单又正确。
"""

from __future__ import annotations

import asyncio
import functools
import os
import sqlite3
import threading
from typing import Any, Callable

from .textutil import day_start_ts, now_ts

# 允许通过 update_session 修改的列，避免拼 SQL 时被注入
SESSION_FIELDS = {
    "scope",
    "target_id",
    "enabled",
    "paused",
    "paused_reason",
    "last_human_ts",
    "last_bot_ts",
    "last_proactive_ts",
    "awaiting_reply",
    "pending_question",
    "pending_question_ts",
    "unanswered_streak",
    "total_sent",
    "override_json",
    "msg_counter",
    "last_extract_ts",
}

# 允许自增的列——只有数值列能参与 `col = col + ?`，其余一律拒绝
SESSION_NUMERIC_FIELDS = {
    "enabled",
    "paused",
    "last_human_ts",
    "last_bot_ts",
    "last_proactive_ts",
    "awaiting_reply",
    "pending_question",
    "pending_question_ts",
    "unanswered_streak",
    "total_sent",
    "msg_counter",
    "last_extract_ts",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    umo                 TEXT PRIMARY KEY,
    scope               TEXT    NOT NULL DEFAULT 'group',
    target_id           TEXT    NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    paused              INTEGER NOT NULL DEFAULT 0,
    paused_reason       TEXT    NOT NULL DEFAULT '',
    last_human_ts       REAL    NOT NULL DEFAULT 0,
    last_bot_ts         REAL    NOT NULL DEFAULT 0,
    last_proactive_ts   REAL    NOT NULL DEFAULT 0,
    awaiting_reply      INTEGER NOT NULL DEFAULT 0,
    pending_question    INTEGER NOT NULL DEFAULT 0,
    pending_question_ts REAL    NOT NULL DEFAULT 0,
    unanswered_streak   INTEGER NOT NULL DEFAULT 0,
    total_sent          INTEGER NOT NULL DEFAULT 0,
    override_json       TEXT    NOT NULL DEFAULT '{}',
    msg_counter         INTEGER NOT NULL DEFAULT 0,
    last_extract_ts     REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    umo         TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',
    sender_id   TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_umo_ts ON messages (umo, ts DESC);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    umo         TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'fact',
    content     TEXT NOT NULL,
    norm        TEXT NOT NULL,
    importance  REAL NOT NULL DEFAULT 0.5,
    created_ts  REAL NOT NULL DEFAULT 0,
    last_hit_ts REAL NOT NULL DEFAULT 0,
    hits        INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_unique ON memories (umo, norm);

CREATE TABLE IF NOT EXISTS history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    umo      TEXT NOT NULL,
    trigger  TEXT NOT NULL DEFAULT '',
    reason   TEXT NOT NULL DEFAULT '',
    content  TEXT NOT NULL,
    ts       REAL NOT NULL,
    sent     INTEGER NOT NULL DEFAULT 1,
    answered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_umo_ts ON history (umo, ts DESC);

CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    umo           TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    at_time       TEXT NOT NULL DEFAULT '08:00',
    weekdays      TEXT NOT NULL DEFAULT '1,2,3,4,5,6,7',
    brief         TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_run_date TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_schedules_umo ON schedules (umo);
"""


def _async(fn: Callable) -> Callable:
    """把同步的存储方法包装成异步方法，丢进线程池执行。"""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    return wrapper


class Store:
    """所有持久化读写都从这里走。"""

    #: 等锁超时（秒）。与 AstrBot 核心保持一致（它设的是 ``busy_timeout=30000``）。
    #:
    #: 为什么要显式设：``sqlite3.connect`` 默认只等 **5 秒**。本插件的库和
    #: AstrBot 核心的库在**同一个进程、同一块磁盘**上，写锁是全局互斥的；
    #: 只要别的写者持锁超过 5 秒，这里就直接抛
    #: ``sqlite3.OperationalError: database is locked``。
    #: 实测：另一写者持锁 7 秒时，默认 5 秒超时必失败；设为 30 秒则正常等待并通过。
    LOCK_TIMEOUT_SECONDS = 30.0

    def __init__(self, data_dir: str, filename: str = "proactive_care.db"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, filename)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=self.LOCK_TIMEOUT_SECONDS
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # 双保险：connect(timeout=) 与 busy_timeout 都设上。
            # WAL 下这能让写入在别人持锁时排队等待，而不是直接失败。
            self._conn.execute(
                f"PRAGMA busy_timeout={int(self.LOCK_TIMEOUT_SECONDS * 1000)}"
            )
            self._conn.commit()

    # ==================================================================
    #  内部工具
    # ==================================================================
    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    def _write(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor.rowcount

    def _write_many(self, sql: str, seq: list[tuple]) -> int:
        if not seq:
            return 0
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()
            return len(seq)

    # ==================================================================
    #  会话
    # ==================================================================
    @_async
    def ensure_session(self, umo: str, scope: str, target_id: str, enabled: bool = True) -> dict:
        """会话不存在则创建，已存在则原样返回。"""
        existing = self._one("SELECT * FROM sessions WHERE umo = ?", (umo,))
        if existing:
            return existing
        self._write(
            "INSERT OR IGNORE INTO sessions (umo, scope, target_id, enabled) VALUES (?, ?, ?, ?)",
            (umo, scope, str(target_id), 1 if enabled else 0),
        )
        return self._one("SELECT * FROM sessions WHERE umo = ?", (umo,)) or {}

    @_async
    def get_session(self, umo: str) -> dict | None:
        return self._one("SELECT * FROM sessions WHERE umo = ?", (umo,))

    @_async
    def list_sessions(self, only_enabled: bool = False) -> list[dict]:
        sql = "SELECT * FROM sessions"
        if only_enabled:
            sql += " WHERE enabled = 1 AND paused = 0"
        sql += " ORDER BY last_human_ts DESC"
        return self._query(sql)

    @_async
    def update_session(self, umo: str, **fields: Any) -> int:
        usable = {key: value for key, value in fields.items() if key in SESSION_FIELDS}
        if not usable:
            return 0
        assignments = ", ".join(f"{key} = ?" for key in usable)
        params = tuple(int(value) if isinstance(value, bool) else value for value in usable.values())
        return self._write(f"UPDATE sessions SET {assignments} WHERE umo = ?", (*params, umo))

    @_async
    def bump_session(self, umo: str, field: str, delta: int = 1) -> int:
        """对某个数值列做自增/自减，避开「读-改-写」的竞态。"""
        if field not in SESSION_NUMERIC_FIELDS:
            raise ValueError(f"不允许自增的字段: {field}")
        return self._write(f"UPDATE sessions SET {field} = {field} + ? WHERE umo = ?", (delta, umo))

    @_async
    def delete_session(self, umo: str, purge: bool = True) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE umo = ?", (umo,))
            self._conn.execute("DELETE FROM schedules WHERE umo = ?", (umo,))
            if purge:
                self._conn.execute("DELETE FROM messages WHERE umo = ?", (umo,))
                self._conn.execute("DELETE FROM memories WHERE umo = ?", (umo,))
                self._conn.execute("DELETE FROM history WHERE umo = ?", (umo,))
            self._conn.commit()

    # ==================================================================
    #  消息上下文
    # ==================================================================
    @_async
    def add_message(
        self,
        umo: str,
        role: str,
        content: str,
        sender_id: str = "",
        sender_name: str = "",
        ts: float | None = None,
    ) -> None:
        self._write(
            "INSERT INTO messages (umo, role, sender_id, sender_name, content, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (umo, role, str(sender_id), str(sender_name), content, float(ts if ts is not None else now_ts())),
        )

    @_async
    def recent_messages(self, umo: str, limit: int = 20, since_ts: float = 0.0) -> list[dict]:
        rows = self._query(
            "SELECT * FROM messages WHERE umo = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (umo, float(since_ts), int(limit)),
        )
        rows.reverse()  # 调用方要的是时间正序
        return rows

    @_async
    def count_messages(self, umo: str, since_ts: float = 0.0) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM messages WHERE umo = ? AND ts >= ?", (umo, float(since_ts)))
        return int(row["n"]) if row else 0

    @_async
    def prune_messages(self, umo: str, keep: int = 200) -> int:
        """只保留最近 ``keep`` 条，防止数据库无限增长。"""
        keep = max(10, int(keep))
        return self._write(
            "DELETE FROM messages WHERE umo = ? AND id NOT IN "
            "(SELECT id FROM messages WHERE umo = ? ORDER BY ts DESC LIMIT ?)",
            (umo, umo, keep),
        )

    @_async
    def clear_messages(self, umo: str) -> None:
        self._write("DELETE FROM messages WHERE umo = ?", (umo,))

    # ==================================================================
    #  长期记忆
    # ==================================================================
    @_async
    def upsert_memory(
        self,
        umo: str,
        content: str,
        norm: str,
        kind: str = "fact",
        importance: float = 0.5,
        ts: float | None = None,
    ) -> int:
        """按 (umo, norm) 去重。已存在则提升重要度、刷新时间，并累加命中次数。"""
        moment = float(ts if ts is not None else now_ts())
        self._write(
            "INSERT INTO memories (umo, kind, content, norm, importance, created_ts, last_hit_ts, hits) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0) "
            "ON CONFLICT(umo, norm) DO UPDATE SET "
            "  importance = MAX(importance, excluded.importance), "
            "  content = excluded.content, "
            "  kind = excluded.kind",
            (umo, kind, content, norm, float(importance), moment),
        )
        row = self._one("SELECT id FROM memories WHERE umo = ? AND norm = ?", (umo, norm))
        return int(row["id"]) if row else 0

    @_async
    def list_memories(self, umo: str | None = None, limit: int = 500, query: str = "") -> list[dict]:
        sql = "SELECT * FROM memories WHERE 1 = 1"
        params: list[Any] = []
        if umo:
            sql += " AND umo = ?"
            params.append(umo)
        if query:
            sql += " AND content LIKE ?"
            params.append(f"%{query}%")
        sql += " ORDER BY importance DESC, created_ts DESC LIMIT ?"
        params.append(int(limit))
        return self._query(sql, tuple(params))

    @_async
    def update_memory(self, memory_id: int, content: str | None = None, importance: float | None = None) -> int:
        sets, params = [], []
        if content is not None:
            from .textutil import normalize

            sets.append("content = ?")
            params.append(content)
            sets.append("norm = ?")
            params.append(normalize(content))
        if importance is not None:
            sets.append("importance = ?")
            params.append(float(importance))
        if not sets:
            return 0
        params.append(int(memory_id))
        return self._write(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", tuple(params))

    @_async
    def delete_memory(self, memory_id: int) -> int:
        return self._write("DELETE FROM memories WHERE id = ?", (int(memory_id),))

    @_async
    def clear_memories(self, umo: str) -> int:
        return self._write("DELETE FROM memories WHERE umo = ?", (umo,))

    @_async
    def touch_memories(self, ids: list[int], ts: float | None = None) -> None:
        """记录一次「被检索命中」，用于后续排序。"""
        if not ids:
            return
        moment = float(ts if ts is not None else now_ts())
        self._write_many(
            "UPDATE memories SET hits = hits + 1, last_hit_ts = ? WHERE id = ?",
            [(moment, int(mid)) for mid in ids],
        )

    @_async
    def prune_memories(self, umo: str, keep: int = 300) -> int:
        return self._write(
            "DELETE FROM memories WHERE umo = ? AND id NOT IN "
            "(SELECT id FROM memories WHERE umo = ? ORDER BY importance DESC, created_ts DESC LIMIT ?)",
            (umo, umo, max(20, int(keep))),
        )

    # ==================================================================
    #  发送历史
    # ==================================================================
    @_async
    def add_history(
        self,
        umo: str,
        trigger: str,
        reason: str,
        content: str,
        sent: bool = True,
        ts: float | None = None,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO history (umo, trigger, reason, content, ts, sent) VALUES (?, ?, ?, ?, ?, ?)",
                (umo, trigger, reason, content, float(ts if ts is not None else now_ts()), 1 if sent else 0),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    @_async
    def list_history(self, umo: str | None = None, limit: int = 100) -> list[dict]:
        if umo:
            return self._query(
                "SELECT * FROM history WHERE umo = ? ORDER BY ts DESC LIMIT ?", (umo, int(limit))
            )
        return self._query("SELECT * FROM history ORDER BY ts DESC LIMIT ?", (int(limit),))

    @_async
    def recent_sent_contents(self, umo: str, limit: int = 8) -> list[str]:
        rows = self._query(
            "SELECT content FROM history WHERE umo = ? AND sent = 1 ORDER BY ts DESC LIMIT ?",
            (umo, int(limit)),
        )
        return [row["content"] for row in rows]

    @_async
    def mark_latest_replied(self, umo: str) -> int:
        """把最近一条还没被回应的主动消息标记为「已回应」。"""
        row = self._one(
            "SELECT id FROM history WHERE umo = ? AND answered = 0 ORDER BY ts DESC LIMIT 1", (umo,)
        )
        if not row:
            return 0
        return self._write("UPDATE history SET answered = 1 WHERE id = ?", (int(row["id"]),))

    @_async
    def count_sent_today(self, umo: str, ts: float | None = None) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM history WHERE umo = ? AND sent = 1 AND ts >= ?",
            (umo, day_start_ts(ts)),
        )
        return int(row["n"]) if row else 0

    @_async
    def count_trigger_today(self, umo: str, trigger: str, ts: float | None = None) -> int:
        """统计某个触发方式今天已经用过几次（随机关怀的排期靠它推进）。"""
        row = self._one(
            "SELECT COUNT(*) AS n FROM history WHERE umo = ? AND trigger = ? AND sent = 1 AND ts >= ?",
            (umo, trigger, day_start_ts(ts)),
        )
        return int(row["n"]) if row else 0

    @_async
    def clear_history(self, umo: str | None = None) -> int:
        if umo:
            return self._write("DELETE FROM history WHERE umo = ?", (umo,))
        return self._write("DELETE FROM history")

    # ==================================================================
    #  定时规则
    # ==================================================================
    @_async
    def list_schedules(self, umo: str | None = None) -> list[dict]:
        if umo:
            return self._query("SELECT * FROM schedules WHERE umo = ? ORDER BY at_time", (umo,))
        return self._query("SELECT * FROM schedules ORDER BY at_time")

    @_async
    def add_schedule(
        self,
        umo: str,
        at_time: str,
        name: str = "",
        weekdays: str = "1,2,3,4,5,6,7",
        brief: str = "",
        enabled: bool = True,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO schedules (umo, name, at_time, weekdays, brief, enabled) VALUES (?, ?, ?, ?, ?, ?)",
                (umo, name, at_time, weekdays, brief, 1 if enabled else 0),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    @_async
    def update_schedule(self, schedule_id: int, **fields: Any) -> int:
        allowed = {"name", "at_time", "weekdays", "brief", "enabled"}
        usable = {key: value for key, value in fields.items() if key in allowed}
        if not usable:
            return 0
        assignments = ", ".join(f"{key} = ?" for key in usable)
        params = tuple(int(v) if isinstance(v, bool) else v for v in usable.values())
        return self._write(f"UPDATE schedules SET {assignments} WHERE id = ?", (*params, int(schedule_id)))

    @_async
    def delete_schedule(self, schedule_id: int) -> int:
        return self._write("DELETE FROM schedules WHERE id = ?", (int(schedule_id),))

    @_async
    def mark_schedule_run(self, schedule_id: int, date_key: str) -> int:
        return self._write(
            "UPDATE schedules SET last_run_date = ? WHERE id = ?", (date_key, int(schedule_id))
        )

    # ==================================================================
    #  汇总
    # ==================================================================
    @_async
    def overview(self) -> dict:
        sessions = self._one("SELECT COUNT(*) AS n FROM sessions")
        active = self._one("SELECT COUNT(*) AS n FROM sessions WHERE enabled = 1 AND paused = 0")
        paused = self._one("SELECT COUNT(*) AS n FROM sessions WHERE paused = 1")
        sent_today = self._one("SELECT COUNT(*) AS n FROM history WHERE sent = 1 AND ts >= ?", (day_start_ts(),))
        sent_total = self._one("SELECT COUNT(*) AS n FROM history WHERE sent = 1")
        previews = self._one("SELECT COUNT(*) AS n FROM history WHERE sent = 0")
        answered = self._one("SELECT COUNT(*) AS n FROM history WHERE sent = 1 AND answered = 1")
        memories = self._one("SELECT COUNT(*) AS n FROM memories")
        msgs = self._one("SELECT COUNT(*) AS n FROM messages")
        answered_denominator = int(sent_total["n"]) if sent_total else 0
        return {
            "sessions": int(sessions["n"]) if sessions else 0,
            "active": int(active["n"]) if active else 0,
            "paused": int(paused["n"]) if paused else 0,
            "sent_today": int(sent_today["n"]) if sent_today else 0,
            "sent_total": answered_denominator,
            "previews": int(previews["n"]) if previews else 0,
            "answered": int(answered["n"]) if answered else 0,
            "answer_rate": round(int(answered["n"]) / answered_denominator, 3) if answered_denominator else 0.0,
            "memories": int(memories["n"]) if memories else 0,
            "messages": int(msgs["n"]) if msgs else 0,
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
