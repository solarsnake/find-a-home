"""
Zillow scraper using Playwright + playwright-stealth.

Strategy:
  1. Navigate to Zillow's search-results page for a zip code.
  2. Extract the Next.js __NEXT_DATA__ JSON blob (most reliable structured data).
  3. Fall back to CSS-selector HTML parsing if the JSON path changes.
  4. Visit each *new* listing's detail page to collect HOA fee + description.
  5. Yield RawListing objects.

Zillow actively mitigates scraping.  playwright-stealth patches the
`navigator.webdriver` flag and a number of other bot-detection signals.
Even so, expect occasional CAPTCHAs — the scraper detects them and skips
the affected zip code rather than stalling indefinitely.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import urllib.parse
from typing import AsyncIterator, Optional
from datetime import datetime

from app.models import DataSource, RawListing, SearchProfile
from app.scraper.base import BaseScraper, ScraperError
from app.config import settings

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    from playwright_stealth import stealth_async
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


# ── URL helpers ───────────────────────────────────────────────────────────────

def _search_url(zip_code: str, min_beds: int, min_baths: float) -> str:
    """
    Build a Zillow search URL with bedroom/bathroom filters encoded in
    searchQueryState (the format Zillow's own frontend uses).
    """
    state = {
        "pagination": {},
        "isMapVisible": False,
        "filterState": {
            "beds": {"min": min_beds},
            "baths": {"min": int(min_baths)},
            "sort": {"value": "globalrelevanceex"},
            "ah": {"value": True},  # for-sale only
        },
        "isListVisible": True,
    }
    encoded = urllib.parse.quote(json.dumps(state, separators=(",", ":")))
    return f"https://www.zillow.com/homes/for_sale/{zip_code}_rb/?searchQueryState={encoded}"


def _detail_url(path: str) -> str:
    if path.startswith("http"):
        return path
    return f"https://www.zillow.com{path}"


# ── __NEXT_DATA__ parsers ─────────────────────────────────────────────────────

def _extract_next_data(page_content: str) -> Optional[dict]:
    """Pull the __NEXT_DATA__ JSON script tag from raw HTML."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_content, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _parse_search_results(next_data: dict) -> list[dict]:
    """Navigate the Zillow search-page __NEXT_DATA__ structure to listing cards."""
    try:
        return (
            next_data["props"]["pageProps"]["searchPageState"]
            ["cat1"]["searchResults"]["listResults"]
        )
    except (KeyError, TypeError):
        return []


def _parse_price(raw: str | int | float | None) -> float:
    """Convert Zillow price strings like '$850,000' to float."""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    return float(cleaned) if cleaned else 0.0


def _parse_detail_page(next_data: dict, zpid: str) -> dict:
    """
    Extract description and HOA from a listing detail page's __NEXT_DATA__.
    Zillow stores detail data in a 'gdpClientCache' key as a nested JSON string.
    """
    result: dict = {"description": "", "hoa_monthly": None}
    try:
        cache_raw = next_data["props"]["pageProps"]["gdpClientCache"]
        cache: dict = json.loads(cache_raw) if isinstance(cache_raw, str) else cache_raw
        # The cache is keyed by a string containing the zpid
        for key, val in cache.items():
            prop = val.get("property", {})
            if prop:
                result["description"] = prop.get("description", "")
                # monthlyHoaFee may be 0 (no HOA) or a positive number
                raw_hoa = prop.get("monthlyHoaFee")
                if raw_hoa is not None:
                    result["hoa_monthly"] = float(raw_hoa)
                break
    except (KeyError, TypeError, json.JSONDecodeError):
        pass
    return result


# ── Fallback: HTML parsing ────────────────────────────────────────────────────

async def _parse_listing_cards_from_dom(page: "Page") -> list[dict]:
    """
    CSS-selector fallback for when __NEXT_DATA__ is missing or has changed
    its structure.  Returns a list of partial listing dicts.
    """
    cards = await page.query_selector_all('[data-test="property-card"]')
    results = []
    for card in cards:
        try:
            price_el = await card.query_selector('[data-test="property-card-price"]')
            addr_el = await card.query_selector('[data-test="property-card-addr"]')
            link_el = await card.query_selector('a[data-test="property-card-link"]')

            price_text = await price_el.inner_text() if price_el else ""
            address_text = await addr_el.inner_text() if addr_el else ""
            detail_path = await link_el.get_attribute("href") if link_el else ""

            # Beds / baths sit in a list; grab all li text
            li_els = await card.query_selector_all("ul li")
            facts = [await li.inner_text() for li in li_els]
            beds = baths = 0
            for fact in facts:
                if "bd" in fact or "bed" in fact.lower():
                    m = re.search(r"(\d+)", fact)
                    if m:
                        beds = int(m.group(1))
                if "ba" in fact or "bath" in fact.lower():
                    m = re.search(r"(\d+\.?\d*)", fact)
                    if m:
                        baths = float(m.group(1))

            if price_text and address_text:
                results.append({
                    "price": _parse_price(price_text),
                    "address": address_text,
                    "beds": beds,
                    "baths": baths,
                    "detailUrl": detail_path,
                    "zpid": re.search(r"/(\d+)_zpid", detail_path or "").group(1)
                    if detail_path else "",
                })
        except Exception:
            continue
    return results


# ── Main scraper class ────────────────────────────────────────────────────────

class ZillowScraper(BaseScraper):
    source_name = "zillow"

    def __init__(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise ImportError(
                "playwright and playwright-stealth are required.\n"
                "Run: pip install playwright playwright-stealth && playwright install chromium"
            )
        self._browser: Optional["Browser"] = None
        self._context: Optional["BrowserContext"] = None
        self._playwright = None

    async def _init_browser(self) -> None:
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=settings.playwright_headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
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

    async def _new_page(self) -> "Page":
        await self._init_browser()
        page = await self._context.new_page()
        await stealth_async(page)
        return page

    async def _random_delay(self) -> None:
        delay = random.uniform(settings.scrape_delay_min, settings.scrape_delay_max)
        await asyncio.sleep(delay)

    async def _is_captcha(self, page: "Page") -> bool:
        content = await page.content()
        return "captcha" in content.lower() or "are you a robot" in content.lower()

    async def _fetch_listing_detail(
        self, page: "Page", detail_url: str, zpid: str
    ) -> dict:
        """Visit a listing detail page and extract description + HOA."""
        try:
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
            await self._random_delay()

            if await self._is_captcha(page):
                return {"description": "", "hoa_monthly": None}

            content = await page.content()
            nd = _extract_next_data(content)
            if nd:
                return _parse_detail_page(nd, zpid)

            # Fallback: look for description text in DOM
            desc_el = await page.query_selector('[data-testid="listing-description"]')
            description = await desc_el.inner_text() if desc_el else ""
            return {"description": description, "hoa_monthly": None}

        except Exception:
            return {"description": "", "hoa_monthly": None}

    async def search(self, profile: SearchProfile) -> AsyncIterator[RawListing]:
        """Yield listings for every zip code in the profile."""
        seen_in_run: set[str] = set()

        for zip_code in profile.zip_codes:
            url = _search_url(zip_code, profile.min_bedrooms, profile.min_bathrooms)
            page = await self._new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await self._random_delay()

                if await self._is_captcha(page):
                    await page.close()
                    continue

                content = await page.content()
                next_data = _extract_next_data(content)
                raw_listings: list[dict] = []

                if next_data:
                    raw_listings = _parse_search_results(next_data)
                    if not raw_listings:
                        # __NEXT_DATA__ loaded but path changed — use DOM fallback
                        raw_listings = await _parse_listing_cards_from_dom(page)
                else:
                    raw_listings = await _parse_listing_cards_from_dom(page)

                for item in raw_listings:
                    zpid = str(item.get("zpid") or item.get("id", ""))
                    if not zpid or zpid in seen_in_run:
                        continue
                    seen_in_run.add(zpid)

                    price = _parse_price(
                        item.get("price")
                        or item.get("hdpData", {}).get("homeInfo", {}).get("price")
                    )
                    if not price:
                        continue

                    beds = int(item.get("beds") or item.get("bedrooms") or 0)
                    baths = float(item.get("baths") or item.get("bathrooms") or 0)
                    detail_path = item.get("detailUrl") or item.get("url") or ""
                    full_detail_url = _detail_url(detail_path)

                    # Address parsing (Zillow returns a combined string or structured)
                    addr_raw = item.get("address") or item.get("streetAddress") or ""
                    city = item.get("city") or ""
                    state = item.get("state") or ""
                    if not city and "," in addr_raw:
                        # "123 Main St, Escondido, CA 92025"
                        parts = [p.strip() for p in addr_raw.split(",")]
                        city = parts[1] if len(parts) > 1 else ""
                        if len(parts) > 2:
                            sc = parts[2].strip().split()
                            state = sc[0] if sc else ""

                    # HOA from search results (sometimes present)
                    hoa_search = item.get("hdpData", {}).get("homeInfo", {}).get("hoaFee")
                    sqft = item.get("area") or item.get("livingArea") or item.get(
                        "hdpData", {}
                    ).get("homeInfo", {}).get("livingArea")

                    # Fetch full detail for HOA + description (only new listings)
                    detail = await self._fetch_listing_detail(page, full_detail_url, zpid)
                    hoa = detail.get("hoa_monthly") if detail["hoa_monthly"] is not None else (
                        float(hoa_search) if hoa_search is not None else None
                    )

                    yield RawListing(
                        listing_id=f"zillow_{zpid}",
                        source=DataSource.ZILLOW,
                        url=full_detail_url,
                        address=addr_raw.split(",")[0].strip() if "," in addr_raw else addr_raw,
                        city=city,
                        state=state,
                        zip_code=zip_code,
                        price=price,
                        bedrooms=beds,
                        bathrooms=baths,
                        sqft=int(sqft) if sqft else None,
                        hoa_monthly=hoa,
                        description=detail.get("description", ""),
                        scraped_at=datetime.utcnow(),
                    )

                    await self._random_delay()

            except Exception as exc:
                raise ScraperError("zillow", str(exc), zip_code=zip_code) from exc
            finally:
                await page.close()

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
