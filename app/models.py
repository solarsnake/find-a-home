"""
Core data models for find-a-home.

Design intent: every model here is JSON-serialisable via .model_dump().
This means the same objects work unchanged in:
  - the CLI (printed via rich)
  - the FastAPI layer (serialised to JSON responses)
  - a future iOS push-notification payload

SearchProfile is the primary user-facing entity.  In the future web/iOS app
it becomes the body of a POST /api/v1/profiles request from a "New Search"
form page.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field, field_validator


# ── Region / tax helpers ─────────────────────────────────────────────────────

class TaxRegion(str, Enum):
    CALIFORNIA = "california"
    GEORGIA = "georgia"
    SOUTH_CAROLINA = "south_carolina"
    NORTH_CAROLINA = "north_carolina"
    TEXAS = "texas"
    FLORIDA = "florida"
    COLORADO = "colorado"
    WASHINGTON = "washington"
    OREGON = "oregon"
    ARIZONA = "arizona"


# Effective annual property-tax rates by region (as a decimal fraction of AV)
# Add new states here — no other code changes needed.
TAX_RATES: dict[TaxRegion, float] = {
    TaxRegion.CALIFORNIA: 0.012,       # ~1.2 % (Escondido / Ramona / VC)
    TaxRegion.GEORGIA: 0.006,          # ~0.6 % (Hart County)
    TaxRegion.SOUTH_CAROLINA: 0.006,   # ~0.6 % (Oconee / Anderson Counties)
    TaxRegion.NORTH_CAROLINA: 0.011,   # ~1.1 % (Durham County combined rate)
    TaxRegion.TEXAS: 0.018,            # ~1.8 % (varies widely by county)
    TaxRegion.FLORIDA: 0.009,          # ~0.9 %
    TaxRegion.COLORADO: 0.005,         # ~0.5 %
    TaxRegion.WASHINGTON: 0.010,       # ~1.0 %
    TaxRegion.OREGON: 0.009,           # ~0.9 %
    TaxRegion.ARIZONA: 0.007,          # ~0.7 %
}


class DataSource(str, Enum):
    ZILLOW = "zillow"
    REDFIN = "redfin"
    REALTOR = "realtor"
    HOMES = "homes"


class PropertyType(str, Enum):
    SINGLE_FAMILY = "single_family"
    MULTI_FAMILY = "multi_family"
    CONDO = "condo"
    TOWNHOUSE = "townhouse"
    LAND = "land"
    ANY = "any"


class AlertPriority(str, Enum):
    NORMAL = "normal"       # Meets all criteria
    HIGH = "high"           # Assumable loan detected
    CRITICAL = "critical"   # Assumable + under PITI budget


# ── Search profile ────────────────────────────────────────────────────────────

class SearchProfile(BaseModel):
    """
    All criteria for a single search.

    JSON-safe so it can be POSTed from a web form or stored in a mobile app's
    user profile.  Multiple profiles are supported — run them all with
    `python main.py run` or target one with `--profile "Name"`.
    """

    name: str
    enabled: bool = True

    # ── Location ──────────────────────────────────────────────────────────────
    zip_codes: list[str]
    tax_region: TaxRegion

    # ── Property filters ──────────────────────────────────────────────────────
    min_bedrooms: int = Field(default=4, ge=1)
    max_bedrooms: Optional[int] = None
    min_bathrooms: float = Field(default=2.0, ge=1.0)
    min_sqft: Optional[int] = None
    max_sqft: Optional[int] = None
    property_types: list[PropertyType] = Field(
        default_factory=lambda: [PropertyType.SINGLE_FAMILY]
    )

    # ── HOA ───────────────────────────────────────────────────────────────────
    # max_hoa_monthly = 0  → strict no-HOA only
    # max_hoa_monthly = 50 → allow up to $50/mo (default)
    # max_hoa_monthly = None is not allowed — use a large number if you don't care
    max_hoa_monthly: float = Field(default=50.0, ge=0)

    # ── Financial targets ─────────────────────────────────────────────────────
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    max_monthly_piti: float = Field(default=4_500.0, gt=0)
    down_payment: float = Field(default=100_000.0, ge=0)
    interest_rate: float = Field(default=0.065, gt=0, lt=1)
    monthly_insurance: float = Field(default=200.0, ge=0)

    # ── Special deal flags ────────────────────────────────────────────────────
    assumable_only: bool = False
    requires_solar: bool = False
    # waterway_within_feet: only include listings with a stream/river within this
    # distance of the property address (via OSM Overpass). None = no filter.
    waterway_within_feet: Optional[int] = None

    # ── Data sources ──────────────────────────────────────────────────────────
    sources: list[DataSource] = Field(
        default_factory=lambda: [DataSource.REDFIN, DataSource.REALTOR]
    )

    @field_validator("zip_codes")
    @classmethod
    def zip_codes_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("At least one zip code is required")
        return v


# ── Solar detection ───────────────────────────────────────────────────────────

SOLAR_KEYWORDS = [
    "solar panel", "solar panels", "solar system", "solar energy", "solar power",
    "solar owned", "owned solar", "solar paid", "paid-off solar", "paid off solar",
    "solar array", "photovoltaic", "pv system",
    "net metering", "solar lease", "solar ppa", "rooftop solar",
    "solar installed", "solar included",
]

# Deal-signal keywords — not filters, just surfaced as negotiability flags
DEAL_SIGNAL_KEYWORDS: dict[str, str] = {
    "as-is":             "As-Is",
    "as is":             "As-Is",
    "sold as is":        "As-Is",
    "estate sale":       "Estate Sale",
    "estate owned":      "Estate Sale",
    "motivated seller":  "Motivated Seller",
    "must sell":         "Motivated Seller",
    "price reduced":     "Price Reduced",
    "price improvement": "Price Reduced",
    "reduced price":     "Price Reduced",
    "fixer":             "Fixer-Upper",
    "fixer upper":       "Fixer-Upper",
    "fixer-upper":       "Fixer-Upper",
    "tlc":               "Fixer-Upper",
    "needs work":        "Fixer-Upper",
    "handyman":          "Fixer-Upper",
    "short sale":        "Short Sale",
    "reo":               "REO/Bank-Owned",
    "bank owned":        "REO/Bank-Owned",
    "foreclosure":       "REO/Bank-Owned",
}

STALE_LISTING_DAYS = 45   # DOM threshold for "stale" flag


# ── Raw listing (scraper output) ──────────────────────────────────────────────

class RawListing(BaseModel):
    """
    Data as extracted from a scraper — no processing applied yet.
    Stored in seen_listings.json to deduplicate across runs.
    """

    listing_id: str                         # Source-specific ID (zpid, redfin ID …)
    source: DataSource
    url: str                                # Full canonical listing URL
    address: str
    city: str
    state: str
    zip_code: str
    price: float
    bedrooms: int
    bathrooms: float
    sqft: Optional[int] = None
    lot_sqft: Optional[int] = None
    hoa_monthly: Optional[float] = None     # None = not reported; 0 = no HOA
    description: str = ""
    property_type: Optional[str] = None
    days_on_market: Optional[int] = None
    year_built: Optional[int] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def short_address(self) -> str:
        return f"{self.address}, {self.city}, {self.state} {self.zip_code}"

    @computed_field  # type: ignore[misc]
    @property
    def has_solar(self) -> bool:
        """Detect solar mention in description."""
        desc = self.description.lower()
        return any(kw in desc for kw in SOLAR_KEYWORDS)

    @computed_field  # type: ignore[misc]
    @property
    def deal_signals(self) -> list[str]:
        """Negotiability/deal flags detected in description (deduplicated labels)."""
        desc = self.description.lower()
        seen: set[str] = set()
        result: list[str] = []
        for kw, label in DEAL_SIGNAL_KEYWORDS.items():
            if kw in desc and label not in seen:
                seen.add(label)
                result.append(label)
        return result

    @computed_field  # type: ignore[misc]
    @property
    def is_stale(self) -> bool:
        """True if listing has been on market longer than STALE_LISTING_DAYS."""
        return self.days_on_market is not None and self.days_on_market >= STALE_LISTING_DAYS


# ── Financial breakdown ───────────────────────────────────────────────────────

class PITIBreakdown(BaseModel):
    """Monthly PITI components.  Immutable once computed."""

    loan_amount: float
    annual_rate: float
    principal_interest: float
    monthly_taxes: float
    monthly_insurance: float
    monthly_pmi: float = 0.0      # 0 when down payment ≥ 20 %
    total_monthly: float

    @property
    def formatted(self) -> str:
        parts = (
            f"P&I ${self.principal_interest:,.0f}  "
            f"+ Tax ${self.monthly_taxes:,.0f}  "
            f"+ Ins ${self.monthly_insurance:,.0f}"
        )
        if self.monthly_pmi:
            parts += f"  + PMI ${self.monthly_pmi:,.0f}"
        parts += f"  = ${self.total_monthly:,.0f}/mo"
        return parts


# ── Assumable-loan details ────────────────────────────────────────────────────

# Keywords searched (case-insensitive) in the listing description
ASSUMABLE_KEYWORDS = [
    "assumable",
    "assume the loan",
    "va assumption",
    "assumption of loan",
    "subject to existing",   # "subject to existing financing/mortgage/loan"
    "take over the loan",
]

# Regex patterns to extract loan balance / rate from description
_BALANCE_RE = re.compile(
    r"(?:loan\s+balance|outstanding\s+balance|remaining\s+balance|balance\s+of)"
    r"\s*(?:is|of|:)?\s*\$?([\d,]+(?:\.\d+)?)\s*[kK]?",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"(\d+\.?\d*)\s*%\s*(?:interest\s+rate|rate|fixed|VA)",
    re.IGNORECASE,
)
_DOLLAR_NEAR_ASSUME_RE = re.compile(
    r"assum\w*[^.]*?\$\s*([\d,]+(?:\.\d+)?)\s*[kK]?",
    re.IGNORECASE,
)


class AssumableDetails(BaseModel):
    is_assumable: bool = False
    assumable_rate: Optional[float] = None      # e.g. 0.025 for 2.5 %
    estimated_loan_balance: Optional[float] = None
    equity_gap: Optional[float] = None          # listing_price − loan_balance
    high_cash_required: bool = False            # equity_gap > $110 k
    matched_keywords: list[str] = Field(default_factory=list)

    @classmethod
    def from_description(cls, description: str, price: float) -> "AssumableDetails":
        desc_lower = description.lower()

        matched = [kw for kw in ASSUMABLE_KEYWORDS if kw in desc_lower]
        if not matched:
            return cls()

        # Try to parse loan balance
        balance: Optional[float] = None
        for pattern in (_BALANCE_RE, _DOLLAR_NEAR_ASSUME_RE):
            m = pattern.search(description)
            if m:
                raw = m.group(1).replace(",", "")
                val = float(raw)
                # Handle shorthand like "$450k"
                if val < 5_000:
                    val *= 1_000
                balance = val
                break

        # Try to parse assumable rate
        rate: Optional[float] = None
        m = _RATE_RE.search(description)
        if m:
            rate = float(m.group(1)) / 100

        equity_gap = (price - balance) if balance is not None else None
        high_cash = (equity_gap is not None and equity_gap > 110_000)

        return cls(
            is_assumable=True,
            assumable_rate=rate,
            estimated_loan_balance=balance,
            equity_gap=equity_gap,
            high_cash_required=high_cash,
            matched_keywords=matched,
        )


# ── Waterway details ──────────────────────────────────────────────────────────

class WaterwayDetails(BaseModel):
    found: bool = False
    name: Optional[str] = None
    waterway_type: Optional[str] = None   # river, stream, canal, …
    within_feet: Optional[int] = None     # radius used for the check


# ── Final match result ────────────────────────────────────────────────────────

class MatchResult(BaseModel):
    """
    A listing that passed all filters.

    Returned by the engine regardless of transport layer — the CLI pretty-prints
    it, the FastAPI layer serialises it to JSON, a future push-notification
    service wraps it in an APNs/FCM payload.
    """

    listing: RawListing
    profile_name: str
    piti: PITIBreakdown
    assumable: AssumableDetails
    is_affordable: bool                         # piti.total_monthly ≤ profile.max_monthly_piti
    alert_priority: AlertPriority
    why_matched: list[str] = Field(default_factory=list)
    assumable_piti: Optional[PITIBreakdown] = None  # PITI at assumable rate/balance
    hazard_notes: list[str] = Field(default_factory=list)
    waterway: Optional[WaterwayDetails] = None
    matched_at: datetime = Field(default_factory=datetime.utcnow)
