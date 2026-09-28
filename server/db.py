import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_msg_id TEXT UNIQUE,
    sender TEXT NOT NULL,
    text TEXT NOT NULL,
    received_at TEXT,
    device_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sms_id ON sms(id);

CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_msg_id TEXT UNIQUE,
    direction TEXT NOT NULL,
    number TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    answered_at TEXT,
    ended_at TEXT,
    duration INTEGER,
    recording_file TEXT,
    device_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_msg_id TEXT UNIQUE,
    recipient TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    device_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT UNIQUE NOT NULL,
    device_key TEXT,
    owner_id INTEGER,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);
"""

_USER_FIELDS = ("password_hash", "role", "disabled")
_DEVICE_FIELDS = ("name", "disabled", "last_seen_at", "owner_id")

_CALL_FIELDS = ("direction", "number", "status", "started_at",
                "answered_at", "ended_at", "duration", "recording_file", "device_id")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 老库迁移：缺列就补（SQLite 支持 ADD COLUMN）
            self._migrate()

    # 老库需要补的列：(表, 列, DDL)
    _MIGRATIONS = (
        ("devices", "device_key", "ALTER TABLE devices ADD COLUMN device_key TEXT"),
        ("devices", "owner_id", "ALTER TABLE devices ADD COLUMN owner_id INTEGER"),
        ("sms", "device_id", "ALTER TABLE sms ADD COLUMN device_id INTEGER"),
        ("calls", "device_id", "ALTER TABLE calls ADD COLUMN device_id INTEGER"),
        ("sms_outbox", "device_id", "ALTER TABLE sms_outbox ADD COLUMN device_id INTEGER"),
    )

    def _migrate(self):
        for table, col, ddl in self._MIGRATIONS:
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._conn.execute(ddl)
                self._conn.commit()
        # 索引放在迁移后建：老库的 sms 表可能还没 device_id 列
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_sms_device ON sms(device_id)")
        self._conn.commit()

    def insert_sms(
        self,
        sender: str,
        text: str,
        received_at: Optional[str] = None,
        client_msg_id: Optional[str] = None,
        device_id: Optional[int] = None,
    ) -> tuple[dict, bool]:
        """Insert one SMS. Returns (row, created). If client_msg_id already
        exists (agent retry), returns the existing row with created=False."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            if client_msg_id:
                row = self._conn.execute(
                    "SELECT * FROM sms WHERE client_msg_id = ?", (client_msg_id,)
                ).fetchone()
                if row:
                    return dict(row), False
            cur = self._conn.execute(
                "INSERT INTO sms (client_msg_id, sender, text, received_at, device_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (client_msg_id, sender, text, received_at, device_id, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sms WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row), True

    def list_sms(
        self,
        limit: int = 50,
        before_id: Optional[int] = None,
        device_ids: Optional[set[int]] = None,
    ) -> list[dict]:
        """device_ids=None 不过滤（admin）；传集合则只看这些设备的；
        空集合 → 什么也看不到。"""
        limit = max(1, min(limit, 500))
        if device_ids is not None and not device_ids:
            return []
        cond, params = "", []
        if device_ids is not None:
            cond = "device_id IN (%s)" % ",".join("?" * len(device_ids))
            params.extend(device_ids)
        if before_id is not None:
            cond += " AND " if cond else ""
            cond += "id < ?"
            params.append(before_id)
        sql = "SELECT * FROM sms" + (" WHERE " + cond if cond else "") \
              + " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_sms(self, sms_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sms WHERE id = ?", (sms_id,)
            ).fetchone()
            return dict(row) if row else None

    def delete_sms(self, sms_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sms WHERE id = ?", (sms_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- calls ----

    def upsert_call(self, client_msg_id: str, **fields) -> dict:
        """Insert a call or update it by client_msg_id (agent sends the same id
        across status transitions). Returns the current row."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sets = {k: v for k, v in fields.items() if k in _CALL_FIELDS and v is not None}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calls WHERE client_msg_id = ?", (client_msg_id,)
            ).fetchone()
            if row:
                if sets:
                    cols = ", ".join(f"{k} = ?" for k in sets)
                    self._conn.execute(
                        f"UPDATE calls SET {cols} WHERE client_msg_id = ?",
                        (*sets.values(), client_msg_id),
                    )
                    self._conn.commit()
            else:
                cols = ["client_msg_id", *sets.keys(), "created_at"]
                self._conn.execute(
                    f"INSERT INTO calls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    (client_msg_id, *sets.values(), now),
                )
                self._conn.commit()
            return dict(self._conn.execute(
                "SELECT * FROM calls WHERE client_msg_id = ?", (client_msg_id,)
            ).fetchone())

    def list_calls(self, limit: int = 50, device_ids: Optional[set[int]] = None) -> list[dict]:
        limit = max(1, min(limit, 500))
        if device_ids is not None and not device_ids:
            return []
        sql = "SELECT * FROM calls"
        params: list = []
        if device_ids is not None:
            sql += " WHERE device_id IN (%s)" % ",".join("?" * len(device_ids))
            params.extend(device_ids)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_call(self, call_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calls WHERE id = ?", (call_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_recording(self, call_id: int, filename: str) -> Optional[dict]:
        with self._lock:
            self._conn.execute(
                "UPDATE calls SET recording_file = ? WHERE id = ?", (filename, call_id)
            )
            self._conn.commit()
        return self.get_call(call_id)

    # ---- sms outbox (sent messages) ----

    def insert_outbox_sms(self, client_msg_id: str, recipient: str, text: str,
                          device_id: Optional[int] = None) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sms_outbox (client_msg_id, recipient, text, status, device_id, created_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                (client_msg_id, recipient, text, device_id, now),
            )
            self._conn.commit()
            return dict(self._conn.execute(
                "SELECT * FROM sms_outbox WHERE client_msg_id = ?", (client_msg_id,)
            ).fetchone())

    def update_outbox_status(self, client_msg_id: str, status: str, error: Optional[str] = None) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "UPDATE sms_outbox SET status = ?, error = ?, updated_at = ?"
                " WHERE client_msg_id = ?",
                (status, error, now, client_msg_id),
            )
            self._conn.commit()
            return dict(self._conn.execute(
                "SELECT * FROM sms_outbox WHERE client_msg_id = ?", (client_msg_id,)
            ).fetchone())

    def list_outbox_sms(self, limit: int = 50, device_ids: Optional[set[int]] = None) -> list[dict]:
        limit = max(1, min(limit, 500))
        if device_ids is not None and not device_ids:
            return []
        sql = "SELECT * FROM sms_outbox"
        params: list = []
        if device_ids is not None:
            sql += " WHERE device_id IN (%s)" % ",".join("?" * len(device_ids))
            params.extend(device_ids)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ---- users ----

    def count_users(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def count_admins(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
            ).fetchone()[0]

    def create_user(self, username: str, password_hash: str, role: str = "user") -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at)"
                " VALUES (?, ?, ?, ?)",
                (username, password_hash, role, now),
            )
            self._conn.commit()
            return dict(self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (cur.lastrowid,)
            ).fetchone())

    def get_user(self, user_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            return dict(row) if row else None

    def list_users(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, username, role, disabled, created_at FROM users ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_user(self, user_id: int, **fields) -> Optional[dict]:
        sets = {k: v for k, v in fields.items() if k in _USER_FIELDS and v is not None}
        with self._lock:
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                self._conn.execute(
                    f"UPDATE users SET {cols} WHERE id = ?", (*sets.values(), user_id)
                )
                self._conn.commit()
            row = self._conn.execute(
                "SELECT id, username, role, disabled, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_user(self, user_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---- devices（采集端设备码认证） ----

    def create_device(self, name: str, key_hash: str, device_key: str,
                      owner_id: Optional[int] = None) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO devices (name, key_hash, device_key, owner_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (name, key_hash, device_key, owner_id, now),
            )
            self._conn.commit()
            return dict(self._conn.execute(
                "SELECT id, name, device_key, owner_id, disabled, created_at, last_seen_at"
                " FROM devices WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone())

    def get_device(self, device_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, key_hash, owner_id, disabled, created_at, last_seen_at"
                " FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_device_by_hash(self, key_hash: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE key_hash = ?", (key_hash,)
            ).fetchone()
            return dict(row) if row else None

    def list_devices(self, owner_id: Optional[int] = None) -> list[dict]:
        with self._lock:
            if owner_id is None:
                rows = self._conn.execute(
                    "SELECT id, name, device_key, owner_id, disabled, created_at, last_seen_at"
                    " FROM devices ORDER BY id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, name, device_key, owner_id, disabled, created_at, last_seen_at"
                    " FROM devices WHERE owner_id = ? ORDER BY id",
                    (owner_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def owned_device_ids(self, user_id: int) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM devices WHERE owner_id = ?", (user_id,)
            ).fetchall()
            return {r[0] for r in rows}

    def clear_device_key(self, device_id: int):
        """设备首次认证成功后调用：清除明文设备码，此后后台不再可见。"""
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET device_key = NULL WHERE id = ?", (device_id,)
            )
            self._conn.commit()

    def regenerate_device_key(self, device_id: int, key_hash: str, device_key: str) -> Optional[dict]:
        """重置设备码（旧码立即失效）。返回更新后的行（含新明文码）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET key_hash = ?, device_key = ? WHERE id = ?",
                (key_hash, device_key, device_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id, name, device_key, owner_id, disabled, created_at, last_seen_at FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_device(self, device_id: int, **fields) -> Optional[dict]:
        # owner_id 允许显式置 None（解绑）；其余字段 None 表示不更新
        sets = {k: v for k, v in fields.items()
                if k in _DEVICE_FIELDS and (v is not None or k == "owner_id")}
        with self._lock:
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                self._conn.execute(
                    f"UPDATE devices SET {cols} WHERE id = ?", (*sets.values(), device_id)
                )
                self._conn.commit()
            row = self._conn.execute(
                "SELECT id, name, owner_id, disabled, created_at, last_seen_at FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_device(self, device_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            self._conn.commit()
            return cur.rowcount > 0


def to_event(row: dict) -> str:
    return json.dumps({"type": "sms", "data": row}, ensure_ascii=False)
