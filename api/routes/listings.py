"""
Listings history routes.

GET /api/v1/results — paginated list of all past MatchResults
GET /api/v1/results/{listing_id} — single result detail

Currently stubs — will read from a proper database in the web/iOS version.
The seen_listings.json file only stores IDs; full result objects would need
a results store (SQLite → Postgres).  Wired up here so the route contract
is established before the storage layer is upgraded.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["listings"])


@router.get("/results")
async def get_results(page: int = 1, per_page: int = 20) -> dict:
    """
    Placeholder — returns a stub response until persistent result storage
    is implemented.
    """
    return {
        "page": page,
        "per_page": per_page,
        "total": 0,
        "results": [],
        "_note": (
            "Full result history requires a database backend. "
            "Run `python main.py run` to generate and display results."
        ),
    }
