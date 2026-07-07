import os
import sqlite3


class ReviewStore:
    """Persistent record of reviewed units (commits, PR heads) so redelivered
    webhooks don't produce duplicate comments — including across restarts,
    which the old in-memory store couldn't survive."""

    def __init__(self, path: str | None = None, max_rows: int = 10000):
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
                "CREATE TABLE IF NOT EXISTS reviewed ("
                "  key TEXT PRIMARY KEY,"
                "  created_at TEXT DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
            self._conn.commit()
        return self._conn

    def already_reviewed(self, key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM reviewed WHERE key = ?", (key,)
        ).fetchone()
        return row is not None

    def mark_reviewed(self, key: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO reviewed (key) VALUES (?)", (key,)
        )
        # Keep the table bounded: drop the oldest rows past the cap.
        self.conn.execute(
            "DELETE FROM reviewed WHERE rowid <= "
            "  (SELECT MAX(rowid) FROM reviewed) - ?",
            (self.max_rows,),
        )
        self.conn.commit()
