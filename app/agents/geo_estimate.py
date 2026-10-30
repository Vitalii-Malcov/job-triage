"""Preliminary commute estimate from coordinates (Stage 22 P1).

The BA search result carries the posting's workplace coordinates. A
straight-line distance is NOT a car route: it is only a LOWER bound of it
(no road is shorter than the straight line). That makes two statements
sound without a routing service:

- straight line > car-route limit  => the car route is beyond the limit too
  (provided the coordinates are the real workplace);
- straight line <= limit / ROAD_FACTOR => very likely within the limit
  (German road distances are typically 1.2-1.4x the straight line).

Everything in between is "near the limit". The estimate ranks vacancies
(the fit tier); it never replaces the car-route check before a Bewerbung
(`job_fit_assessment` keeps the decision at NEEDS_VERIFICATION).
"""

import math
from dataclasses import dataclass
from typing import Literal

EARTH_RADIUS_KM = 6371.0088
# Conservative road/straight-line ratio for "very likely within the limit".
ROAD_FACTOR = 1.4

EstimateBand = Literal["WITHIN", "NEAR_LIMIT", "BEYOND"]

# BA `region` values of Hessen and its neighbouring states: daily commuting
# across the state border is fine for the candidate, relocation is not.
HOME_STATE = "HESSEN"
NEIGHBOUR_STATES = frozenset(
    {
        "BADEN_WUERTTEMBERG",
        "BAYERN",
        "NIEDERSACHSEN",
        "NORDRHEIN_WESTFALEN",
        "RHEINLAND_PFALZ",
        "THUERINGEN",
    }
)


def straight_line_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class CommuteEstimate:
    straight_km: float
    band: EstimateBand
    limit_km: int


def estimate_commute(
    origin_lat: float, origin_lon: float, lat: float, lon: float, limit_km: int
) -> CommuteEstimate:
    km = straight_line_km(origin_lat, origin_lon, lat, lon)
    if km > limit_km:
        band: EstimateBand = "BEYOND"
    elif km * ROAD_FACTOR <= limit_km:
        band = "WITHIN"
    else:
        band = "NEAR_LIMIT"
    return CommuteEstimate(round(km, 1), band, limit_km)


def state_key(name: str | None) -> str:
    """Comparable key of a German state name: BA "BADEN_WUERTTEMBERG" and
    a configured "Baden-Württemberg" give the same key."""
    value = (name or "").casefold()
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(umlaut, plain)
    return "".join(ch for ch in value if ch.isalnum())


def commutable_state(region: str | None) -> bool | None:
    """True for Hessen and its neighbours, False for any other German state,
    None when the region is unknown."""
    if not region:
        return None
    value = region.strip().upper()
    return value == HOME_STATE or value in NEIGHBOUR_STATES
