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
from typing import Optional

from app.alerts.email_alert import EmailAlert
from app.alerts.sms import SMSAlert
from app.config import Settings, load_profiles
from app.filters.listing_filter import evaluate_listing
from app.models import AlertPriority, DataSource, MatchResult, SearchProfile
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

        # Fire alerts
        if all_results and not self._dry_run:
            await self._fire_alerts(all_results)

        # Mark everything seen AFTER alerts so a failed alert doesn't silence a listing
        if not self._dry_run:
            self._seen.mark_seen_bulk([r.listing.listing_id for r in all_results])

        return all_results

    async def _run_profile(self, profile: SearchProfile) -> list[MatchResult]:
        sources = self._sources_override or profile.sources
        results: list[MatchResult] = []

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
            try:
                async for listing in scraper.search(profile):
                    # Skip listings we've already processed
                    if not self._dry_run and not self._seen.is_new(listing.listing_id):
                        continue

                    result = evaluate_listing(listing, profile)
                    if result is not None:
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
            finally:
                await scraper.close()

        return results

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
