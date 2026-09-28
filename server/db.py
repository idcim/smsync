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
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sms_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_msg_id TEXT UNIQUE,
    recipient TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
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
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);
"""

_USER_FIELDS = ("password_hash", "role", "disabled")
_DEVICE_FIELDS = ("name", "disabled", "last_seen_at")

_CALL_FIELDS = ("direction", "number", "status", "started_at",
                "answered_at", "ended_at", "duration", "recording_file")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 老库迁移：devices 表补 device_key 列（未使用的设备码临时明文存储，
            # 设备首次认证成功后即清除）
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(devices)")}
            if "device_key" not in cols:
                self._conn.execute("ALTER TABLE devices ADD COLUMN device_key TEXT")
                self._conn.commit()

    def insert_sms(
        self,
        sender: str,
        text: str,
        received_at: Optional[str] = None,
        client_msg_id: Optional[str] = None,
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
                "INSERT INTO sms (client_msg_id, sender, text, received_at, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (client_msg_id, sender, text, received_at, now),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM sms WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return dict(row), True

    def list_sms(self, limit: int = 50, before_id: Optional[int] = None) -> list[dict]:
        limit = max(1, min(limit, 500))
        with self._lock:
            if before_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM sms WHERE id < ? ORDER BY id DESC LIMIT ?",
                    (before_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM sms ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
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

    def list_calls(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
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

    def insert_outbox_sms(self, client_msg_id: str, recipient: str, text: str) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sms_outbox (client_msg_id, recipient, text, status, created_at)"
                " VALUES (?, ?, ?, 'pending', ?)",
                (client_msg_id, recipient, text, now),
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

    def list_outbox_sms(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(limit, 500))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sms_outbox ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
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

    def create_device(self, name: str, key_hash: str, device_key: str) -> dict:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO devices (name, key_hash, device_key, created_at) VALUES (?, ?, ?, ?)",
                (name, key_hash, device_key, now),
            )
            self._conn.commit()
            return dict(self._conn.execute(
                "SELECT id, name, device_key, disabled, created_at, last_seen_at FROM devices WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone())

    def get_device(self, device_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, key_hash, disabled, created_at, last_seen_at FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_device_by_hash(self, key_hash: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE key_hash = ?", (key_hash,)
            ).fetchone()
            return dict(row) if row else None

    def list_devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, device_key, disabled, created_at, last_seen_at FROM devices ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

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
                "SELECT id, name, device_key, disabled, created_at, last_seen_at FROM devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_device(self, device_id: int, **fields) -> Optional[dict]:
        sets = {k: v for k, v in fields.items() if k in _DEVICE_FIELDS and v is not None}
        with self._lock:
            if sets:
                cols = ", ".join(f"{k} = ?" for k in sets)
                self._conn.execute(
                    f"UPDATE devices SET {cols} WHERE id = ?", (*sets.values(), device_id)
                )
                self._conn.commit()
            row = self._conn.execute(
                "SELECT id, name, disabled, created_at, last_seen_at FROM devices WHERE id = ?",
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
