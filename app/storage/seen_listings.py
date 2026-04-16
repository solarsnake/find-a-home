"""
Persistent store of listing IDs we have already processed.

Current backend: a flat JSON file (data/seen_listings.json).
Future web/iOS: swap this class for a SQLite or PostgreSQL-backed version —
the interface stays the same.

Thread/process safety: the file is read-once at startup and written atomically
(write to .tmp then rename) so concurrent processes don't corrupt it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Set


class SeenListings:
    def __init__(self, path: str = "data/seen_listings.json") -> None:
        self._path = Path(path)
        self._seen: Set[str] = self._load()

    def _load(self) -> Set[str]:
        if self._path.exists():
            try:
                return set(json.loads(self._path.read_text()))
            except (json.JSONDecodeError, ValueError):
                return set()
        return set()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._seen), indent=2))
        # Atomic rename (POSIX)
        os.replace(tmp, self._path)

    def is_new(self, listing_id: str) -> bool:
        return listing_id not in self._seen

    def mark_seen(self, listing_id: str) -> None:
        self._seen.add(listing_id)
        self._save()

    def mark_seen_bulk(self, listing_ids: list[str]) -> None:
        self._seen.update(listing_ids)
        self._save()

    def count(self) -> int:
        return len(self._seen)
