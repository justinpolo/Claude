"""SQLite persistence: which listings we've already seen/alerted, per-watch baselines."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    scope TEXT NOT NULL,          -- watch fingerprint
    item_key TEXT NOT NULL,       -- platform:id
    first_seen REAL NOT NULL,
    PRIMARY KEY (scope, item_key)
);
CREATE TABLE IF NOT EXISTS baseline (
    scope TEXT PRIMARY KEY,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watermark (
    scope TEXT PRIMARY KEY,       -- watch fingerprint
    max_id INTEGER NOT NULL       -- highest numeric listing id seen (ids only ever go up)
);
CREATE TABLE IF NOT EXISTS alerts (
    item_key TEXT PRIMARY KEY,
    watch_id TEXT NOT NULL,
    title TEXT, url TEXT, price REAL, currency TEXT,
    listed_ts REAL, alerted_ts REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str = "sniper.db"):
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def has_baseline(self, scope: str) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM baseline WHERE scope=?", (scope,)).fetchone() is not None

    def set_baseline(self, scope: str):
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO baseline VALUES (?,?)", (scope, time.time()))
            self.db.commit()

    def get_watermark(self, scope: str) -> Optional[int]:
        with self._lock:
            r = self.db.execute("SELECT max_id FROM watermark WHERE scope=?", (scope,)).fetchone()
            return r[0] if r else None

    def raise_watermark(self, scope: str, max_id: int):
        with self._lock:
            self.db.execute(
                "INSERT INTO watermark VALUES (?,?) ON CONFLICT(scope) "
                "DO UPDATE SET max_id=MAX(max_id, excluded.max_id)", (scope, max_id))
            self.db.commit()

    def filter_unseen(self, scope: str, keys: Iterable[str]) -> list[str]:
        """Return keys not seen before in this scope, and mark them seen (atomically)."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return []
        now = time.time()
        with self._lock:
            q = "SELECT item_key FROM seen WHERE scope=? AND item_key IN (%s)" % ",".join("?" * len(keys))
            seen = {r[0] for r in self.db.execute(q, (scope, *keys))}
            new = [k for k in keys if k not in seen]
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?)",
                                [(scope, k, now) for k in new])
            self.db.commit()
        return new

    def claim_alert(self, item_key: str, watch_id: str, listing=None) -> bool:
        """True if this item has never been alerted (and records it). Prevents duplicate pings
        when two watches match the same listing."""
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?,?,?)",
                (item_key, watch_id,
                 getattr(listing, "title", None), getattr(listing, "url", None),
                 getattr(listing, "price", None), getattr(listing, "currency", None),
                 getattr(listing, "created_ts", None), time.time()))
            self.db.commit()
            return cur.rowcount == 1

    def recent_alerts(self, limit: int = 10) -> list[tuple]:
        with self._lock:
            return self.db.execute(
                "SELECT watch_id, title, price, currency, url, alerted_ts FROM alerts "
                "ORDER BY alerted_ts DESC LIMIT ?", (limit,)).fetchall()

    def alert_count(self, since: Optional[float] = None) -> int:
        with self._lock:
            if since:
                return self.db.execute("SELECT COUNT(*) FROM alerts WHERE alerted_ts>=?", (since,)).fetchone()[0]
            return self.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

    def prune(self, days: float = 30):
        cutoff = time.time() - days * 86400
        with self._lock:
            self.db.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,))
            self.db.execute("DELETE FROM alerts WHERE alerted_ts < ?", (cutoff,))
            self.db.commit()

    def close(self):
        self.db.close()
