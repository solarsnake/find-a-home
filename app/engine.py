"""
Main orchestration engine.

Ties together: scrapers → filter → alerts → storage.

Callable from:
  - main.py CLI  (asyncio.run(engine.run(...)))
  - api/app.py   (await engine.run(...))  ← future web/iOS backend
  - test harness (direct await)

Returns a list of MatchResult so the caller can render/serialise them
however it needs to (terminal, JSON API response, push notification, …).
"""

from __future__ import annotations

import logging
from typing import Optional  # noqa: F401 — used in _enrich_waterway return type

import re

from app.alerts.email_alert import EmailAlert
from app.alerts.sms import SMSAlert
from app.config import Settings, load_profiles
from app.filters.listing_filter import evaluate_listing
from app.geo.geocode import geocode_address
from app.geo.waterway import WaterwayResult, check_waterway
from app.models import AlertPriority, DataSource, MatchResult, SearchProfile, WaterwayDetails
from app.scraper.base import ScraperError
from app.scraper.homes import HomesScraper
from app.scraper.realtor import RealtorScraper
from app.scraper.redfin import RedfinScraper
from app.scraper.zillow import ZillowScraper
from app.storage.seen_listings import SeenListings

logger = logging.getLogger(__name__)


class Engine:
    """
    Stateless per-run coordinator.
    Create one instance per invocation; don't reuse across runs.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        dry_run: bool = False,
        sources_override: Optional[list[DataSource]] = None,
    ) -> None:
        self._settings = settings
        self._dry_run = dry_run
        self._sources_override = sources_override
        self._seen = SeenListings(settings.seen_listings_file)
        self._sms = SMSAlert()
        self._email = EmailAlert()

    async def run(
        self,
        profiles: list[SearchProfile],
        *,
        profile_name_filter: Optional[str] = None,
    ) -> list[MatchResult]:
        """
        Run all (or a named) profiles and return every MatchResult found.

        Args:
            profiles:             List of SearchProfile objects (from search_profiles.json).
            profile_name_filter:  If set, only run the profile with this exact name.

        Returns:
            All new MatchResult objects found in this run.
        """
        self._settings.ensure_data_dir()

        active = [
            p for p in profiles
            if p.enabled and (profile_name_filter is None or p.name == profile_name_filter)
        ]
        if not active:
            logger.warning("No active profiles found (filter: %s)", profile_name_filter)
            return []

        all_results: list[MatchResult] = []

        for profile in active:
            results = await self._run_profile(profile)
            all_results.extend(results)

        # Fire alerts and run summary
        if not self._dry_run:
            if all_results:
                await self._fire_alerts(all_results)
            profile_names = [p.name for p in active]
            await self._email.send_run_summary(all_results, profile_names)

        # Mark everything seen AFTER alerts so a failed alert doesn't silence a listing
        if not self._dry_run:
            self._seen.mark_seen_bulk(
                [r.listing.listing_id for r in all_results],
                prices={r.listing.listing_id: r.listing.price for r in all_results},
                addresses={r.listing.listing_id: r.listing.address for r in all_results},
                zip_codes={r.listing.listing_id: r.listing.zip_code for r in all_results},
            )

        return all_results

    @staticmethod
    def _address_key(listing) -> str:
        """Normalize address+zip to a dedup key across sources."""
        addr = re.sub(r"[^a-z0-9]", "", listing.address.lower())
        return f"{addr}_{listing.zip_code}"

    async def _run_profile(self, profile: SearchProfile) -> list[MatchResult]:
        sources = self._sources_override or profile.sources
        results: list[MatchResult] = []
        seen_addresses: dict[str, str] = {}   # address_key → listing_id first seen

        # Initialise scrapers for this profile's requested sources
        scrapers: list = []
        scraper_map = {
            DataSource.ZILLOW: ZillowScraper,
            DataSource.REDFIN: RedfinScraper,
            DataSource.REALTOR: RealtorScraper,
            DataSource.HOMES: HomesScraper,
        }
        for source in sources:
            cls = scraper_map.get(source)
            if cls:
                try:
                    scrapers.append(cls())
                except ImportError as exc:
                    logger.warning("%s scraper unavailable: %s", source.value, exc)

        for scraper in scrapers:
            scraper_count = 0
            try:
                async for listing in scraper.search(profile):
                    scraper_count += 1
                    # Skip listings we've already processed — unless price dropped
                    if not self._dry_run and not self._seen.is_new(listing.listing_id):
                        if not self._seen.price_dropped(listing.listing_id, listing.price):
                            logger.debug("SEEN %s — skipping", listing.short_address)
                            continue
                        logger.info(
                            "Price drop detected on %s — re-evaluating",
                            listing.short_address,
                        )

                    # Cross-source dedup: skip if another scraper already found same address
                    addr_key = self._address_key(listing)
                    if addr_key in seen_addresses:
                        logger.debug(
                            "Dedup: %s already seen from %s — skipping %s",
                            listing.short_address,
                            seen_addresses[addr_key],
                            listing.source.value,
                        )
                        continue
                    seen_addresses[addr_key] = listing.source.value

                    result = evaluate_listing(listing, profile)
                    if result is None:
                        continue

                    # Waterway check — only if profile requests it
                    if profile.waterway_within_feet:
                        result = await self._enrich_waterway(result, profile.waterway_within_feet)
                        if result is None:
                            continue

                    results.append(result)
                    logger.info(
                        "[%s] MATCH %s — $%s/mo PITI — %s",
                        result.alert_priority.value.upper(),
                        listing.short_address,
                        f"{result.piti.total_monthly:,.0f}",
                        listing.url,
                    )
            except ScraperError as exc:
                logger.error("Scraper error: %s", exc)
            else:
                logger.info(
                    "[%s] %d listing(s) scraped for profile '%s'",
                    scraper.__class__.__name__, scraper_count, profile.name,
                )
            finally:
                await scraper.close()

        return results

    async def _enrich_waterway(
        self, result: MatchResult, radius_feet: int
    ) -> Optional[MatchResult]:
        """
        Geocode the listing address then check Overpass for nearby waterways.
        Returns None if waterway_within_feet is set and no waterway is found.
        Attaches WaterwayDetails to the result regardless (found=True/False).
        """
        listing = result.listing
        coords = await geocode_address(
            listing.address, listing.city, listing.state, listing.zip_code
        )
        if coords is None:
            logger.debug(
                "Could not geocode %s — excluding from waterway-filtered results",
                listing.short_address,
            )
            return None

        lat, lon = coords
        wresult: WaterwayResult = await check_waterway(lat, lon, radius_feet)

        waterway = WaterwayDetails(
            found=wresult.found,
            name=wresult.name,
            waterway_type=wresult.waterway_type,
            within_feet=radius_feet,
        )

        if not wresult.found:
            logger.debug(
                "No waterway within %dft of %s — excluded",
                radius_feet, listing.short_address,
            )
            return None

        logger.info(
            "💧 Waterway '%s' (%s) within %dft of %s",
            wresult.name, wresult.waterway_type, radius_feet, listing.short_address,
        )
        return result.model_copy(update={"waterway": waterway})

    async def _fire_alerts(self, results: list[MatchResult]) -> None:
        """
        Send immediate SMS+email for CRITICAL listings;
        send a batch digest for everything else.
        """
        critical_high = [
            r for r in results
            if r.alert_priority in (AlertPriority.CRITICAL, AlertPriority.HIGH)
        ]

        for result in critical_high:
            await self._sms.send(result)
            await self._email.send(result)

        # Always send a digest email for all matches
        await self._email.send_batch(results)

        # SMS batch summary if there are non-critical results
        remaining = [r for r in results if r not in critical_high]
        if remaining:
            await self._sms.send_batch(remaining)
