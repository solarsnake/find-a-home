"""
Geocoding route — converts a place name or address to zip codes.

GET /api/v1/geocode?q={query}
  Returns a list of {zip_code, place_name, state, country} objects.

Strategy:
  1. If query is a 5-digit zip code, return it directly.
  2. If query looks like "City ST" or "City State", use zippopotam.us
     which maps US city names → zip codes (free, no key).
  3. Fall back to Nominatim (OpenStreetMap) for non-US or unrecognized patterns.
"""

from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["geocode"])

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_ZIPPO_URL = "https://api.zippopotam.us/us"
_USER_AGENT = "find-a-home/1.0 (real-estate-deal-finder; local-dev)"

# US state abbreviation → full name (and reverse)
_STATE_ABBREVS: dict[str, str] = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
_STATE_NAMES_TO_ABBREV = {v: k for k, v in _STATE_ABBREVS.items()}


def _parse_city_state(query: str) -> tuple[str, str] | None:
    """
    Try to extract (city, state_abbrev) from a query like:
      "Escondido CA", "Durham NC", "Lake Hartwell GA", "Durham North Carolina"
    Returns None if parsing fails.
    """
    q = query.strip()

    # Match "City ST" (2-letter abbreviation at end)
    m = re.match(r"^(.+?)[,\s]+([A-Z]{2})$", q, re.IGNORECASE)
    if m:
        city = m.group(1).strip()
        state = m.group(2).upper()
        if state in _STATE_ABBREVS:
            return city, state

    # Match "City, Full State Name" or "City Full State Name"
    q_lower = q.lower()
    for abbrev, full in _STATE_ABBREVS.items():
        if q_lower.endswith(full):
            city = q[:len(q) - len(full)].rstrip(", ").strip()
            if city:
                return city, abbrev
        if q_lower.endswith(abbrev.lower()):
            city = q[:len(q) - len(abbrev)].rstrip(", ").strip()
            if city:
                return city, abbrev

    return None


async def _lookup_by_zip(zip_code: str, client: httpx.AsyncClient) -> list[dict]:
    """Reverse-geocode a single zip code to get its place name."""
    try:
        resp = await client.get(f"{_ZIPPO_URL}/{zip_code}", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            place = data.get("places", [{}])[0].get("place name", "")
            state = data.get("state", "")
            return [{
                "zip_code": zip_code,
                "place_name": f"{place}, {state}" if place else zip_code,
                "state": state,
                "country": "US",
            }]
    except Exception:
        pass
    return [{"zip_code": zip_code, "place_name": zip_code, "state": "", "country": "US"}]


async def _lookup_city_state(city: str, state_abbrev: str, client: httpx.AsyncClient) -> list[dict]:
    """Use zippopotam.us to find all zip codes for a US city/state pair."""
    city_slug = city.lower().replace(" ", "%20")
    state_slug = state_abbrev.lower()
    try:
        resp = await client.get(
            f"{_ZIPPO_URL}/{state_slug}/{city_slug}",
            timeout=8.0,
            headers={"User-Agent": _USER_AGENT},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        state_full = data.get("state", state_abbrev)
        results = []
        for place in data.get("places", []):
            zip_code = place.get("post code", "")
            place_name = place.get("place name", city)
            if zip_code:
                results.append({
                    "zip_code": zip_code,
                    "place_name": f"{place_name}, {state_full}",
                    "state": state_full,
                    "country": "US",
                })
        return results
    except Exception:
        return []


async def _lookup_nominatim(query: str, client: httpx.AsyncClient) -> list[dict]:
    """Nominatim fallback — useful for non-US or complex queries."""
    try:
        resp = await client.get(
            _NOMINATIM_URL,
            params={"q": query, "format": "json", "addressdetails": "1", "limit": "15"},
            timeout=8.0,
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    results = []
    seen: set[str] = set()
    for item in data:
        addr = item.get("address", {})
        zip_code = addr.get("postcode", "").split("-")[0].strip()
        if not zip_code or zip_code in seen:
            continue
        if not re.match(r"^\d{5}$", zip_code):
            continue
        seen.add(zip_code)
        city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county") or ""
        state = addr.get("state", "")
        results.append({
            "zip_code": zip_code,
            "place_name": ", ".join(filter(None, [city, state])),
            "state": state,
            "country": addr.get("country_code", "").upper(),
        })
    return results


@router.get("/geocode")
async def geocode(q: str) -> list[dict]:
    """
    Convert a location query to a list of matching zip codes with place names.

    Examples:
      /api/v1/geocode?q=Escondido+CA
      /api/v1/geocode?q=92025
      /api/v1/geocode?q=Durham+North+Carolina
      /api/v1/geocode?q=Lake+Hartwell+GA
    """
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Query must be at least 2 characters")

    async with httpx.AsyncClient() as client:
        # Strategy 1: direct 5-digit zip
        if re.match(r"^\d{5}$", q):
            return await _lookup_by_zip(q, client)

        # Strategy 2: city + state (US)
        parsed = _parse_city_state(q)
        if parsed:
            city, state = parsed
            results = await _lookup_city_state(city, state, client)
            if results:
                return results

        # Strategy 3: Nominatim fallback
        return await _lookup_nominatim(q, client)
