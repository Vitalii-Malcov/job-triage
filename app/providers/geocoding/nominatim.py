"""Night audit 2026-10-27: workplace address -> coordinates, with provenance.

`Geocoder` is the interface; `NominatimGeocoder` asks an OpenStreetMap
Nominatim server. Called only on an explicit operator command
(`scripts/stage17_job_fit.py check-commute --address ...`), never during
collection or in bulk. Usage-policy safeguards (operations.osmfoundation.org
/policies/nominatim): an identifying User-Agent, at most one request per
second (`min_interval_seconds`), results cached locally so the same address
is never asked twice, single result, Germany only.

Only the employer's workplace address text leaves the machine -- never the
candidate's home (the commute origin comes from the stored configuration as
coordinates and is not geocoded here).

`precision` says what the coordinates stand for. Only ADDRESS (a house or
building) may later count as a verified workplace route; STREET, POSTCODE
and CITY are approximations.
"""

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx

from app.agents.workplace_address import (
    CITY_QUALIFIERS,
    address_words,
    city_key,
    city_qualifiers,
)
from app.models.candidate_search import GeoPoint

DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org"
USER_AGENT = "JobTriage/1.0 (single-user job search tool; manual commute checks)"
_LAST_REQUEST_KEY = "__last_request_epoch__"
Precision = Literal["ADDRESS", "AREA", "STREET", "POSTCODE", "CITY", "UNKNOWN"]

# Only a building-level object is an ADDRESS. An industrial / commercial
# AREA (class=landuse) is a polygon's centre, not the workplace (Codex review).
_ADDRESS_TYPES = {"house", "building"}
_ADDRESS_CLASSES = {"building", "amenity", "office", "shop", "craft"}
_AREA_TYPES = {"industrial", "commercial", "retail", "landuse", "neighbourhood"}
_STREET_TYPES = {"road", "street", "highway"}
_POSTCODE_TYPES = {"postcode"}
_CITY_TYPES = {"city", "town", "village", "suburb", "municipality", "hamlet", "quarter", "county"}


class GeocodingUnavailableError(Exception):
    """No usable coordinates (network/server error or no result)."""


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    precision: Precision
    provenance: str  # e.g. "nominatim:way/123456 (house)"
    display_name: str
    # Codex round 10: ADDRESS precision only after the hit's street and
    # house number were compared with the request; results cached before
    # that check (False) are read back as STREET.
    building_checked: bool = False
    # Version of the hit checks this result passed; an entry cached under an
    # older version is fetched again (Codex round 12).
    check_version: int = 0

    @property
    def point(self) -> GeoPoint:
        return GeoPoint(lat=self.lat, lon=self.lon)


class Geocoder(Protocol):
    name: str

    def geocode(self, address: str) -> GeocodeResult: ...


def precision_from(addresstype: str | None, osm_class: str | None) -> Precision:
    kind = (addresstype or "").casefold()
    osm = (osm_class or "").casefold()
    if osm == "landuse" or kind in _AREA_TYPES:
        return "AREA"
    if kind in _ADDRESS_TYPES or osm in _ADDRESS_CLASSES:
        return "ADDRESS"
    if kind in _STREET_TYPES:
        return "STREET"
    if kind in _POSTCODE_TYPES:
        return "POSTCODE"
    if kind in _CITY_TYPES:
        return "CITY"
    return "UNKNOWN"


_HOUSE_NUMBER = re.compile(r"(?<![\w-])(\d{1,4}\s?[a-zA-Z]?(?:\s?[-/]\s?\d{1,4}[a-zA-Z]?)?)$")


