"""
Current mortgage rate fetcher.

Source: Freddie Mac Primary Mortgage Market Survey (PMMS)
  https://www.freddiemac.com/pmms
  Published weekly (Thursdays). Free, no API key.

Returns the most recent 30-year and 15-year fixed rates as decimals
(e.g. 0.063 for 6.3%).

Results are cached in-process for 6 hours so repeat searches in the
same server session don't re-fetch.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_PMMS_CSV = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
_USER_AGENT = "find-a-home/1.0 (real-estate-deal-finder; local-dev)"
_CACHE_TTL = 6 * 3600   # 6 hours

_cache: dict = {}   # {"30yr": float, "15yr": float, "fetched_at": float, "as_of": str}


async def fetch_current_rates() -> dict:
    """
    Return {"rate_30yr": float, "rate_15yr": float, "as_of": str}.
    Falls back to {"rate_30yr": 0.065, "rate_15yr": 0.059} on error.
    Uses a 6-hour in-process cache.
    """
    global _cache

    # Return cached value if fresh
    if _cache and (time.monotonic() - _cache.get("fetched_at", 0)) < _CACHE_TTL:
        return _cache

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                _PMMS_CSV,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            lines = [l.strip() for l in resp.text.splitlines() if l.strip()]

        # Find the last data row (skip header rows that don't start with a date)
        rate_30yr: Optional[float] = None
        rate_15yr: Optional[float] = None
        as_of: str = ""

        for line in reversed(lines):
            parts = line.split(",")
            if len(parts) < 3:
                continue
            try:
                r30 = float(parts[1]) if parts[1].strip() else None
                r15 = float(parts[2]) if parts[2].strip() else None
                if r30 and r30 > 0:
                    rate_30yr = r30 / 100
                    rate_15yr = (r15 / 100) if r15 and r15 > 0 else None
                    as_of = parts[0].strip()
                    break
            except (ValueError, IndexError):
                continue

        if rate_30yr is None:
            raise ValueError("Could not parse rate from PMMS CSV")

        result = {
            "rate_30yr": rate_30yr,
            "rate_15yr": rate_15yr or round(rate_30yr - 0.007, 4),
            "as_of": as_of,
            "fetched_at": time.monotonic(),
            "source": "Freddie Mac PMMS",
        }
        _cache = result
        logger.info(
            "Mortgage rates fetched: 30yr=%.2f%% 15yr=%.2f%% (as of %s)",
            rate_30yr * 100, result["rate_15yr"] * 100, as_of,
        )
        return result

    except Exception as exc:
        logger.warning("Could not fetch mortgage rates from Freddie Mac: %s — using fallback", exc)
        fallback = {
            "rate_30yr": 0.065,
            "rate_15yr": 0.059,
            "as_of": "fallback",
            "fetched_at": time.monotonic(),
            "source": "fallback",
        }
        # Cache fallback for only 30 min so we retry sooner
        fallback["fetched_at"] -= (_CACHE_TTL - 1800)
        return fallback
