"""Local SQLite retry queue: SMS that failed to upload are kept here and
re-sent with exponential backoff until the server accepts them."""

import json
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_msg_id TEXT UNIQUE NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry REAL NOT NULL,
    created_at REAL NOT NULL
);
"""


class Outbox:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def enqueue(self, client_msg_id: str, payload: dict):
        self._conn.execute(
            "INSERT OR IGNORE INTO outbox (client_msg_id, payload, next_retry, created_at)"
            " VALUES (?, ?, ?, ?)",
            (client_msg_id, json.dumps(payload, ensure_ascii=False), time.time(), time.time()),
        )
        self._conn.commit()

    def due(self, limit: int = 20) -> list[tuple[int, dict, int]]:
        rows = self._conn.execute(
            "SELECT id, payload, attempts FROM outbox WHERE next_retry <= ?"
            " ORDER BY id LIMIT ?",
            (time.time(), limit),
        ).fetchall()
        return [(r[0], json.loads(r[1]), r[2]) for r in rows]

    def done(self, row_id: int):
        self._conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
        self._conn.commit()

    def fail(self, row_id: int, attempts: int):
        delay = min(300.0, 5.0 * (2 ** attempts))
        self._conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, next_retry = ? WHERE id = ?",
            (time.time() + delay, row_id),
        )
        self._conn.commit()

    def size(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
