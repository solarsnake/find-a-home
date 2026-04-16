"""
Search API routes.

POST /api/v1/search
  Body: SearchProfile (JSON)
  Returns: list[MatchResult]

This is the endpoint a "Search Now" button on the web/iOS UI will call.
The body is identical to a row in search_profiles.json.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.engine import Engine
from app.models import MatchResult, SearchProfile

router = APIRouter(tags=["search"])


@router.post("/search", response_model=list[MatchResult])
async def run_search(profile: SearchProfile) -> list[MatchResult]:
    """
    Run a one-off search for the given profile and return matching listings.
    Results are NOT persisted as 'seen' when triggered via the API so the same
    listing can surface again on a subsequent API call.
    """
    try:
        engine = Engine(settings, dry_run=True)  # dry_run: don't mark seen or fire SMS
        results = await engine.run([profile])
        return results
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/profiles")
async def list_profiles() -> list[SearchProfile]:
    """Return all configured search profiles from search_profiles.json."""
    from app.config import load_profiles
    try:
        return load_profiles()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
