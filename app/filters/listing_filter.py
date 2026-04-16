"""
Filtering and scoring logic — applied after scraping, before alerts.

Filter order (fail-fast — cheapest checks first):
  1. Property type
  2. Bedrooms / bathrooms
  3. Sqft
  4. Price range
  5. HOA hard cap
  6. Solar requirement
  7. Assumable-only flag
  8. PITI affordability  (most expensive check — skip if already excluded)

Pure functions: no I/O, fully testable.
"""

from __future__ import annotations

from typing import Optional

from app.financial.calculator import calculate_piti
from app.models import (
    AlertPriority,
    AssumableDetails,
    DataSource,
    MatchResult,
    PITIBreakdown,
    PropertyType,
    RawListing,
    SearchProfile,
)


# ── Individual checks (return exclusion reason or None) ──────────────────────

def _check_property_type(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    if PropertyType.ANY in profile.property_types:
        return None
    if not listing.property_type:
        return None  # Unknown type — don't exclude
    ptype = listing.property_type.lower().replace(" ", "_").replace("-", "_")
    allowed = {pt.value for pt in profile.property_types}
    if ptype not in allowed and not any(a in ptype for a in allowed):
        return f"Property type '{listing.property_type}' not in {[p.value for p in profile.property_types]}"
    return None


def _check_beds_baths(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    if listing.bedrooms < profile.min_bedrooms:
        return f"Only {listing.bedrooms} bed (need ≥{profile.min_bedrooms})"
    if profile.max_bedrooms and listing.bedrooms > profile.max_bedrooms:
        return f"{listing.bedrooms} beds exceeds max {profile.max_bedrooms}"
    if listing.bathrooms < profile.min_bathrooms:
        return f"Only {listing.bathrooms} bath (need ≥{profile.min_bathrooms})"
    return None


def _check_sqft(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    if profile.min_sqft and listing.sqft and listing.sqft < profile.min_sqft:
        return f"{listing.sqft:,} sqft < min {profile.min_sqft:,}"
    if profile.max_sqft and listing.sqft and listing.sqft > profile.max_sqft:
        return f"{listing.sqft:,} sqft > max {profile.max_sqft:,}"
    return None


def _check_price(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    if profile.min_price and listing.price < profile.min_price:
        return f"Price ${listing.price:,.0f} < min ${profile.min_price:,.0f}"
    return None


def _check_hoa(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    """
    Exclude only when a positive fee exceeding the cap is explicitly present.
    None = not reported → do NOT exclude (many listings omit HOA even when $0).
    """
    if listing.hoa_monthly is not None and listing.hoa_monthly > profile.max_hoa_monthly:
        return f"HOA ${listing.hoa_monthly:,.0f}/mo > ${profile.max_hoa_monthly:,.0f} cap"
    return None


def _check_solar(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    if profile.requires_solar and not listing.has_solar:
        return "No solar mention in description"
    return None


def _check_assumable_only(
    listing: RawListing, profile: SearchProfile, assumable: AssumableDetails
) -> Optional[str]:
    if profile.assumable_only and not assumable.is_assumable:
        return "assumable_only filter set — no assumable keywords found"
    return None


# ── Result builders ───────────────────────────────────────────────────────────

def _build_why_matched(
    listing: RawListing,
    profile: SearchProfile,
    piti: PITIBreakdown,
    assumable: AssumableDetails,
) -> list[str]:
    reasons: list[str] = []

    reasons.append(
        f"PITI ${piti.total_monthly:,.0f}/mo ≤ budget ${profile.max_monthly_piti:,.0f}"
    )
    reasons.append(
        f"{listing.bedrooms}bd / {listing.bathrooms}ba"
        + (f" / {listing.sqft:,} sqft" if listing.sqft else "")
    )

    if listing.hoa_monthly is None:
        reasons.append("HOA not reported (verify manually)")
    elif listing.hoa_monthly == 0:
        reasons.append("No HOA")
    else:
        reasons.append(f"HOA ${listing.hoa_monthly:,.0f}/mo ≤ ${profile.max_hoa_monthly:,.0f} cap")

    if listing.has_solar:
        reasons.append("Solar detected in description")

    if listing.days_on_market is not None:
        reasons.append(f"{listing.days_on_market} days on market")

    if assumable.is_assumable:
        kws = ", ".join(f'"{k}"' for k in assumable.matched_keywords[:3])
        reasons.append(f"Assumable loan keywords: {kws}")
        if assumable.assumable_rate:
            reasons.append(
                f"Assumable rate ~{assumable.assumable_rate * 100:.2f}%"
                f" vs market {profile.interest_rate * 100:.2f}%"
            )
        if assumable.estimated_loan_balance:
            reasons.append(f"Est. loan balance ~${assumable.estimated_loan_balance:,.0f}")
        if assumable.equity_gap is not None:
            flag = "  ⚠ HIGH CASH REQUIRED" if assumable.high_cash_required else ""
            reasons.append(f"Equity gap: ${assumable.equity_gap:,.0f}{flag}")

    return reasons


def _determine_priority(
    assumable: AssumableDetails, is_affordable: bool
) -> AlertPriority:
    if assumable.is_assumable:
        return AlertPriority.CRITICAL if is_affordable else AlertPriority.HIGH
    return AlertPriority.NORMAL


# ── Main entry point ──────────────────────────────────────────────────────────

def evaluate_listing(
    listing: RawListing,
    profile: SearchProfile,
) -> Optional[MatchResult]:
    """
    Apply all filters. Return a MatchResult if the listing passes, None if excluded.
    Called by the engine for every scraped listing.
    """
    # Run all hard-exclusion checks (order matters for fast failure)
    for check in (
        _check_property_type,
        _check_beds_baths,
        _check_sqft,
        _check_price,
        _check_hoa,
        _check_solar,
    ):
        if check(listing, profile):
            return None

    # Assumable detection (needed for both assumable_only check and scoring)
    assumable = AssumableDetails.from_description(listing.description, listing.price)

    if _check_assumable_only(listing, profile, assumable):
        return None

    # PITI at market rate
    piti = calculate_piti(listing.price, profile)
    is_affordable = piti.total_monthly <= profile.max_monthly_piti

    # Always include assumable deals even if over market-rate budget
    if not is_affordable and not assumable.is_assumable:
        return None

    # PITI at assumable rate/balance (if we have enough info)
    assumable_piti: Optional[PITIBreakdown] = None
    if assumable.is_assumable and (
        assumable.assumable_rate is not None or assumable.estimated_loan_balance is not None
    ):
        assumable_piti = calculate_piti(
            listing.price,
            profile,
            annual_rate=assumable.assumable_rate,
            loan_amount_override=assumable.estimated_loan_balance,
        )
        if assumable_piti.total_monthly <= profile.max_monthly_piti:
            is_affordable = True

    why_matched = _build_why_matched(listing, profile, piti, assumable)
    priority = _determine_priority(assumable, is_affordable)

    return MatchResult(
        listing=listing,
        profile_name=profile.name,
        piti=piti,
        assumable=assumable,
        is_affordable=is_affordable,
        alert_priority=priority,
        why_matched=why_matched,
        assumable_piti=assumable_piti,
    )
