"""
Wildfire and flood hazard lookups for CA listings.

Fire Hazard Severity Zones (FHSZ) are defined by Cal Fire:
  https://osfm.fire.ca.gov/divisions/community-wildfire-preparedness-and-mitigation/fire-hazard-severity-zones/

This module uses a static zip-code lookup derived from Cal Fire FHSZ GIS data
(Local Responsibility Area + State Responsibility Area maps combined).
Zones:
  VERY_HIGH  — highest risk; most insurance carriers restrict or surcharge
  HIGH       — significant risk; standard insurance may be harder to obtain
  MODERATE   — below-average risk for CA

Flood risk uses FEMA Flood Zone designations (zone A/AE = 100-year floodplain).
The lookup covers the user's active search areas; expand as needed.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class FireRisk(str, Enum):
    VERY_HIGH = "VERY HIGH"
    HIGH      = "HIGH"
    MODERATE  = "MODERATE"
    UNKNOWN   = "unknown"


# Cal Fire FHSZ — zip codes in High or Very High SRA/LRA zones.
# Source: Cal Fire FHSZ viewer + county parcel data cross-reference.
# Last updated: 2024. Update as new designations are published.
_FIRE_RISK: dict[str, FireRisk] = {
    # ── Escondido CA ────────────────────────────────────────────────────────
    "92025": FireRisk.HIGH,
    "92026": FireRisk.VERY_HIGH,
    "92027": FireRisk.HIGH,
    # ── Ramona CA ───────────────────────────────────────────────────────────
    "92065": FireRisk.VERY_HIGH,
    # ── Valley Center CA ────────────────────────────────────────────────────
    "92082": FireRisk.VERY_HIGH,
    # ── Poway CA ────────────────────────────────────────────────────────────
    "92064": FireRisk.HIGH,
    # ── Durham NC (no wildfire FHSZ equivalent) ─────────────────────────────
    # GA/SC lake areas — minimal wildfire risk
}

# FEMA Special Flood Hazard Areas (Zone A/AE) by zip.
# Lake Hartwell area has some flood zones along the shoreline.
_FLOOD_RISK: set[str] = {
    "29693",  # Westminster SC (Lake Hartwell shore)
    "29691",  # Walhalla SC (Oconee County lake areas)
}


def fire_risk(zip_code: str) -> FireRisk:
    return _FIRE_RISK.get(zip_code, FireRisk.UNKNOWN)


def flood_risk(zip_code: str) -> bool:
    return zip_code in _FLOOD_RISK


def hazard_notes(zip_code: str) -> list[str]:
    """
    Return a list of hazard warning strings for display in the listing panel.
    Empty list means no known hazards for this zip.
    """
    notes: list[str] = []
    fr = fire_risk(zip_code)
    if fr == FireRisk.VERY_HIGH:
        notes.append("🔥 VERY HIGH fire risk (Cal Fire FHSZ) — insurance may be restricted or very expensive")
    elif fr == FireRisk.HIGH:
        notes.append("🔥 HIGH fire risk (Cal Fire FHSZ) — verify insurance availability before offer")
    if flood_risk(zip_code):
        notes.append("🌊 FEMA flood zone — flood insurance required by lender (~$1,000–3,000/yr)")
    return notes
