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
"""

_CALL_FIELDS = ("direction", "number", "status", "started_at",
                "answered_at", "ended_at", "duration", "recording_file")


class Database:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

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


def to_event(row: dict) -> str:
    return json.dumps({"type": "sms", "data": row}, ensure_ascii=False)
