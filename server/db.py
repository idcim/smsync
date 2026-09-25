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
"""


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


def to_event(row: dict) -> str:
    return json.dumps({"type": "sms", "data": row}, ensure_ascii=False)
