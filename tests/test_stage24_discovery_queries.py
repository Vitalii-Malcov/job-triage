"""Stage 24: opt-in discovery search profile (never used by default)."""

from app.models.candidate_search import CandidateSearchSettings, GeoPoint
from app.services.search_strategy import (
    DISCOVERY_NEAR_TERMS,
    DISCOVERY_REMOTE_TERMS,
    DISCOVERY_REMOTE_WEEKLY_TERMS,
    bundesagentur_queries,
    discovery_queries,
)

SETTINGS = CandidateSearchSettings(
    home_city="Heusenstamm",
    home_postal_code="63150",
    commute_origin_label="Heusenstamm",
    commute_origin=GeoPoint(lat=50.0559, lon=8.8011),
    max_commute_km=100,
    commute_measure="CAR_ROUTE",
    relocation_regions=["Hessen"],
    remote_work_scope="GERMANY",
    remote_work_from_region="Hessen",
)


class _S:
    bundesagentur_search_keywords = "Python"
    bundesagentur_search_location = "Frankfurt"
    bundesagentur_search_radius_km = 25


def test_every_discovery_query_asks_for_employment_only():
    queries = discovery_queries(_S(), SETTINGS)
    assert queries and all(q.offer_type == 1 for q in queries)


def test_only_the_7_day_window_is_used_and_only_for_broad_remote_terms():
    queries = discovery_queries(_S(), SETTINGS)
    windowed = [q for q in queries if q.published_within_days is not None]
    assert {q.published_within_days for q in windowed} == {7}
    assert {q.keywords for q in windowed} == set(DISCOVERY_REMOTE_WEEKLY_TERMS)
    assert all(q.label == "discovery-remote-weekly" and q.home_office_only for q in windowed)


def test_commute_queries_reuse_the_regular_radius_and_place():
    regular = bundesagentur_queries(_S(), SETTINGS)[0]
    near = [q for q in discovery_queries(_S(), SETTINGS) if q.label == "discovery-commute"]
    assert [q.keywords for q in near] == list(DISCOVERY_NEAR_TERMS)
    assert all(q.location == regular.location and q.radius_km == regular.radius_km for q in near)


def test_remote_queries_keep_the_home_office_flag_filter_and_are_nationwide():
    remote = [q for q in discovery_queries(_S(), SETTINGS) if q.label == "discovery-remote"]
    assert [q.keywords for q in remote] == list(DISCOVERY_REMOTE_TERMS)
    assert all(q.home_office_only and q.location == "" and q.radius_km == 0 for q in remote)


def test_no_remote_queries_without_nationwide_remote_scope():
    local = SETTINGS.model_copy(update={"remote_work_scope": "UNKNOWN"})
    labels = {q.label for q in discovery_queries(_S(), local)}
    assert labels == {"discovery-commute"}


def test_unconfigured_search_gives_no_discovery_queries():
    assert discovery_queries(_S(), None) == []
    assert discovery_queries(_S(), CandidateSearchSettings()) == []


def test_no_term_targets_electrical_plc_or_sap_work():
    terms = {*DISCOVERY_NEAR_TERMS, *DISCOVERY_REMOTE_TERMS, *DISCOVERY_REMOTE_WEEKLY_TERMS}
    banned = ("sps", "plc", "sap", "elektro", "embedded", "automatisierung", "salesforce")
    assert not [t for t in terms if any(b in t.lower() for b in banned)]


def test_regular_queries_are_unchanged_by_the_discovery_profile():
    assert all(
        q.offer_type is None and q.published_within_days is None
        for q in bundesagentur_queries(_S(), SETTINGS)
    )
