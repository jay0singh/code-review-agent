import json
import os
import secrets
import sqlite3


def new_token() -> str:
    return secrets.token_urlsafe(9)


class PendingReviewStore:
    """Persistent store of review payloads awaiting a human's approve/reject
    decision via Telegram. Payload-agnostic: stores/returns arbitrary
    JSON-serializable dicts under a short opaque token."""

    def __init__(self, path: str | None = None, max_rows: int = 1000):
        self._path = path
        self._conn = None
        self.max_rows = max_rows

    @property
    def conn(self):
        # Lazy: no db file is created until the store is actually used.
        if self._conn is None:
            path = self._path or os.getenv("DEDUPE_DB", "reviewed.db")
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS pending_reviews ("
                "  token TEXT PRIMARY KEY,"
                "  payload TEXT NOT NULL,"
                "  created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            self._conn.commit()
        return self._conn

    def save(self, token: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO pending_reviews (token, payload) VALUES (?, ?)",
            (token, json.dumps(payload)),
        )
        # Keep the table bounded: drop the oldest rows past the cap.
        self.conn.execute(
            "DELETE FROM pending_reviews WHERE rowid <= "
            "  (SELECT MAX(rowid) FROM pending_reviews) - ?",
            (self.max_rows,),
        )
        self.conn.commit()

    def take(self, token: str) -> dict | None:
        """Pop semantics: a token can be consumed at most once."""
        row = self.conn.execute(
            "SELECT payload FROM pending_reviews WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "DELETE FROM pending_reviews WHERE token = ?", (token,)
        )
        self.conn.commit()
        return json.loads(row[0])
