"""
Homes.com scraper — Playwright + DOM parsing.

Homes.com (a CoStar property) injects listing data via Next.js __NEXT_DATA__.
We parse that JSON blob rather than scraping the DOM where possible, and fall
back to CSS selectors when the JSON path changes.

Search URL format:
  https://www.homes.com/homes-for-sale/?zip={zip}&beds={beds}&baths={baths}
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime
from typing import AsyncIterator, Optional

import logging

from app.config import settings
from app.models import DataSource, RawListing, SearchProfile
from app.scraper.base import BaseScraper, ScraperError

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    from playwright_stealth import stealth_async
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE = "https://www.homes.com"

# Listing ID from Homes.com URL: /for-sale/some-address-1234567890/
_LISTING_ID_RE = re.compile(r"/for-sale/[^/]+-(\d+)/?$")


def _build_search_url(zip_code: str, min_beds: int, min_baths: float) -> str:
    baths_int = int(min_baths)
    return (
        f"{BASE}/homes-for-sale/"
        f"?zip={zip_code}&beds={min_beds}&baths={baths_int}"
        f"&property_type=single_family_house,multi_family,townhome"
    )


def _extract_next_data(content: str) -> Optional[dict]:
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _listings_from_next_data(data: dict) -> list[dict]:
    """Navigate common Homes.com __NEXT_DATA__ paths for listing arrays."""
    paths = [
        ["props", "pageProps", "searchResults", "listings"],
        ["props", "pageProps", "listings"],
        ["props", "pageProps", "initialData", "listings"],
        ["props", "pageProps", "data", "results"],
    ]
    for path in paths:
        node = data
        for key in path:
            if isinstance(node, dict):
                node = node.get(key)
            else:
                node = None
                break
        if isinstance(node, list) and node:
            return node
    return []


def _parse_price(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    cleaned = re.sub(r"[^\d.]", "", str(val))
    return float(cleaned) if cleaned else 0.0


class HomesScraper(BaseScraper):
    source_name = "homes"

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
        )

    async def _new_page(self) -> Page:
        await self._init_browser()
        page = await self._context.new_page()
        await stealth_async(page)
        return page

    async def _random_delay(self) -> None:
        await asyncio.sleep(random.uniform(settings.scrape_delay_min, settings.scrape_delay_max))

    async def _parse_cards_from_dom(self, page: Page) -> list[dict]:
        """
        CSS-selector fallback for when __NEXT_DATA__ path has changed.
        Homes.com listing cards use data-testid or class-based selectors.
        """
        results = []
        # Common Homes.com card selectors (may need updating if site redesigns)
        card_selectors = [
            "[data-testid='property-card']",
            ".property-card",
            ".srp-card",
            "[class*='PropertyCard']",
            "[class*='ListingCard']",
        ]
        cards = []
        for sel in card_selectors:
            cards = await page.query_selector_all(sel)
            if cards:
                break

        for card in cards:
            try:
                # Price
                price_el = await card.query_selector(
                    "[data-testid='price'], [class*='price'], [class*='Price']"
                )
                price_text = await price_el.inner_text() if price_el else ""
                price = _parse_price(price_text)
                if not price:
                    continue

                # Beds/baths from text
                stats_el = await card.query_selector(
                    "[data-testid='property-meta'], [class*='meta'], [class*='Stats']"
                )
                stats_text = await stats_el.inner_text() if stats_el else ""
                beds_m = re.search(r"(\d+)\s*bed", stats_text, re.I)
                baths_m = re.search(r"(\d+\.?\d*)\s*bath", stats_text, re.I)
                beds = int(beds_m.group(1)) if beds_m else 0
                baths = float(baths_m.group(1)) if baths_m else 0.0

                # Address
                addr_el = await card.query_selector(
                    "[data-testid='address'], [class*='address'], [class*='Address']"
                )
                addr_text = await addr_el.inner_text() if addr_el else ""

                # Link
                link_el = await card.query_selector("a[href*='/for-sale/']")
                href = (await link_el.get_attribute("href") if link_el else "") or ""
                full_url = f"{BASE}{href}" if href and not href.startswith("http") else href

                m = _LISTING_ID_RE.search(href)
                lid = m.group(1) if m else re.sub(r"\W", "_", href[-20:])

                if not addr_text or not lid:
                    continue

                # Parse city/state from address
                parts = [p.strip() for p in addr_text.split(",")]
                street = parts[0] if parts else addr_text
                city = parts[1] if len(parts) > 1 else ""
                state_zip = parts[2].strip().split() if len(parts) > 2 else []
                state = state_zip[0] if state_zip else ""
                zip_val = state_zip[1] if len(state_zip) > 1 else ""

                results.append(dict(
                    listing_id=f"homes_{lid}",
                    url=full_url,
                    price=price,
                    beds=beds,
                    baths=baths,
                    street=street,
                    city=city,
                    state=state,
                    zip_code=zip_val,
                ))
            except Exception:
                continue
        return results

    async def search(self, profile: SearchProfile) -> AsyncIterator[RawListing]:
        seen_in_run: set[str] = set()

        for zip_code in profile.zip_codes:
            url = _build_search_url(zip_code, profile.min_bedrooms, profile.min_bathrooms)
            page = await self._new_page()
            raw_listings: list[dict] = []

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(6)

                content = await page.content()

                # Detect bot-block response (Cloudflare / CoStar WAF)
                if len(content) < 2000 and "access denied" in content.lower():
                    logger.warning(
                        "Homes.com blocked request for zip %s (Access Denied). "
                        "This scraper requires a residential IP — skipping.",
                        zip_code,
                    )
                    continue

                # Try __NEXT_DATA__ first
                nd = _extract_next_data(content)
                if nd:
                    json_listings = _listings_from_next_data(nd)
                    for item in json_listings:
                        pid = str(
                            item.get("listingId") or item.get("id") or item.get("propertyId") or ""
                        )
                        if not pid:
                            continue
                        uid = f"homes_{pid}"
                        price = _parse_price(
                            item.get("listPrice") or item.get("price") or
                            item.get("priceInfo", {}).get("listPrice")
                        )
                        if not price:
                            continue
                        addr = item.get("address") or item.get("location") or {}
                        raw_listings.append(dict(
                            listing_id=uid,
                            url=f"{BASE}{item.get('url', '')}" if item.get("url", "").startswith("/") else item.get("url", ""),
                            price=price,
                            beds=int(item.get("beds") or item.get("bedrooms") or 0),
                            baths=float(item.get("baths") or item.get("bathrooms") or 0),
                            sqft=item.get("sqft") or item.get("livingArea"),
                            street=addr.get("streetAddress") or addr.get("street") or item.get("streetLine", ""),
                            city=addr.get("city") or "",
                            state=addr.get("state") or addr.get("stateCode") or "",
                            zip_code=addr.get("postalCode") or zip_code,
                            hoa_monthly=item.get("hoaFee") or item.get("hoa"),
                            description=item.get("description") or item.get("remarks") or "",
                            property_type=item.get("propertyType") or item.get("type") or "",
                        ))

                # Fall back to DOM parsing if __NEXT_DATA__ gave nothing
                if not raw_listings:
                    dom_results = await self._parse_cards_from_dom(page)
                    raw_listings = dom_results

            except Exception as exc:
                raise ScraperError("homes", str(exc), zip_code=zip_code) from exc
            finally:
                await page.close()

            for item in raw_listings:
                uid = item.get("listing_id", f"homes_{zip_code}_{item.get('price', 0)}")
                if uid in seen_in_run:
                    continue
                seen_in_run.add(uid)

                try:
                    yield RawListing(
                        listing_id=uid,
                        source=DataSource.HOMES,
                        url=item.get("url", ""),
                        address=item.get("street", ""),
                        city=item.get("city", ""),
                        state=item.get("state", ""),
                        zip_code=item.get("zip_code") or zip_code,
                        price=float(item["price"]),
                        bedrooms=int(item.get("beds", 0)),
                        bathrooms=float(item.get("baths", 0)),
                        sqft=int(item["sqft"]) if item.get("sqft") else None,
                        hoa_monthly=float(item["hoa_monthly"]) if item.get("hoa_monthly") else None,
                        description=str(item.get("description") or ""),
                        property_type=str(item.get("property_type") or "") or None,
                        scraped_at=datetime.utcnow(),
                    )
                except Exception:
                    continue

                await self._random_delay()

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
