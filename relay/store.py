"""SQLite message index. Files live on disk beside it."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    direction   TEXT    NOT NULL CHECK (direction IN ('inbound', 'reply')),
    sender      TEXT    NOT NULL DEFAULT 'dad',
    photo_bytes INTEGER,
    audio_bytes INTEGER,
    audio_ms    INTEGER,
    seen_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dir_id ON messages (direction, id);
"""


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "index.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def msg_dir(self, msg_id: int) -> Path:
        d = self.root / str(msg_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create(self, direction: str, sender: str = "dad") -> int:
        cur = self.db.execute(
            "INSERT INTO messages (ts, direction, sender) VALUES (?, ?, ?)",
            (int(time.time()), direction, sender),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finalize(self, msg_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE messages SET {cols} WHERE id = ?", (*fields.values(), msg_id)
        )
        self.db.commit()

    def since(self, direction: str, after: int, limit: int = 20):
        """Oldest-first, for the device's incremental catch-up: it advances its
        watermark to whatever it's given, so skipping ahead would lose whatever
        sat between the old watermark and the newer id."""
        return self.db.execute(
            "SELECT * FROM messages WHERE direction = ? AND id > ? "
            "AND (photo_bytes IS NOT NULL OR audio_bytes IS NOT NULL) "
            "ORDER BY id LIMIT ?",
            (direction, after, limit),
        ).fetchall()

    def latest(self, direction: str, after: int, limit: int = 20):
        """Newest-first-then-reversed, for a listing that always re-fetches
        from `after` with no cursor of its own (the phone's reply list). Using
        `since` here would mean the oldest `limit` rows win forever once a
        direction passes `limit` rows, hiding everything newer."""
        rows = self.db.execute(
            "SELECT * FROM messages WHERE direction = ? AND id > ? "
            "AND (photo_bytes IS NOT NULL OR audio_bytes IS NOT NULL) "
            "ORDER BY id DESC LIMIT ?",
            (direction, after, limit),
        ).fetchall()
        return list(reversed(rows))

    def get(self, msg_id: int):
        return self.db.execute(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()

    def mark_seen(self, msg_id: int) -> None:
        self.db.execute(
            "UPDATE messages SET seen_at = ? WHERE id = ? AND seen_at IS NULL",
            (int(time.time()), msg_id),
        )
        self.db.commit()

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def delete_older_than(self, days: int) -> list[int]:
        cutoff = int(time.time()) - days * 86400
        rows = self.db.execute(
            "SELECT id FROM messages WHERE ts < ?", (cutoff,)
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        self.db.executemany("DELETE FROM messages WHERE id = ?", [(i,) for i in ids])
        self.db.commit()
        return ids
