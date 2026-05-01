"""
Search API routes.

POST /api/v1/search
  Body: SearchProfile (JSON)
  Returns: list[MatchResult]

GET  /api/v1/profiles
  Returns: list[SearchProfile] from search_profiles.json

POST /api/v1/profiles
  Body: SearchProfile (JSON)
  Appends to search_profiles.json

DELETE /api/v1/profiles/{name}
  Removes a profile by name from search_profiles.json
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.config import settings, load_profiles
from app.engine import Engine
from app.models import MatchResult, SearchProfile
from app.rates import fetch_current_rates

router = APIRouter(tags=["search"])

_PROFILES_PATH = "search_profiles.json"


@router.get("/rates")
async def get_rates() -> dict:
    """Current 30yr and 15yr fixed mortgage rates from Freddie Mac PMMS."""
    return await fetch_current_rates()


@router.post("/search", response_model=list[MatchResult])
async def run_search(profile: SearchProfile) -> list[MatchResult]:
    """
    Run a one-off search for the given profile and return matching listings.
    If interest_rate is the default (0.065), replace it with the current
    Freddie Mac PMMS rate so searches always use a live market rate.
    Results are NOT persisted as 'seen' — same listing can surface on repeat calls.
    """
    try:
        # Inject live rate if the caller left the default
        if profile.interest_rate == 0.065:
            rates = await fetch_current_rates()
            profile = profile.model_copy(update={"interest_rate": rates["rate_30yr"]})
        engine = Engine(settings, dry_run=True)
        results = await engine.run([profile])
        return results
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/profiles", response_model=list[SearchProfile])
async def list_profiles() -> list[SearchProfile]:
    """Return all configured search profiles from search_profiles.json."""
    try:
        return load_profiles(_PROFILES_PATH)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/profiles", response_model=SearchProfile, status_code=201)
async def create_profile(profile: SearchProfile) -> SearchProfile:
    """Add a new search profile to search_profiles.json."""
    p = Path(_PROFILES_PATH)
    try:
        data = json.loads(p.read_text()) if p.exists() else {"profiles": []}
        existing_names = [pr["name"] for pr in data.get("profiles", [])]
        if profile.name in existing_names:
            raise HTTPException(
                status_code=409, detail=f"Profile '{profile.name}' already exists"
            )
        data.setdefault("profiles", []).append(profile.model_dump(mode="json"))
        p.write_text(json.dumps(data, indent=2))
        return profile
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/profiles/{name}", status_code=200)
async def delete_profile(name: str) -> dict:
    """Remove a profile by name from search_profiles.json."""
    p = Path(_PROFILES_PATH)
    try:
        data = json.loads(p.read_text())
        profiles = data.get("profiles", [])
        updated = [pr for pr in profiles if pr["name"] != name]
        if len(updated) == len(profiles):
            raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")
        data["profiles"] = updated
        p.write_text(json.dumps(data, indent=2))
        return {"deleted": name}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
