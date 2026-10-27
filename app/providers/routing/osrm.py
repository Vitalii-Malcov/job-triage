"""Stage 17C: car-route distance between two coordinates.

`CarRouteProvider` is the interface; `OsrmRouteProvider` asks an OSRM server
(default: the public demo at router.project-osrm.org) for the driving route.
Only the two coordinate pairs leave the machine -- no name, address, job or
employer text. It is called only on an explicit operator command
(`scripts/stage17_job_fit.py check-commute`), never during collection.

OSRM reports free-flow travel time (no live traffic); callers label it so.
"""

import math
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.models.candidate_search import GeoPoint

DEFAULT_OSRM_URL = "https://router.project-osrm.org"


class RouteUnavailableError(Exception):
    """No route could be determined (network, server or no-route answer)."""


@dataclass(frozen=True)
class CarRoute:
    km: float
    minutes: float


class CarRouteProvider(Protocol):
    name: str

    def car_route(self, origin: GeoPoint, destination: GeoPoint) -> CarRoute: ...


class OsrmRouteProvider:
    name = "OSRM"

    def __init__(
        self,
        base_url: str = DEFAULT_OSRM_URL,
        timeout_seconds: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = http_client

    def car_route(self, origin: GeoPoint, destination: GeoPoint) -> CarRoute:
        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{origin.lon:.6f},{origin.lat:.6f};{destination.lon:.6f},{destination.lat:.6f}"
        )
        params = {"overview": "false", "alternatives": "false", "steps": "false"}
        try:
            if self._client is not None:
                response = self._client.get(url, params=params, timeout=self.timeout_seconds)
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RouteUnavailableError(f"OSRM request failed: {type(exc).__name__}") from None
        if not isinstance(payload, dict):
            raise RouteUnavailableError("OSRM returned a non-object payload")
        routes = payload.get("routes")
        if payload.get("code") != "Ok" or not isinstance(routes, list) or not routes:
            raise RouteUnavailableError(f"OSRM returned no route (code={payload.get('code')})")
        if not isinstance(routes[0], dict):
            raise RouteUnavailableError("OSRM route entry is not an object")
        distance, duration = routes[0].get("distance"), routes[0].get("duration")
        if not isinstance(distance, int | float) or not isinstance(duration, int | float):
            raise RouteUnavailableError("OSRM route has no numeric distance/duration")
        if not math.isfinite(distance) or distance < 0:
            raise RouteUnavailableError("OSRM route distance is invalid")
        return CarRoute(km=round(distance / 1000, 1), minutes=round(duration / 60, 1))


def straight_line_km(a: GeoPoint, b: GeoPoint) -> float:
    """Great-circle distance. A LOWER BOUND for the car route, never a
    substitute for it (informational only)."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat, dlon = lat2 - lat1, math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return round(2 * 6371.0088 * math.asin(math.sqrt(h)), 1)
