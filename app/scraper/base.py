"""Abstract scraper interface — all scrapers implement this contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.models import RawListing, SearchProfile


class BaseScraper(ABC):
    """
    One scraper = one data source (Zillow, Redfin, …).

    Adding a new data source (e.g. Realtor.com, an MLS feed) is just adding
    a new subclass here and registering it in `app/scraper/__init__.py`.
    """

    source_name: str  # must be set by subclasses

    @abstractmethod
    async def search(self, profile: SearchProfile) -> AsyncIterator[RawListing]:
        """
        Yield RawListing objects matching the profile's zip codes and
        property filters (bedrooms, bathrooms).

        HOA filtering and PITI math happen downstream — yield *all* listings
        that match the basic property type/size criteria so the filter layer
        has full visibility.

        Implementations should:
          - Respect rate-limiting delays from Settings.
          - Deduplicate within a single run (same listing from multiple pages).
          - NOT deduplicate across runs — that is storage's responsibility.
          - Raise ScraperError on unrecoverable failures.
        """
        ...  # pragma: no cover

    async def close(self) -> None:
        """Clean up browser/session resources.  Override if needed."""
        pass


class ScraperError(Exception):
    """Raised when a scraper encounters an unrecoverable error."""

    def __init__(self, source: str, message: str, *, zip_code: str = "") -> None:
        self.source = source
        self.zip_code = zip_code
        super().__init__(f"[{source}] {message}" + (f" (zip: {zip_code})" if zip_code else ""))
