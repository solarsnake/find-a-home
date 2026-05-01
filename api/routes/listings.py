"""
Seen-listings history routes.

GET /api/v1/results
  Returns paginated records from the SQLite seen_listings store.
  Each row: listing_id, first_seen_at, last_seen_at, last_price, address, zip_code.

GET /api/v1/results/stats
  Summary counts: total seen, seen in last 7/30 days.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from app.config import settings

router = APIRouter(tags=["listings"])


def _get_conn():
    import sqlite3
    from pathlib import Path
    db = Path(settings.seen_listings_file)
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


@router.get("/results")
async def get_results(page: int = 1, per_page: int = 20) -> dict:
    """Paginated list of all previously seen listing IDs with metadata."""
    if page < 1 or per_page < 1:
        raise HTTPException(status_code=400, detail="page and per_page must be ≥ 1")
    conn = _get_conn()
    if conn is None:
        return {"page": page, "per_page": per_page, "total": 0, "results": []}
    try:
        total = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
        offset = (page - 1) * per_page
        rows = conn.execute(
            "SELECT listing_id, first_seen_at, last_seen_at, last_price, address, zip_code "
            "FROM seen_listings ORDER BY last_seen_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "results": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@router.get("/results/stats")
async def get_stats() -> dict:
    """Summary statistics about the seen-listings store."""
    conn = _get_conn()
    if conn is None:
        return {"total": 0, "seen_last_7d": 0, "seen_last_30d": 0}
    try:
        now = time.time()
        total = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
        last_7d = conn.execute(
            "SELECT COUNT(*) FROM seen_listings WHERE last_seen_at >= ?",
            (now - 7 * 86_400,),
        ).fetchone()[0]
        last_30d = conn.execute(
            "SELECT COUNT(*) FROM seen_listings WHERE last_seen_at >= ?",
            (now - 30 * 86_400,),
        ).fetchone()[0]
        return {"total": total, "seen_last_7d": last_7d, "seen_last_30d": last_30d}
    finally:
        conn.close()
