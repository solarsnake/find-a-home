"""
Address → (lat, lon) geocoding.

Primary: US Census Bureau Geocoder (free, no key, no rate limit for normal use, US only).
Fallback: Nominatim (OpenStreetMap) for non-US addresses or Census failures.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "find-a-home/1.0 (real-estate-deal-finder; local-dev)"


async def _geocode_census(
    address: str, city: str, state: str, zip_code: str, client: httpx.AsyncClient
) -> Optional[tuple[float, float]]:
    """US Census Bureau geocoder — free, reliable, no rate limit."""
    params = {
        "street": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    try:
        resp = await client.get(_CENSUS_URL, params=params, timeout=8.0)
        resp.raise_for_status()
        matches = resp.json()["result"]["addressMatches"]
        if matches:
            c = matches[0]["coordinates"]
            return float(c["y"]), float(c["x"])   # y=lat, x=lon
    except Exception as exc:
        logger.debug("Census geocoder failed for %s %s: %s", address, zip_code, exc)
    return None


async def _geocode_nominatim(
    address: str, city: str, state: str, zip_code: str, client: httpx.AsyncClient
) -> Optional[tuple[float, float]]:
    """Nominatim fallback — use for non-US or when Census misses an address."""
    params = {
        "q": f"{address}, {city}, {state} {zip_code}",
        "format": "json",
        "limit": "1",
        "countrycodes": "us",
    }
    try:
        resp = await client.get(
            _NOMINATIM_URL, params=params,
            headers={"User-Agent": _USER_AGENT},
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as exc:
        logger.debug("Nominatim geocoder failed for %s %s: %s", address, zip_code, exc)
    return None


async def geocode_address(
    address: str,
    city: str,
    state: str,
    zip_code: str,
) -> Optional[tuple[float, float]]:
    """
    Return (lat, lon) for a property address, or None if not found.
    Tries Census Bureau first (best for US), falls back to Nominatim.
    """
    async with httpx.AsyncClient() as client:
        coords = await _geocode_census(address, city, state, zip_code, client)
        if coords:
            return coords
        return await _geocode_nominatim(address, city, state, zip_code, client)
