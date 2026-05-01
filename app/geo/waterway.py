"""
Waterway proximity check via USGS National Hydrography Dataset (NHD).

Queries the USGS NHD MapServer for stream/river flowlines within a
configurable radius of a lat/lon point.  A property address is geocoded
first; if a natural waterway is found within the radius it is considered
to run through or immediately border the property.

Service: https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer
Layer 6: Flowline - Large Scale (streams + rivers + tidal shoreline)
Free, no API key, US-only (worldwide coverage planned via OSM fallback).

NHD FType codes we surface:
  558 = StreamRiver  — natural streams and rivers
  566 = TidalShoreline — tidal/coastal waterways

We exclude:
  336 = CanalDitch   — man-made drainage
  334 = Connector    — routing connectors (not real water)
  428 = Pipeline     — underground
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_NHD_URL = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query"
)
_USER_AGENT = "find-a-home/1.0 (real-estate-deal-finder; local-dev)"

# FType values to include as "natural flowing water"
_INCLUDE_FTYPES = {558, 566}   # StreamRiver, TidalShoreline

_FTYPE_LABELS = {
    558: "Stream/River",
    566: "Tidal Waterway",
}

FEET_TO_DEGREES_LAT = 1.0 / 364_000   # approx: 1 degree lat ≈ 364,000 ft


class WaterwayResult:
    """Result of a waterway proximity check."""

    def __init__(
        self,
        found: bool,
        name: Optional[str] = None,
        waterway_type: Optional[str] = None,
        distance_feet: Optional[float] = None,
    ) -> None:
        self.found = found
        self.name = name
        self.waterway_type = waterway_type
        self.distance_feet = distance_feet

    def __repr__(self) -> str:
        if not self.found:
            return "WaterwayResult(found=False)"
        return (
            f"WaterwayResult({self.waterway_type} '{self.name}' within ~{self.distance_feet:.0f}ft)"
        )


def _bbox(lat: float, lon: float, radius_feet: int) -> dict:
    """Convert a point + radius to a bounding box dict for the ArcGIS REST API."""
    d_lat = radius_feet * FEET_TO_DEGREES_LAT
    d_lon = d_lat / max(math.cos(math.radians(lat)), 0.001)
    return {
        "xmin": lon - d_lon,
        "ymin": lat - d_lat,
        "xmax": lon + d_lon,
        "ymax": lat + d_lat,
    }


async def check_waterway(
    lat: float,
    lon: float,
    radius_feet: int = 150,
) -> WaterwayResult:
    """
    Return a WaterwayResult indicating whether a mapped stream/river exists
    within `radius_feet` of (lat, lon).

    Uses USGS NHD large-scale flowline layer.  US coverage only;
    international addresses will return found=False without error.
    """
    import json as _json

    box = _bbox(lat, lon, radius_feet)
    ftype_filter = ",".join(str(f) for f in _INCLUDE_FTYPES)

    params = {
        "where": f"FType IN ({ftype_filter})",
        "geometry": _json.dumps(box),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outFields": "GNIS_Name,FType",
        "returnGeometry": "false",
        "resultRecordCount": "5",
        "f": "json",
    }

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.get(
                _NHD_URL, params=params,
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            features = resp.json().get("features", [])
    except Exception as exc:
        logger.debug("NHD query failed at %.4f,%.4f: %s", lat, lon, exc)
        return WaterwayResult(found=False)

    if not features:
        return WaterwayResult(found=False)

    # Pick the best named result, preferring StreamRiver over Tidal
    best_name: Optional[str] = None
    best_ftype: int = 999
    for feat in features:
        attrs = feat.get("attributes", {})
        ftype = attrs.get("FType") or attrs.get("ftype") or 0
        name = attrs.get("GNIS_Name") or attrs.get("gnis_name") or None
        if ftype < best_ftype or (ftype == best_ftype and name and not best_name):
            best_ftype = ftype
            best_name = name

    wtype = _FTYPE_LABELS.get(best_ftype, "Waterway")
    logger.debug(
        "Waterway found near %.4f,%.4f: %s '%s' (within %dft)",
        lat, lon, wtype, best_name, radius_feet,
    )
    return WaterwayResult(
        found=True,
        name=best_name,
        waterway_type=wtype,
        distance_feet=float(radius_feet),
    )
