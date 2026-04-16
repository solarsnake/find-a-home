"""
Realtor.com scraper via the HomeHarvest library.

HomeHarvest wraps Realtor.com's internal GraphQL API — no browser required.
It returns a pandas DataFrame which we convert to RawListing objects.

pip install homeharvest
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator, Optional

from app.config import settings
from app.models import DataSource, PropertyType, RawListing, SearchProfile
from app.scraper.base import BaseScraper, ScraperError

logger = logging.getLogger(__name__)

try:
    from homeharvest import scrape_property
    HOMEHARVEST_AVAILABLE = True
except (ImportError, TypeError):
    # TypeError is raised on Python < 3.10: homeharvest uses X | Y union syntax
    # in function signatures which requires Python 3.10+ to evaluate at import time.
    # Upgrade to Python 3.10+ to enable Realtor.com scraping.
    HOMEHARVEST_AVAILABLE = False

# Map our PropertyType → HomeHarvest style strings
_STYLE_MAP: dict[str, list[str]] = {
    PropertyType.SINGLE_FAMILY: ["SINGLE_FAMILY", "single_family"],
    PropertyType.MULTI_FAMILY: ["MULTI_FAMILY", "multi_family"],
    PropertyType.CONDO: ["CONDOS", "condo", "APARTMENT"],
    PropertyType.TOWNHOUSE: ["TOWNHOMES", "townhouse", "townhome"],
    PropertyType.LAND: ["LAND", "land"],
}


def _beds_baths_from_row(row) -> tuple[int, float]:
    beds = int(row.get("beds") or 0)
    full = float(row.get("full_baths") or 0)
    half = float(row.get("half_baths") or 0)
    baths = full + (half * 0.5)
    return beds, baths


def _safe_int(val) -> Optional[int]:
    try:
        return int(val) if val is not None and str(val) != "nan" else None
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None and str(val) != "nan" else None
    except (ValueError, TypeError):
        return None


class RealtorScraper(BaseScraper):
    source_name = "realtor"

    def __init__(self) -> None:
        if not HOMEHARVEST_AVAILABLE:
            raise ImportError(
                "homeharvest is unavailable. If you are on Python 3.9, upgrade to "
                "Python 3.10+ — homeharvest uses union syntax (X | Y) that requires it.\n"
                "brew install python@3.11 && python3.11 -m venv .venv"
            )

    async def search(self, profile: SearchProfile) -> AsyncIterator[RawListing]:
        seen_in_run: set[str] = set()

        for zip_code in profile.zip_codes:
            try:
                # HomeHarvest is synchronous — run in a thread so we don't block the event loop
                df = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda z=zip_code: scrape_property(
                        location=z,
                        listing_type="for_sale",
                        beds_min=profile.min_bedrooms,
                        beds_max=profile.max_bedrooms,
                        baths_min=int(profile.min_bathrooms),
                        sqft_min=profile.min_sqft,
                        sqft_max=profile.max_sqft,
                        price_max=int(profile.max_price) if profile.max_price else None,
                        price_min=int(profile.min_price) if profile.min_price else None,
                        property_type=_property_types_for_homeharvest(profile),
                    ),
                )

                if df is None or df.empty:
                    continue

                for _, row in df.iterrows():
                    row = row.to_dict()

                    mls_id = str(row.get("mls_id") or row.get("listing_id") or "")
                    uid = f"realtor_{zip_code}_{mls_id}" if mls_id else None
                    if not uid or uid in seen_in_run:
                        continue
                    seen_in_run.add(uid)

                    price = _safe_float(row.get("list_price"))
                    if not price:
                        continue

                    beds, baths = _beds_baths_from_row(row)

                    # URL
                    url = str(row.get("property_url") or "")
                    if not url.startswith("http"):
                        url = f"https://www.realtor.com{url}"

                    # Address
                    street = str(row.get("street") or "")
                    unit = str(row.get("unit") or "")
                    if unit and unit != "nan":
                        street = f"{street} #{unit}"
                    city = str(row.get("city") or "")
                    state = str(row.get("state") or "")
                    zip_val = str(row.get("zip_code") or zip_code)

                    # Description — HomeHarvest may call it 'text' or 'description'
                    description = str(
                        row.get("description") or row.get("text") or ""
                    )
                    if description == "nan":
                        description = ""

                    # HOA
                    hoa = _safe_float(row.get("hoa_fee"))

                    # Property type
                    ptype = str(row.get("style") or row.get("property_type") or "")
                    if ptype == "nan":
                        ptype = ""

                    yield RawListing(
                        listing_id=uid,
                        source=DataSource.REALTOR,
                        url=url,
                        address=street,
                        city=city,
                        state=state,
                        zip_code=zip_val,
                        price=price,
                        bedrooms=beds,
                        bathrooms=baths,
                        sqft=_safe_int(row.get("sqft")),
                        lot_sqft=_safe_int(row.get("lot_sqft")),
                        hoa_monthly=hoa,
                        description=description,
                        property_type=ptype or None,
                        days_on_market=_safe_int(row.get("days_on_mls")),
                        year_built=_safe_int(row.get("year_built")),
                        scraped_at=datetime.utcnow(),
                    )

                # Brief pause between zip codes
                await asyncio.sleep(settings.scrape_delay_min)

            except Exception as exc:
                logger.warning("Realtor.com scrape error for %s: %s", zip_code, exc)
                # Don't raise — skip the zip and continue with others


def _property_types_for_homeharvest(profile: SearchProfile) -> Optional[list[str]]:
    """Convert our PropertyType list to HomeHarvest-compatible style strings."""
    if not profile.property_types or PropertyType.ANY in profile.property_types:
        return None
    types = []
    for pt in profile.property_types:
        types.extend(_STYLE_MAP.get(pt, []))
    return types or None