def _number_key(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _compact(text: str) -> str:
    return address_words(text).replace(" ", "")


def _same_building(address: str, hit: dict) -> bool:
    """True when the hit names the requested street and house number (or
    the request names no house number). Uses the structured `address`
    details, else `display_name` (Codex rounds 9-10: "Industriestraße 50"
    or "Hauptstraße 5" returned for "Industriestraße 5" is not the
    building)."""
    street_part = (address or "").split(",", 1)[0].strip()
    wanted = _HOUSE_NUMBER.search(street_part)
    if wanted is None:
        return True
    key = _number_key(wanted.group(1))
    street = _compact(street_part[: wanted.start()])
    details = hit.get("address")
    display = str(hit.get("display_name") or "")
    if not street:
        return False
    if isinstance(details, dict) and details.get("house_number"):
        number_ok = _number_key(str(details["house_number"])) == key
        road = _compact(str(details.get("road") or details.get("pedestrian") or ""))
        return number_ok and road == street
    # display_name fallback: a whole comma part must be "street number" or
    # the parts "number" and "street" (round 11: "Neue Industriestraße 5"
    # is another street).
    parts = {_compact(part) for part in display.split(",")}
    return street + key in parts or (key in parts and street in parts)


_POSTCODE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
# 3: round 16-17 town check for postcode-less hits.
CHECK_VERSION = 3


def _hit_postcode(hit: dict) -> str | None:
    details = hit.get("address")
    if isinstance(details, dict) and details.get("postcode"):
        found = _POSTCODE.search(str(details["postcode"]))
        return found.group(1) if found else None
    found = _POSTCODE.findall(str(hit.get("display_name") or ""))
    return found[-1] if found else None


def _names_requested_town(address: str, hit: dict) -> bool:
    """True when the hit names every word of the requested town (the part
    after the postcode, e.g. "63450 Hanau" -> "hanau")."""
    # The whole town part, qualifier included ("15230 Frankfurt (Oder)").
    found = re.search(r"(?<!\d)\d{5}\s+([^,;]+)", address or "")
    if found is None:
        return False
    town = city_key(found.group(1))
    details = hit.get("address") if isinstance(hit.get("address"), dict) else {}
    named = " ".join(
        str(details.get(field) or "")
        for field in ("city", "town", "village", "municipality", "suburb")
    )
    words = set(address_words(f"{named} {hit.get('display_name') or ''}").split())
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue")):
        words = {word.replace(umlaut, plain) for word in words}
    if not town or not all(part in words for part in town.split()):
        return False
    # Same name, other town: "Frankfurt (Oder)" for "Frankfurt am Main".
    wanted, named = city_qualifiers(found.group(1)), words & CITY_QUALIFIERS
    return not (wanted and named and not wanted & named)


def _verified_precision(address: str, hit: dict, precision: Precision) -> Precision:
    """The precision a hit may be used with, or GeocodingUnavailableError.

    Codex round 11: a hit for another building or another postcode is an
    unrelated point. Its coordinates are never used, not even as an
    approximate destination (a route to it could make a wrong FAIL as
    easily as a wrong PASS); the distance stays unverified."""
    wanted = _POSTCODE.search(address or "")
    got = _hit_postcode(hit)
    if wanted is not None and got is not None and got != wanted.group(1):
        raise GeocodingUnavailableError(
            f"geocoder returned a place in {got}, not {wanted.group(1)}; "
            "measure with --dest-lat/--dest-lon"
        )
    if wanted is not None and got is None and not _names_requested_town(address, hit):
        # Round 16: an approximate hit without a postcode must at least be
        # in the requested town ("Industriestraße" exists in many towns).
        raise GeocodingUnavailableError(
            "geocoder result is not in the requested town; measure with --dest-lat/--dest-lon"
        )
    if precision == "ADDRESS" and (wanted is None or got is None):
        # Rounds 12-13: an exact building needs a postcode on both sides
        # ("Industriestraße 5, Hanau" also exists in Maintal).
        raise GeocodingUnavailableError(
            "no postcode to confirm the building (give the address with its postcode, "
            "or measure with --dest-lat/--dest-lon)"
        )
    if precision == "ADDRESS" and not _same_building(address, hit):
        raise GeocodingUnavailableError(
            "geocoder returned a different building "
            f"({str(hit.get('display_name') or '?')[:80]}); measure with --dest-lat/--dest-lon"
        )
    return precision


def _from_cache(entry: dict) -> GeocodeResult | None:
    """A cached result, or None when it must be fetched again: a result
    cached under older hit checks (rounds 10-12) may be another building or
    another place, so it is neither trusted nor downgraded."""
    if entry.get("check_version") != CHECK_VERSION:
        return None
    return GeocodeResult(**{**entry, "provenance": entry["provenance"] + " [cached]"})


class NominatimGeocoder:
    name = "NOMINATIM"

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        base_url: str = DEFAULT_NOMINATIM_URL,
        timeout_seconds: float = 10.0,
        min_interval_seconds: float = 1.1,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = min_interval_seconds
        self.cache_path = cache_path
        self._client = http_client
        self._clock = clock
        self._sleep = sleep
        self.lock_timeout_seconds = lock_timeout_seconds
        self._cache: dict[str, dict] = self._load_cache()
        # Persisted with the cache, so the 1 request/s limit also holds across
        # separate CLI runs (Codex night-audit review).
        stored = self._cache.pop(_LAST_REQUEST_KEY, None)
        self._last_request: float | None = stored if isinstance(stored, int | float) else None

    def _load_cache(self) -> dict[str, dict]:
        if self.cache_path is None or not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(self._cache)
        if self._last_request is not None:
            data[_LAST_REQUEST_KEY] = self._last_request
        # A unique temporary file per writer: two runs never share one.
        handle, name = tempfile.mkstemp(dir=self.cache_path.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(json.dumps(data, ensure_ascii=False, indent=1))
        Path(name).replace(self.cache_path)

    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Cross-process lock around "wait, record time, send": concurrent CLI
        runs cannot both see no recent request (Codex review). Stale locks
        (> 60 s, a crashed run) are taken over."""
        if self.cache_path is None:
            yield
            return
        lock = self.cache_path.with_suffix(self.cache_path.suffix + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout_seconds
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 60:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise GeocodingUnavailableError("geocoder busy (lock held)") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _merge_from_disk(self) -> None:
        """Take over addresses and the newest request time other runs saved."""
        disk = self._load_cache()
        stored = disk.pop(_LAST_REQUEST_KEY, None)
        if isinstance(stored, int | float) and (
            self._last_request is None or stored > self._last_request
        ):
            self._last_request = stored
        for address, value in disk.items():
            if isinstance(value, dict):
                self._cache.setdefault(address, value)

    @staticmethod
    def _key(address: str) -> str:
        return " ".join(address.casefold().split())

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval_seconds - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(min(wait, self.min_interval_seconds))
        self._last_request = self._clock()
        self._save_cache()  # record the request time before sending (under the lock)

    def _fetch(self, address: str):
        params = {
            "q": address,
            "format": "jsonv2",
            "limit": "1",
            "countrycodes": "de",
            "addressdetails": "1",
        }
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "de"}
        try:
            if self._client is not None:
                response = self._client.get(
                    f"{self.base_url}/search",
                    params=params,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.get(f"{self.base_url}/search", params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeocodingUnavailableError(
                f"geocoding request failed: {type(exc).__name__}"
            ) from None

    def _parse(self, payload, address: str = "") -> GeocodeResult:
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise GeocodingUnavailableError("no geocoding result for this address")
        hit = payload[0]
        try:
            lat, lon = float(hit["lat"]), float(hit["lon"])
            GeoPoint(lat=lat, lon=lon)
        except (KeyError, TypeError, ValueError):
            raise GeocodingUnavailableError("geocoding result has no valid coordinates") from None
        addresstype = hit.get("addresstype") or hit.get("type")
        precision = precision_from(addresstype, hit.get("class") or hit.get("category"))
        precision = _verified_precision(address, hit, precision)
        checked = precision == "ADDRESS"
        result = GeocodeResult(
            lat=lat,
            lon=lon,
            precision=precision,
            provenance=f"nominatim:{hit.get('osm_type', '?')}/{hit.get('osm_id', '?')} "
            f"({addresstype or 'unknown'})",
            display_name=str(hit.get("display_name") or "")[:300],
            building_checked=checked,
            check_version=CHECK_VERSION,
        )
        return result

    def geocode(self, address: str) -> GeocodeResult:
        key = self._key(address or "")
        if not key:
            raise GeocodingUnavailableError("empty address")
        cached = self._cache.get(key)
        if cached is not None and (hit := _from_cache(cached)) is not None:
            return hit
        with self._exclusive():
            # Everything that reads or writes shared state happens under the
            # cross-process lock (Codex round 3): another run may have cached
            # this address meanwhile, and the save merges with the file.
            self._merge_from_disk()
            cached = self._cache.get(key)
            if cached is not None and (hit := _from_cache(cached)) is not None:
                return hit
            self._throttle()
            result = self._parse(self._fetch(address), address)
            self._cache[key] = asdict(result)
            self._save_cache()
        return result
