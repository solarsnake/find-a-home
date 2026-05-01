"""
Persistent store of listing IDs we have already processed.

Backend: SQLite (data/seen_listings.db).
Migrates automatically from the old seen_listings.json on first run.

Schema:
  listing_id    TEXT PRIMARY KEY
  first_seen_at REAL  (unix timestamp)
  last_seen_at  REAL
  last_price    REAL
  address       TEXT
  zip_code      TEXT

Features:
  - is_new(id)                   → True if never seen before
  - price_changed(id, price)     → True if price dropped vs stored price
  - mark_seen_bulk(ids, prices)  → upsert all, update last_price + last_seen_at
  - prune_old(days=90)           → delete rows not seen for N days
  - migrate_from_json(path)      → one-time import of old flat-file IDs

Thread/process safety: SQLite WAL mode; writes are serialised by the GIL
in a single process, which is all this app needs.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_listings (
    listing_id    TEXT PRIMARY KEY,
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    last_price    REAL,
    address       TEXT,
    zip_code      TEXT
);
CREATE INDEX IF NOT EXISTS idx_last_seen ON seen_listings (last_seen_at);
"""


class SeenListings:
    def __init__(self, path: str = "data/seen_listings.db") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate_from_json()
        self.prune_old()

    # ── Migration ─────────────────────────────────────────────────────────────

    def _migrate_from_json(self) -> None:
        """One-time import of seen_listings.json → SQLite."""
        json_path = self._path.parent / "seen_listings.json"
        if not json_path.exists():
            return
        try:
            ids = json.loads(json_path.read_text())
            if not isinstance(ids, list):
                return
            now = time.time()
            self._conn.executemany(
                "INSERT OR IGNORE INTO seen_listings "
                "(listing_id, first_seen_at, last_seen_at) VALUES (?, ?, ?)",
                [(lid, now, now) for lid in ids],
            )
            self._conn.commit()
            # Rename so we don't re-migrate
            json_path.rename(json_path.with_suffix(".json.migrated"))
            logger.info(
                "Migrated %d listing IDs from seen_listings.json → SQLite", len(ids)
            )
        except Exception as exc:
            logger.warning("seen_listings.json migration failed: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def is_new(self, listing_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        return row is None

    def price_dropped(self, listing_id: str, current_price: float, threshold: float = 5_000) -> bool:
        """
        True if we've seen this listing before AND the price dropped by at least
        `threshold` dollars since we last stored it.
        """
        row = self._conn.execute(
            "SELECT last_price FROM seen_listings WHERE listing_id = ?", (listing_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return False
        return (row[0] - current_price) >= threshold

    def mark_seen_bulk(
        self,
        listing_ids: list[str],
        prices: Optional[dict[str, float]] = None,
        addresses: Optional[dict[str, str]] = None,
        zip_codes: Optional[dict[str, str]] = None,
    ) -> None:
        now = time.time()
        prices = prices or {}
        addresses = addresses or {}
        zip_codes = zip_codes or {}
        rows = [
            (
                lid,
                now,                    # first_seen_at (ignored on conflict)
                now,                    # last_seen_at
                prices.get(lid),
                addresses.get(lid),
                zip_codes.get(lid),
            )
            for lid in listing_ids
        ]
        self._conn.executemany(
            """
            INSERT INTO seen_listings
                (listing_id, first_seen_at, last_seen_at, last_price, address, zip_code)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(listing_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                last_price   = COALESCE(excluded.last_price, last_price),
                address      = COALESCE(excluded.address, address),
                zip_code     = COALESCE(excluded.zip_code, zip_code)
            """,
            rows,
        )
        self._conn.commit()

    def prune_old(self, days: int = 90) -> None:
        """Remove listings not seen in the last `days` days."""
        cutoff = time.time() - days * 86_400
        cur = self._conn.execute(
            "DELETE FROM seen_listings WHERE last_seen_at < ?", (cutoff,)
        )
        self._conn.commit()
        if cur.rowcount:
            logger.info("Pruned %d stale listing(s) (not seen in %d days)", cur.rowcount, days)

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
