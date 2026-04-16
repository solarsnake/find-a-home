"""
Filtering and scoring logic — applied to every listing after scraping.

Pure functions: no I/O.  Returns MatchResult or None (excluded).

Filter order (fail fast):
  1. Bedrooms / bathrooms  (basic property criteria)
  2. HOA hard cap          (strict: > $50/mo is an automatic exclusion)
  3. PITI affordability    (max monthly payment)

After passing all filters, we detect assumable loans and build the
full MatchResult with priority and "why_matched" explanation.
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
    RawListing,
    SearchProfile,
)


def _check_beds_baths(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    """Return an exclusion reason string, or None if the listing passes."""
    if listing.bedrooms < profile.min_bedrooms:
        return f"Only {listing.bedrooms} bed (need {profile.min_bedrooms})"
    if listing.bathrooms < profile.min_bathrooms:
        return f"Only {listing.bathrooms} bath (need {profile.min_bathrooms})"
    return None


def _check_hoa(listing: RawListing, profile: SearchProfile) -> Optional[str]:
    """
    Strict HOA exclusion.

    None means HOA is not reported — we do NOT exclude these because many
    listings omit the HOA field even when it's $0.  The user can review
    manually.  Only exclude when a positive fee exceeding the cap is explicitly
    present.
    """
    if listing.hoa_monthly is not None and listing.hoa_monthly > profile.max_hoa_monthly:
        return f"HOA ${listing.hoa_monthly:,.0f}/mo exceeds ${profile.max_hoa_monthly:,.0f} cap"
    return None


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
        f"{listing.bedrooms}bd/{listing.bathrooms}ba meets "
        f"{profile.min_bedrooms}bd/{profile.min_bathrooms}ba minimum"
    )
    if listing.hoa_monthly is None:
        reasons.append("HOA not reported (no mandatory fee detected)")
    elif listing.hoa_monthly == 0:
        reasons.append("No HOA")
    else:
        reasons.append(f"HOA ${listing.hoa_monthly:,.0f}/mo ≤ ${profile.max_hoa_monthly:,.0f} cap")

    if assumable.is_assumable:
        kws = ", ".join(f'"{k}"' for k in assumable.matched_keywords[:3])
        reasons.append(f"Assumable loan keywords detected: {kws}")
        if assumable.assumable_rate is not None:
            reasons.append(
                f"Assumable rate ~{assumable.assumable_rate * 100:.2f}% "
                f"vs current {profile.interest_rate * 100:.2f}%"
            )
        if assumable.estimated_loan_balance is not None:
            reasons.append(
                f"Est. loan balance ~${assumable.estimated_loan_balance:,.0f}"
            )
        if assumable.equity_gap is not None:
            flag = " ⚠ HIGH CASH REQUIRED" if assumable.high_cash_required else ""
            reasons.append(
                f"Equity gap (cash above down payment needed): "
                f"${assumable.equity_gap:,.0f}{flag}"
            )
    return reasons


def _determine_priority(
    piti: PITIBreakdown,
    assumable: AssumableDetails,
    is_affordable: bool,
) -> AlertPriority:
    if assumable.is_assumable:
        return AlertPriority.CRITICAL if is_affordable else AlertPriority.HIGH
    return AlertPriority.NORMAL


def evaluate_listing(
    listing: RawListing,
    profile: SearchProfile,
) -> Optional[MatchResult]:
    """
    Apply all filters and return a MatchResult if the listing passes,
    or None if it is excluded.

    This is the central function called by the engine for every scraped listing.
    """
    # ── Beds / baths ──────────────────────────────────────────────────────────
    if _check_beds_baths(listing, profile):
        return None

    # ── HOA ───────────────────────────────────────────────────────────────────
    if _check_hoa(listing, profile):
        return None

    # ── PITI at market rate ───────────────────────────────────────────────────
    piti = calculate_piti(listing.price, profile)
    is_affordable = piti.total_monthly <= profile.max_monthly_piti

    # ── Assumable loan detection ──────────────────────────────────────────────
    assumable = AssumableDetails.from_description(listing.description, listing.price)

    # Always include assumable deals even if over market-rate PITI budget
    # (the assumable rate might make them affordable — highlighted separately)
    if not is_affordable and not assumable.is_assumable:
        return None

    # For assumable loans, also compute PITI at the assumable rate/balance
    assumable_piti: Optional[PITIBreakdown] = None
    if assumable.is_assumable:
        rate_override = assumable.assumable_rate  # may be None
        balance_override = assumable.estimated_loan_balance  # may be None
        if rate_override is not None or balance_override is not None:
            assumable_piti = calculate_piti(
                listing.price,
                profile,
                annual_rate=rate_override,
                loan_amount_override=balance_override,
            )
            # Re-evaluate affordability at assumable terms
            if assumable_piti.total_monthly <= profile.max_monthly_piti:
                is_affordable = True

    why_matched = _build_why_matched(listing, profile, piti, assumable)
    priority = _determine_priority(piti, assumable, is_affordable)

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
