"""Night audit 2026-10-27: workplace address, geocoding and approximate routes.

No network: the geocoder and the router are fakes or an httpx MockTransport.
Addresses are public employer-style addresses; no candidate home address.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from app.agents.workplace_address import best_workplace, extract_addresses
from app.db.job_commute_repository import COMMUTE_METHODS, is_approximate
from app.providers.geocoding.nominatim import (
    USER_AGENT,
    GeocodeResult,
    GeocodingUnavailableError,
    NominatimGeocoder,
    precision_from,
)
from app.providers.routing.osrm import CarRoute
from app.services import job_fit_assessment as fit
from tests.test_stage17_job_fit import SETTINGS, _count, _FakeProvider, cli, migrated  # noqa: F401

# --- address extraction ----------------------------------------------------


def test_labelled_workplace_beats_legal_seat():
    text = (
        "Arbeitsort: Industriestraße 5, 63450 Hanau. "
        "Sitz der Gesellschaft: Hauptstraße 1, 80331 München, HRB 12345."
    )
    kinds = {a.text: a.kind for a in extract_addresses(text)}
    assert kinds == {
        "Industriestraße 5, 63450 Hanau": "WORKPLACE",
        "Hauptstraße 1, 80331 München": "LEGAL_SEAT",
    }
    assert best_workplace(text).text == "Industriestraße 5, 63450 Hanau"


def test_head_office_in_another_city_is_not_the_workplace():
    """Real case: a Frankfurt posting whose only address is the Karlsruhe
    head office."""
    text = "Kontakt: Musterbildung GmbH, Kriegsstraße 100, 76133 Karlsruhe"
    assert best_workplace(text, job_location="Frankfurt am Main") is None
    assert best_workplace(text, job_location="Karlsruhe, Baden").kind == "UNLABELLED"


def test_agency_address_is_never_the_workplace():
    text = "Ihr Ansprechpartner: Beispiel Personal GmbH, Mainzer Landstraße 50, 60325 Frankfurt"
    [address] = extract_addresses(text, posted_by_agency=True)
    assert address.kind == "AGENCY"
    assert best_workplace(text, posted_by_agency=True) is None


def test_two_unlabelled_addresses_are_ambiguous():
    text = "Standorte: Am Wall 2, 63450 Hanau und Lindenallee 7, 63452 Hanau"
    assert best_workplace(text, job_location="Hanau") is None


def test_no_address_in_text():
    assert extract_addresses("Wir suchen Verstärkung in Frankfurt am Main.") == []


# --- geocoder ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("addresstype", "osm_class", "precision"),
    [
        ("house", "place", "ADDRESS"),
        ("industrial", "landuse", "AREA"),
        ("commercial", "landuse", "AREA"),
        ("company", "office", "ADDRESS"),
        ("building", "building", "ADDRESS"),
        ("road", "highway", "STREET"),
        ("postcode", "place", "POSTCODE"),
        ("city", "boundary", "CITY"),
        ("town", "place", "CITY"),
        (None, None, "UNKNOWN"),
    ],
)
def test_precision_mapping(addresstype, osm_class, precision):
    assert precision_from(addresstype, osm_class) == precision


def _geocoder(tmp_path, responses, **kwargs):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    geocoder = NominatimGeocoder(
        cache_path=tmp_path / "geocode_cache.local.json",
        http_client=client,
        clock=lambda: clock.now,
        sleep=sleep,
        **kwargs,
    )
    return geocoder, calls, clock


def _hit(addresstype="house", lat="50.13", lon="8.92"):
    return httpx.Response(
        200,
        json=[
            {
                "lat": lat,
                "lon": lon,
                "addresstype": addresstype,
                "class": "place",
                "osm_type": "way",
                "osm_id": 42,
                # Nominatim names the postcode (round 12 requires it).
                "display_name": "Industriestraße 5, 63450 Hanau",
            }
        ],
    )


def test_geocoder_sends_only_the_address_with_policy_headers_and_caches(tmp_path):
    geocoder, calls, _ = _geocoder(tmp_path, [_hit()])
    result = geocoder.geocode("Industriestraße 5, 63450 Hanau")
    assert (result.lat, result.lon, result.precision) == (50.13, 8.92, "ADDRESS")
    assert result.provenance == "nominatim:way/42 (house)"
    [request] = calls
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.url.params["q"] == "Industriestraße 5, 63450 Hanau"
    assert request.url.params["countrycodes"] == "de" and request.url.params["limit"] == "1"
    # Second call (any spacing/case): served from the cache, no request.
    again = geocoder.geocode("  industriestraße 5,   63450 HANAU ")
    assert len(calls) == 1 and again.provenance.endswith("[cached]")
    # Persisted: a new instance needs no request either.
    fresh, fresh_calls, _ = _geocoder(tmp_path, [])
    assert fresh.geocode("Industriestraße 5, 63450 Hanau").lat == 50.13
    assert fresh_calls == []
    stored = json.loads((tmp_path / "geocode_cache.local.json").read_text(encoding="utf-8"))
    assert [k for k in stored if not k.startswith("__")] == ["industriestrasse 5, 63450 hanau"]


def test_geocoder_throttles_to_one_request_per_interval(tmp_path):
    # Street-level hits: these requests name no building the fake returns.
    geocoder, calls, clock = _geocoder(tmp_path, [_hit("road"), _hit("road", lat="50.2")])
    geocoder.geocode("A-Straße 1, 63450 Hanau")
    geocoder.geocode("B-Straße 2, 63450 Hanau")
    assert len(calls) == 2
    assert clock.sleeps and clock.sleeps[0] == pytest.approx(1.1)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=[]),
        httpx.Response(503, text="busy"),
        httpx.Response(200, json=[{"lat": "x", "lon": "8"}]),
        httpx.Response(200, json={"error": "bad"}),
    ],
)
def test_geocoder_failures_raise_and_cache_nothing(tmp_path, response):
    geocoder, _, _ = _geocoder(tmp_path, [response])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("Nirgendwo 1, 99999 Nirgends")
    cache = tmp_path / "geocode_cache.local.json"
    stored = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    assert not [k for k in stored if not k.startswith("__")]


def test_empty_address_never_reaches_the_network(tmp_path):
    geocoder, calls, _ = _geocoder(tmp_path, [])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("   ")
    assert calls == []


# --- location criterion --------------------------------------------------------


def _check(km, method):
    return SimpleNamespace(
        car_route_km=km,
        car_route_minutes=None,
        origin_label=SETTINGS.commute_origin_label,
        origin_lat=SETTINGS.commute_origin.lat,
        origin_lon=SETTINGS.commute_origin.lon,
        destination_label="Hanau",
        destination_region=None,
        method=method,
        source_note="",
    )


_JOB = SimpleNamespace(location="Hanau", description="", company="Beispiel GmbH")
_ONSITE = fit.Criterion("remote", "ONSITE", "on-site")


@pytest.mark.parametrize(
    ("km", "method", "status"),
    [
        (99.0, "OSRM", "PASS"),
        (100.0, "OSRM", "PASS"),
        (100.1, "OSRM", "FAIL"),
        (70.0, "OSRM_APPROX", "PASS_APPROXIMATE"),
        (92.0, "OSRM_APPROX", "APPROXIMATE_NEAR_LIMIT"),
        (108.0, "MANUAL_APPROX", "APPROXIMATE_NEAR_LIMIT"),
        (130.0, "OSRM_APPROX", "FAIL"),
    ],
)
def test_location_status_respects_precision_and_limit(km, method, status):
    criterion = fit.location_criterion(_JOB, SETTINGS, _ONSITE, _check(km, method))
    assert criterion.status == status


def test_exact_route_near_limit_is_flagged_in_reason():
    reason = fit.location_criterion(_JOB, SETTINGS, _ONSITE, _check(98.0, "OSRM")).reason
    assert "within 5 km of the limit" in reason


def test_approximate_near_limit_needs_verification():
    assessment = fit.FitAssessment(
        1, "c", "t", [fit.Criterion("location", "APPROXIMATE_NEAR_LIMIT", "")]
    )
    assert assessment.decision == "NEEDS_VERIFICATION"


def test_unverified_location_names_the_posted_workplace():
    job = SimpleNamespace(
        location="Hanau",
        company="Beispiel GmbH",
        description="Arbeitsort: Industriestraße 5, 63450 Hanau.",
    )
    criterion = fit.location_criterion(job, SETTINGS, _ONSITE, None)
    assert criterion.status == "DISTANCE_UNVERIFIED"
    assert "Industriestraße 5, 63450 Hanau (workplace)" in criterion.reason


def test_approximate_methods_are_known():
    assert {"OSRM_APPROX", "MANUAL_APPROX"} <= COMMUTE_METHODS
    assert is_approximate("OSRM_APPROX") and not is_approximate("OSRM")


# --- CLI ------------------------------------------------------------------------


class _FakeGeocoder:
    name = "FAKE"

    def __init__(self, precision):
        self.precision = precision
        self.calls = []

    def geocode(self, address):
        self.calls.append(address)
        return GeocodeResult(50.13, 8.92, self.precision, "fake:1", address)


def _methods(path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as conn:
            return [
                (row.method, row.source_note)
                for row in conn.execute(text("select method, source_note from job_commute_checks"))
            ]
    finally:
        engine.dispose()


def _address_args(path):
    return [
        "--database",
        str(path),
        "check-commute",
        "1",
        "--address",
        "Industriestraße 5, 63450 Hanau",
        "--destination",
        "Hanau",
        "--apply",
    ]


def test_cli_address_with_city_precision_is_stored_approximate(migrated):  # noqa: F811
    geocoder = _FakeGeocoder("CITY")
    provider = _FakeProvider(CarRoute(km=30.0, minutes=25))
    assert cli.run(_address_args(migrated), route_provider=provider, geocoder=geocoder) == 0
    [(method, note)] = _methods(migrated)
    assert method == "OSRM_APPROX"
    assert "destination CITY via fake:1" in note and "dest=50.13000,8.92000" in note


def test_cli_route_cache_avoids_second_request(migrated, capsys):  # noqa: F811
    geocoder = _FakeGeocoder("ADDRESS")
    provider = _FakeProvider(CarRoute(km=30.0, minutes=25))
    assert cli.run(_address_args(migrated), route_provider=provider, geocoder=geocoder) == 0
    assert cli.run(_address_args(migrated), route_provider=provider, geocoder=geocoder) == 0
    assert len(provider.calls) == 1
    assert "route cache hit" in capsys.readouterr().out
    assert [m for m, _ in _methods(migrated)] == ["OSRM", "OSRM"]


def test_cli_coordinates_with_postcode_precision(migrated):  # noqa: F811
    args = ["--database", str(migrated), "check-commute", "1", "--dest-lat", "50.1"]
    args += ["--dest-lon", "8.9", "--precision", "POSTCODE", "--destination", "63450", "--apply"]
    assert cli.run(args, route_provider=_FakeProvider(CarRoute(km=20.0, minutes=20))) == 0
    assert _methods(migrated)[0][0] == "OSRM_APPROX"


def test_cli_without_destination_is_refused(migrated, capsys):  # noqa: F811
    args = ["--database", str(migrated), "check-commute", "1", "--destination", "x"]
    assert cli.run(args, route_provider=_FakeProvider()) == 2
    assert "--address" in capsys.readouterr().err
    assert _count(migrated) == 0


def test_cli_geocoding_failure_stores_nothing(migrated, capsys):  # noqa: F811
    class Down:
        name = "DOWN"

        def geocode(self, address):
            raise GeocodingUnavailableError("no result")

    provider = _FakeProvider(CarRoute(km=1.0, minutes=1))
    assert cli.run(_address_args(migrated), route_provider=provider, geocoder=Down()) == 1
    assert provider.calls == [] and _count(migrated) == 0


def test_cli_record_commute_approximate(migrated):  # noqa: F811
    args = ["--database", str(migrated), "record-commute", "1", "--car-km", "95"]
    args += ["--destination", "Hanau (Stadtmitte)", "--source-note", "maps", "--approximate"]
    assert cli.run([*args, "--apply"]) == 0
    assert _methods(migrated)[0][0] == "MANUAL_APPROX"


def test_rate_limit_holds_across_separate_runs(tmp_path):
    """Codex review: each CLI run builds a new geocoder; the last request
    time is persisted, so a second run right after still waits."""
    # Street-level hits: these requests name no building the fake returns.
    first, _, clock = _geocoder(tmp_path, [_hit("road")])
    clock.now = 1000.0
    first.geocode("A-Straße 1, 63450 Hanau")
    second, calls, clock2 = _geocoder(tmp_path, [_hit("road", lat="50.2")])
    clock2.now = 1000.3
    second.geocode("B-Straße 2, 63450 Hanau")
    assert len(calls) == 1
    assert clock2.sleeps == [pytest.approx(0.8)]


def test_approximate_pass_still_needs_verification():
    assessment = fit.FitAssessment(1, "c", "t", [fit.Criterion("location", "PASS_APPROXIMATE", "")])
    assert assessment.decision == "NEEDS_VERIFICATION"


def test_fresh_lock_of_another_run_blocks_without_request(tmp_path):
    (tmp_path / "geocode_cache.local.json.lock").write_text("", encoding="utf-8")
    geocoder, calls, _ = _geocoder(tmp_path, [_hit()], lock_timeout_seconds=0.2)
    with pytest.raises(GeocodingUnavailableError, match="busy"):
        geocoder.geocode("A-Straße 1, 63450 Hanau")
    assert calls == []


def test_stale_lock_is_taken_over_and_released(tmp_path):
    import os
    import time

    lock = tmp_path / "geocode_cache.local.json.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 120
    os.utime(lock, (old, old))
    geocoder, calls, _ = _geocoder(tmp_path, [_hit()])
    # The request names the house number the fake hit returns (round 9).
    assert geocoder.geocode("Industriestraße 5, 63450 Hanau").precision == "ADDRESS"
    assert len(calls) == 1 and not lock.exists()


def test_cli_area_result_is_stored_approximate(migrated):  # noqa: F811
    geocoder = _FakeGeocoder("AREA")
    provider = _FakeProvider(CarRoute(km=30.0, minutes=25))
    assert cli.run(_address_args(migrated), route_provider=provider, geocoder=geocoder) == 0
    assert _methods(migrated)[0][0] == "OSRM_APPROX"


def test_two_runs_never_lose_each_others_cache_entries(tmp_path):
    """Codex round 3: two geocoders built on the same empty cache; A then B
    must both stay cached, and A is not requested again."""
    # Street-level hits: these requests name no building the fake returns.
    first, first_calls, _ = _geocoder(tmp_path, [_hit("road")])
    second, second_calls, _ = _geocoder(tmp_path, [_hit("road", lat="50.2")])
    first.geocode("A-Straße 1, 63450 Hanau")
    second.geocode("B-Straße 2, 63450 Hanau")
    assert second.geocode("A-Straße 1, 63450 Hanau").provenance.endswith("[cached]")
    stored = json.loads((tmp_path / "geocode_cache.local.json").read_text(encoding="utf-8"))
    assert sorted(k for k in stored if not k.startswith("__")) == [
        "a-strasse 1, 63450 hanau",
        "b-strasse 2, 63450 hanau",
    ]
    assert len(first_calls) == 1 and len(second_calls) == 1


def test_route_to_a_workplace_the_posting_no_longer_states_is_stale():
    """Codex round 4: same job id, new description with another address."""
    job = SimpleNamespace(
        location="Hanau",
        company="Beispiel GmbH",
        description="Arbeitsort: Neue Straße 9, 63452 Hanau.",
    )
    check = _check(40.0, "OSRM")
    check.destination_label = "Industriestraße 5, 63450 Hanau"
    check.source_note = "OSRM car route; dest=50.13000,8.92000"
    criterion = fit.location_criterion(job, SETTINGS, _ONSITE, check)
    assert criterion.status == "DISTANCE_UNVERIFIED"
    assert "Neue Straße 9, 63452 Hanau" in criterion.reason


def test_route_to_the_stated_workplace_still_counts():
    job = SimpleNamespace(
        location="Hanau",
        company="Beispiel GmbH",
        description="Arbeitsort: Industriestraße 5, 63450 Hanau.",
    )
    check = _check(40.0, "OSRM")
    check.destination_label = "Industriestraße 5, 63450 Hanau"
    check.source_note = "OSRM car route"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "PASS"


def test_new_street_in_the_same_postcode_makes_the_route_stale():
    """Codex round 5: same postcode, another street -> the old route is no
    longer evidence for the posted workplace."""
    job = SimpleNamespace(
        location="Hanau",
        company="Beispiel GmbH",
        description="Arbeitsort: Neue Straße 9, 63450 Hanau.",
    )
    check = _check(40.0, "OSRM")
    check.destination_label = "Industriestraße 5, 63450 Hanau"
    check.source_note = "OSRM car route; dest=50.13000,8.92000"
    criterion = fit.location_criterion(job, SETTINGS, _ONSITE, check)
    assert criterion.status == "DISTANCE_UNVERIFIED"
    assert "Neue Straße 9, 63450 Hanau" in criterion.reason


def test_abbreviated_street_label_still_matches_the_posting():
    job = SimpleNamespace(
        location="Hanau",
        company="Beispiel GmbH",
        description="Arbeitsort: Industriestraße 5, 63450 Hanau.",
    )
    check = _check(40.0, "OSRM")
    check.destination_label = "Industriestr. 5, 63450 Hanau"
    check.source_note = "OSRM car route"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "PASS"


def test_house_number_is_compared_whole():
    """Codex round 6: a route to house number 50 is not a route to 5."""
    job = SimpleNamespace(
        location="Hanau",
        company="Beispiel GmbH",
        description="Arbeitsort: Industriestraße 5, 63450 Hanau.",
    )
    check = _check(40.0, "OSRM")
    check.destination_label = "Industriestraße 50, 63450 Hanau"
    check.source_note = "OSRM car route"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "DISTANCE_UNVERIFIED"
    check.destination_label = "Industriestr. 5, 63450 Hanau"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "PASS"


def test_nearest_label_wins_and_never_crosses_the_previous_address():
    """Codex round 9: a legal-seat label before the first address must not
    also label the workplace address that follows it."""
    text = (
        "Sitz der Gesellschaft: Hauptstraße 1, 63452 Hanau. "
        "Arbeitsort: Industriestraße 5, 63450 Hanau."
    )
    kinds = {a.text: a.kind for a in extract_addresses(text)}
    assert kinds == {
        "Hauptstraße 1, 63452 Hanau": "LEGAL_SEAT",
        "Industriestraße 5, 63450 Hanau": "WORKPLACE",
    }
    assert best_workplace(text, job_location="Hanau").text == "Industriestraße 5, 63450 Hanau"


def _hit_with(display_name, house_number=None, road="Industriestraße", postcode="63450"):
    entry = {
        "lat": "50.13",
        "lon": "8.92",
        "addresstype": "building",
        "class": "building",
        "osm_type": "way",
        "osm_id": 7,
        "display_name": display_name,
    }
    if house_number is not None:
        entry["address"] = {"house_number": house_number, "road": road, "postcode": postcode}
    return httpx.Response(200, json=[entry])


@pytest.mark.parametrize(
    "response",
    [
        # Codex round 9: number 50 returned for number 5 is not the building.
        _hit_with("50, Industriestraße, Hanau", house_number="50"),
        _hit_with("Industriestraße 50, Hanau"),
        # Codex round 10: same number on another street.
        _hit_with("5, Hauptstraße, Hanau", house_number="5", road="Hauptstraße"),
        _hit_with("Hauptstraße 5, 63450 Hanau"),
        # Codex round 11: a longer street name containing the requested one.
        _hit_with("Neue Industriestraße 5, 63450 Hanau"),
        # Codex round 11: the right building name in another postcode.
        _hit_with("5, Industriestraße, 63477 Maintal", house_number="5", postcode="63477"),
    ],
)
def test_another_building_is_refused_and_its_point_never_used(tmp_path, response):
    """Round 11: the coordinates of another building are not even used as an
    approximate destination (they could make a wrong FAIL as easily as a
    wrong PASS); nothing is cached."""
    geocoder, calls, _ = _geocoder(tmp_path, [response])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("Industriestraße 5, 63450 Hanau")
    assert calls[0].url.params["addressdetails"] == "1"
    assert not (tmp_path / "geocode_cache.local.json").exists() or "industriestrasse" not in (
        tmp_path / "geocode_cache.local.json"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "response",
    [
        _hit_with("5, Industriestraße, Hanau", house_number="5"),
        _hit_with("Industriestraße 5, 63450 Hanau"),
    ],
)
def test_the_requested_building_is_exact(tmp_path, response):
    geocoder, _, _ = _geocoder(tmp_path, [response])
    result = geocoder.geocode("Industriestraße 5, 63450 Hanau")
    assert result.precision == "ADDRESS" and result.building_checked


def test_building_cached_before_the_check_is_fetched_again(tmp_path):
    """Codex rounds 10-11: an ADDRESS cached without the street/number check
    is neither trusted nor downgraded; it is looked up again."""
    cache = tmp_path / "geocode_cache.local.json"
    cache.write_text(
        json.dumps(
            {
                "industriestrasse 5, 63450 hanau": {
                    "lat": 50.13,
                    "lon": 8.92,
                    "precision": "ADDRESS",
                    "provenance": "nominatim:way/1 (house)",
                    "display_name": "Industriestraße 50, Hanau",
                }
            }
        ),
        encoding="utf-8",
    )
    geocoder, calls, _ = _geocoder(tmp_path, [_hit_with("5, Industriestraße, Hanau", "5")])
    result = geocoder.geocode("Industriestraße 5, 63450 Hanau")
    assert len(calls) == 1 and result.precision == "ADDRESS" and result.building_checked


def test_checked_building_result_stays_exact_from_the_cache(tmp_path):
    geocoder, calls, _ = _geocoder(tmp_path, [_hit_with("5, Industriestraße, Hanau", "5")])
    assert geocoder.geocode("Industriestraße 5, 63450 Hanau").precision == "ADDRESS"
    again = geocoder.geocode("Industriestraße 5, 63450 Hanau")
    assert again.precision == "ADDRESS" and again.building_checked and len(calls) == 1


def test_cli_stores_the_geocoded_address_as_destination(migrated):  # noqa: F811
    """Codex round 10: --destination naming another address must not become
    the label of a route measured to --address."""
    geocoder = _FakeGeocoder("ADDRESS")
    provider = _FakeProvider(CarRoute(km=30.0, minutes=25))
    args = _address_args(migrated)
    args[args.index("--address") + 1] = "Hauptstraße 5, 63450 Hanau"
    args[args.index("--destination") + 1] = "Industriestraße 5, 63450 Hanau"
    assert cli.run(args, route_provider=provider, geocoder=geocoder) == 0
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{migrated.as_posix()}")
    try:
        with engine.connect() as conn:
            label, note = conn.execute(
                text("select destination_label, source_note from job_commute_checks")
            ).one()
    finally:
        engine.dispose()
    assert label == "Hauptstraße 5, 63450 Hanau"
    assert "operator label 'Industriestraße 5, 63450 Hanau'" in note


def test_operator_label_in_the_note_never_makes_a_route_current():
    """Codex round 11: the note keeps the operator's wording; only the
    destination label names the point routed to."""
    job = SimpleNamespace(
        location="Hanau",
        company="Beispiel GmbH",
        description="Arbeitsort: Industriestraße 5, 63450 Hanau.",
    )
    check = _check(40.0, "OSRM")
    check.destination_label = "Hauptstraße 5, 63450 Hanau"
    check.source_note = "OSRM car route; operator label 'Industriestraße 5, 63450 Hanau'"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "DISTANCE_UNVERIFIED"


def test_route_to_another_city_is_unverified_without_a_posted_address():
    """Codex round 11: no street in the posting, route ends in Frankfurt,
    the job is in Hanau."""
    job = SimpleNamespace(location="Hanau", company="Beispiel GmbH", description="Wir suchen.")
    check = _check(40.0, "OSRM")
    check.destination_label = "Industriestraße 5, 60311 Frankfurt"
    check.source_note = "OSRM car route"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "DISTANCE_UNVERIFIED"
    check.destination_label = "Industriestraße 5, 63450 Hanau"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "PASS"


def test_building_hit_without_a_postcode_is_refused(tmp_path):
    """Codex round 12: Maintal named only by city, no postcode to confirm."""
    response = _hit_with("5, Industriestraße, Maintal", house_number="5", postcode=None)
    geocoder, _, _ = _geocoder(tmp_path, [response])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("Industriestraße 5, 63450 Hanau")


@pytest.mark.parametrize(
    "entry",
    [
        # Cached by round 10 (checked flag, no version) and by round 9's
        # downgrade (STREET at another building's point).
        {"precision": "ADDRESS", "building_checked": True},
        {"precision": "STREET", "building_checked": False},
    ],
)
def test_results_cached_under_older_checks_are_fetched_again(tmp_path, entry):
    cache = tmp_path / "geocode_cache.local.json"
    cache.write_text(
        json.dumps(
            {
                "industriestrasse 5, 63450 hanau": {
                    "lat": 50.2,
                    "lon": 8.8,
                    "provenance": "nominatim:way/1 (house)",
                    "display_name": "Industriestraße 5, 63477 Maintal",
                    **entry,
                }
            }
        ),
        encoding="utf-8",
    )
    geocoder, calls, _ = _geocoder(tmp_path, [_hit_with("5, Industriestraße, Hanau", "5")])
    result = geocoder.geocode("Industriestraße 5, 63450 Hanau")
    assert len(calls) == 1 and (result.lat, result.precision) == (50.13, "ADDRESS")


def test_tool_route_label_without_the_posting_city_is_unverified():
    """Codex round 12: "Frankfurt, Hauptstraße 5" for a Hanau posting."""
    job = SimpleNamespace(location="Hanau", company="Beispiel GmbH", description="Wir suchen.")
    check = _check(40.0, "OSRM")
    check.destination_label = "Frankfurt, Hauptstraße 5"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "DISTANCE_UNVERIFIED"
    manual = _check(40.0, "MANUAL")
    manual.destination_label = "Frankfurt, Hauptstraße 5"
    # Recorded by hand: the operator's statement (Stage 17 contract).
    assert fit.location_criterion(job, SETTINGS, _ONSITE, manual).status == "PASS"


def test_building_request_without_a_postcode_is_refused(tmp_path):
    """Codex round 13: "Industriestraße 5, Hanau" also exists in Maintal; an
    exact building needs a postcode on both sides."""
    response = _hit_with("5, Industriestraße, Maintal", house_number="5", postcode=None)
    geocoder, _, _ = _geocoder(tmp_path, [response])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("Industriestraße 5, Hanau")


def test_bad_homburg_is_not_bad_nauheim():
    """Codex round 13: a generic first word keeps the word after it."""
    job = SimpleNamespace(location="Bad Homburg", company="Beispiel GmbH", description="Wir.")
    check = _check(40.0, "OSRM")
    check.destination_label = "Bad Nauheim, Hauptstraße 5"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "DISTANCE_UNVERIFIED"
    check.destination_label = "Hauptstraße 5, 61348 Bad Homburg vor der Höhe"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "PASS"
    # "Frankfurt am Main" still matches a plain "Frankfurt" label.
    frankfurt = SimpleNamespace(location="Frankfurt am Main", company="X", description="Wir.")
    check.destination_label = "Hauptstraße 5, 60311 Frankfurt"
    assert fit.location_criterion(frankfurt, SETTINGS, _ONSITE, check).status == "PASS"


def test_frankfurt_oder_is_not_frankfurt_am_main():
    """Codex round 14: same name, other town."""
    job = SimpleNamespace(location="Frankfurt am Main", company="X", description="Wir.")
    check = _check(500.0, "OSRM")
    check.destination_label = "Frankfurt (Oder), Hauptstraße 5"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "DISTANCE_UNVERIFIED"
    check.destination_label = "Hauptstraße 5, 60311 Frankfurt am Main"
    assert fit.location_criterion(job, SETTINGS, _ONSITE, check).status == "FAIL"


def test_approximate_hit_without_postcode_in_another_town_is_refused(tmp_path):
    """Codex round 16: a street-level hit naming another town and no
    postcode is not used (a route to it could make a wrong FAIL)."""
    other = httpx.Response(
        200,
        json=[
            {
                "lat": "50.2",
                "lon": "8.8",
                "addresstype": "road",
                "class": "highway",
                "display_name": "Industriestraße, Maintal, Hessen",
                "address": {"road": "Industriestraße", "town": "Maintal"},
            }
        ],
    )
    geocoder, _, _ = _geocoder(tmp_path, [other])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("Industriestraße 5, 63450 Hanau")
    same_town = httpx.Response(
        200,
        json=[
            {
                "lat": "50.13",
                "lon": "8.92",
                "addresstype": "road",
                "class": "highway",
                "display_name": "Industriestraße, Hanau, Hessen",
                "address": {"road": "Industriestraße", "city": "Hanau"},
            }
        ],
    )
    geocoder, _, _ = _geocoder(tmp_path / "b", [same_town])
    assert geocoder.geocode("Industriestraße 5, 63450 Hanau").precision == "STREET"


def test_postcodeless_hit_in_a_same_named_town_is_refused(tmp_path):
    """Codex round 17: Frankfurt (Oder) for a Frankfurt am Main request."""
    oder = httpx.Response(
        200,
        json=[
            {
                "lat": "52.34",
                "lon": "14.55",
                "addresstype": "road",
                "class": "highway",
                "display_name": "Hauptstraße, Frankfurt (Oder), Brandenburg",
                "address": {"road": "Hauptstraße", "city": "Frankfurt (Oder)"},
            }
        ],
    )
    geocoder, _, _ = _geocoder(tmp_path, [oder])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("Hauptstraße 5, 60311 Frankfurt am Main")


def test_street_hit_cached_before_the_town_check_is_fetched_again(tmp_path):
    """Codex round 17: entries cached under check version 2 are refetched."""
    cache = tmp_path / "geocode_cache.local.json"
    cache.write_text(
        json.dumps(
            {
                "industriestrasse 5, 63450 hanau": {
                    "lat": 50.2,
                    "lon": 8.8,
                    "precision": "STREET",
                    "provenance": "nominatim:way/1 (road)",
                    "display_name": "Industriestraße, Maintal",
                    "building_checked": False,
                    "check_version": 2,
                }
            }
        ),
        encoding="utf-8",
    )
    geocoder, calls, _ = _geocoder(tmp_path, [_hit_with("5, Industriestraße, Hanau", "5")])
    result = geocoder.geocode("Industriestraße 5, 63450 Hanau")
    assert len(calls) == 1 and (result.lat, result.precision) == (50.13, "ADDRESS")


def test_frankfurt_am_main_hit_for_a_frankfurt_oder_request_is_refused(tmp_path):
    """Codex round 18: the reverse direction; the requested town keeps its
    "(Oder)" qualifier."""
    main = httpx.Response(
        200,
        json=[
            {
                "lat": "50.11",
                "lon": "8.68",
                "addresstype": "road",
                "class": "highway",
                "display_name": "Hauptstraße, Frankfurt am Main, Hessen",
                "address": {"road": "Hauptstraße", "city": "Frankfurt am Main"},
            }
        ],
    )
    geocoder, _, _ = _geocoder(tmp_path, [main])
    with pytest.raises(GeocodingUnavailableError):
        geocoder.geocode("Hauptstraße 5, 15230 Frankfurt (Oder)")
