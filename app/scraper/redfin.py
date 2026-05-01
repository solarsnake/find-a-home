"""
Redfin scraper — Playwright + DOM parsing.

Redfin now renders listings server-side via their 'rift' streaming protocol
rather than a JSON API. The listing data is in the DOM when the page loads.

Strategy:
  1. Navigate to the zip+filter URL with Playwright (handles cookies/JS).
  2. Wait for listing cards to appear in the DOM.
  3. Parse each .HomeCardContainer card for price, beds, baths, address, URL.
  4. Extract property ID from the listing URL slug (/home/{id}).
  5. For each new listing, call the detail API via fetch() from within the
     browser context to get description + HOA fee.
  6. Yield RawListing objects.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime
from typing import AsyncIterator, Optional

from app.config import settings
from app.models import DataSource, RawListing, SearchProfile
from app.scraper.base import BaseScraper, ScraperError

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    from playwright_stealth import stealth_async
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE = "https://www.redfin.com"

# Aria-label: "Property at 123 Main St, Escondido, CA 92025, 4 beds, 2 baths"
_PROP_ID_RE = re.compile(r"/home/(\d+)")
_ARIA_RE = re.compile(
    r"Property at (.+?),\s*([^,]+),\s*([A-Z]{2})\s*(\d{5})(?:.*?(\d+)\s*beds?)?(?:.*?(\d+\.?\d*)\s*baths?)?",
    re.IGNORECASE,
)


def _strip_prefix(text: str) -> str:
    return text.removeprefix("{}&&").strip()


def _parse_price_str(text: str) -> float:
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    return float(cleaned) if cleaned else 0.0


def _parse_stats(stats_text: str) -> tuple[int, float, Optional[int]]:
    """Parse '4 beds\n3 baths\n2,100 sq ft' → (beds, baths, sqft)."""
    beds = baths = 0
    sqft = None
    beds_m = re.search(r"(\d+)\s*bed", stats_text, re.I)
    baths_m = re.search(r"(\d+\.?\d*)\s*bath", stats_text, re.I)
    sqft_m = re.search(r"([\d,]+)\s*sq", stats_text, re.I)
    if beds_m:
        beds = int(beds_m.group(1))
    if baths_m:
        baths = float(baths_m.group(1))
    if sqft_m:
        sqft = int(sqft_m.group(1).replace(",", ""))
    return beds, baths, sqft


def _search_url(zip_code: str, min_beds: int, min_baths: float, page: int = 1) -> str:
    baths_int = int(min_baths)
    base = (
        f"{BASE}/zipcode/{zip_code}/filter/"
        f"min-beds={min_beds},min-baths={baths_int},property-type=house"
    )
    return base if page == 1 else f"{base}/page-{page}"


async def _has_next_page(page: "Page") -> bool:
    """Return True if Redfin shows a next-page control."""
    selectors = [
        "button[data-rf-test-id='pagination-next-button']:not([disabled])",
        "a[aria-label='Next Page']",
        ".PaginationButton--next:not(.disabled)",
        "[class*='nextPage']:not([disabled])",
    ]
    for sel in selectors:
        el = await page.query_selector(sel)
        if el:
            return True
    return False


class RedfinScraper(BaseScraper):
    source_name = "redfin"

    def __init__(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "playwright and playwright-stealth are required.\n"
                "Run: pip install playwright playwright-stealth && playwright install chromium"
            )
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._playwright = None

    async def _init_browser(self) -> None:
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.playwright_headless,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )

    async def _new_page(self) -> Page:
        await self._init_browser()
        page = await self._context.new_page()
        await stealth_async(page)
        return page

    async def _random_delay(self) -> None:
        await asyncio.sleep(random.uniform(settings.scrape_delay_min, settings.scrape_delay_max))

    async def _parse_cards(self, page: Page) -> list[dict]:
        """Extract raw listing data from all .HomeCardContainer DOM nodes."""
        results: list[dict] = []
        cards = await page.query_selector_all(".HomeCardContainer")

        for card in cards:
            try:
                # Price
                price_el = await card.query_selector(
                    "[data-rf-test-id='abp-price'], .bp-Homecard__Price--value, [class*='Price']"
                )
                price_text = await price_el.inner_text() if price_el else ""
                price = _parse_price_str(price_text)
                if not price:
                    continue

                # Beds / baths / sqft
                stats_el = await card.query_selector(
                    "[data-rf-test-id='abp-homeInfo'], .bp-Homecard__Stats, [class*='Stats']"
                )
                stats_text = await stats_el.inner_text() if stats_el else ""
                beds, baths, sqft = _parse_stats(stats_text)

                # Address from aria-label: "Property at {addr}, {city}, {state} {zip}, ..."
                inner = await card.query_selector("[aria-label]")
                aria = (await inner.get_attribute("aria-label") if inner else "") or ""
                m = _ARIA_RE.match(aria)
                street = city = state = zip_code = ""
                if m:
                    street, city, state, zip_code = m.group(1), m.group(2), m.group(3), m.group(4)
                    if not beds and m.group(5):
                        beds = int(m.group(5))
                    if not baths and m.group(6):
                        baths = float(m.group(6))

                # Listing URL + property ID
                link_el = await card.query_selector("a[href*='/home/']")
                href = (await link_el.get_attribute("href") if link_el else "") or ""
                full_url = f"{BASE}{href}" if href and not href.startswith("http") else href
                pid_m = _PROP_ID_RE.search(href)
                property_id = pid_m.group(1) if pid_m else ""

                if not street or not property_id:
                    continue

                results.append(
                    dict(
                        property_id=property_id,
                        url=full_url,
                        price=price,
                        beds=beds,
                        baths=baths,
                        sqft=sqft,
                        street=street,
                        city=city,
                        state=state,
                        zip_code=zip_code,
                    )
                )
            except Exception:
                continue

        return results

    async def _fetch_listing_detail(self, detail_url: str) -> dict:
        """
        Navigate to the listing detail page in a FRESH browser context to avoid
        CloudFront rate-limiting that triggers after the search page load.
        """
        result: dict = {"description": "", "hoa_monthly": None}
        fresh_context = await self._browser.new_context(
            viewport={"width": random.randint(1280, 1920), "height": random.randint(800, 1080)},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        detail_page = await fresh_context.new_page()
        await stealth_async(detail_page)
        try:
            await detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await detail_page.wait_for_selector(".remarks", timeout=10_000)
            except Exception:
                pass

            # Description
            desc_el = await detail_page.query_selector(".remarks")
            if desc_el:
                result["description"] = (await desc_el.inner_text()).strip()

            # HOA — table row: "HOA Dues | $150/mo" or "—" if undisclosed
            hoa_el = await detail_page.query_selector('text="HOA Dues"')
            if hoa_el:
                parent = await hoa_el.evaluate_handle("el => el.parentElement")
                val_el = await parent.query_selector(".table-value")
                if val_el:
                    hoa_text = (await val_el.inner_text()).strip()
                    m = re.search(r"[\$]?([\d,]+)", hoa_text)
                    if m:
                        result["hoa_monthly"] = float(m.group(1).replace(",", ""))
        except Exception:
            pass
        finally:
            await detail_page.close()
            await fresh_context.close()
        return result

    async def search(self, profile: SearchProfile) -> AsyncIterator[RawListing]:
        seen_in_run: set[str] = set()
        max_pages = 10  # safety cap — Redfin rarely has more than 3-4 pages per zip

        for zip_code in profile.zip_codes:
            raw_listings: list[dict] = []

            for page_num in range(1, max_pages + 1):
                url = _search_url(zip_code, profile.min_bedrooms, profile.min_bathrooms, page_num)
                page = await self._new_page()
                has_more = False

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(6)

                    cards = await self._parse_cards(page)
                    raw_listings.extend(cards)

                    if page_num < max_pages:
                        has_more = await _has_next_page(page)

                except Exception as exc:
                    await page.close()
                    raise ScraperError("redfin", str(exc), zip_code=zip_code) from exc
                finally:
                    # Close search page BEFORE opening detail pages —
                    # Redfin/CloudFront blocks concurrent pages from the same context.
                    await page.close()

                if not has_more:
                    break

                await self._random_delay()

            for item in raw_listings:
                uid = f"redfin_{item['property_id']}"
                if uid in seen_in_run:
                    continue
                seen_in_run.add(uid)

                try:
                    # Fetch detail (description + HOA) one page at a time
                    await self._random_delay()
                    detail = await self._fetch_listing_detail(item["url"])

                    yield RawListing(
                        listing_id=uid,
                        source=DataSource.REDFIN,
                        url=item["url"],
                        address=item["street"],
                        city=item["city"],
                        state=item["state"],
                        zip_code=item["zip_code"] or zip_code,
                        price=item["price"],
                        bedrooms=item["beds"],
                        bathrooms=item["baths"],
                        sqft=item["sqft"],
                        hoa_monthly=detail.get("hoa_monthly"),
                        description=detail.get("description", ""),
                        scraped_at=datetime.utcnow(),
                    )
                except Exception:
                    # Skip individual listing detail failures gracefully
                    continue

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
