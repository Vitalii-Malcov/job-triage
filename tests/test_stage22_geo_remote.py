"""Stage 22 P1: structured BA facts, preliminary geography, home-office search."""

import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.agents.geo_estimate import commutable_state, estimate_commute, state_key, straight_line_km
from app.agents.workplace_address import stated_workplace_towns
from app.collectors.bundesagentur import BundesagenturCollector, _map_posting, structured_facts
from app.db.models import Base, JobRecord
from app.db.repositories import upsert_job
from app.models.candidate_search import CandidateSearchSettings, GeoPoint
from app.models.job import Job, JobScore
from app.services import job_fit_assessment as fit
from app.services.search_strategy import bundesagentur_queries

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEUSENSTAMM = GeoPoint(lat=50.0559, lon=8.8011)
SETTINGS = CandidateSearchSettings(
    home_city="Heusenstamm",
    home_postal_code="63150",
    commute_origin_label="Heusenstamm",
    commute_origin=HEUSENSTAMM,
    max_commute_km=100,
    commute_measure="CAR_ROUTE",
    relocation_regions=["Hessen"],
    remote_work_scope="GERMANY",
    remote_work_from_region="Hessen",
)
CITIES = {
    "Frankfurt am Main": (50.1109, 8.6821, "HESSEN"),
    "Offenbach am Main": (50.0956, 8.7761, "HESSEN"),
    "Hanau": (50.1328, 8.9169, "HESSEN"),
    "Darmstadt": (49.8728, 8.6512, "HESSEN"),
    "Gießen": (50.5841, 8.6784, "HESSEN"),
    "Wiesbaden": (50.0782, 8.2398, "HESSEN"),
    "Mainz": (49.9929, 8.2473, "RHEINLAND_PFALZ"),
    "Würzburg": (49.7913, 9.9534, "BAYERN"),
    "Koblenz": (50.3569, 7.5890, "RHEINLAND_PFALZ"),
    "Stuttgart": (48.7758, 9.1829, "BADEN_WUERTTEMBERG"),
    "Kassel": (51.3127, 9.4797, "HESSEN"),
    "München": (48.1351, 11.5820, "BAYERN"),
    "Berlin": (52.5200, 13.4050, "BERLIN"),
}


def _posting(**overrides):
    posting = {
        "referenznummer": "10001-1000911926-S",
        "stellenangebotsTitel": "Python Entwickler",
        "firma": "ACME GmbH",
        "stellenlokationen": [
            {
                "adresse": {"plz": "60329", "ort": "Frankfurt am Main", "region": "HESSEN"},
                "breite": 50.1061435,
                "laenge": 8.6686369,
            }
        ],
        "homeofficemoeglich": True,
        "homeofficetyp": "ANGABE_IN_PROZENT",
        "homeofficeprozent": 60,
        "quereinstiegGeeignet": True,
    }
    posting.update(overrides)
    return posting


# --- collector ---------------------------------------------------------------------


def test_structured_facts_are_mapped():
    job = _map_posting(_posting())
    assert (job.workplace_lat, job.workplace_lon) == (50.1061435, 8.6686369)
    assert (job.workplace_postal_code, job.workplace_region, job.workplace_count) == (
        "60329",
        "HESSEN",
        1,
    )
    assert job.home_office_possible is True and job.home_office_type == "ANGABE_IN_PROZENT"
    assert job.home_office_percent == 60 and job.quereinstieg_suitable is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"stellenlokationen": [{"breite": "50.1", "laenge": 8.6, "adresse": "x"}]},
        {"stellenlokationen": [{"breite": 95.0, "laenge": 8.6}]},
        {"stellenlokationen": "Frankfurt"},
        {"stellenlokationen": [None]},
        {"homeofficeprozent": 150, "homeofficemoeglich": "ja", "homeofficetyp": 7},
        {"quereinstiegGeeignet": "true"},
        {"stellenlokationen": [{"adresse": {"plz": "6" * 20, "region": "R" * 80}}]},
    ],
)
def test_odd_structured_values_become_none_and_never_drop_the_posting(overrides):
    job = _map_posting(_posting(**overrides))
    assert job is not None and job.source_reference == "10001-1000911926-S"
    facts = structured_facts(_posting(**overrides))
    for value in facts.values():
        assert value is not None


def test_several_locations_are_counted():
    location = _posting()["stellenlokationen"][0]
    assert _map_posting(_posting(stellenlokationen=[location, location])).workplace_count == 2


# --- persistence and migration -------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'geo.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def test_facts_are_stored_refreshed_and_kept_when_omitted(db):
    score = JobScore(score=50, recommendation="MAYBE")
    job = _map_posting(_posting())
    record, _ = upsert_job(db, job, score)
    assert record.workplace_region == "HESSEN" and record.home_office_percent == 60

    changed = _map_posting(_posting(homeofficeprozent=20, homeofficemoeglich=False))
    record, created = upsert_job(db, changed, score)
    assert not created and record.home_office_percent == 20
    assert record.home_office_possible is False

    omitted = _map_posting(_posting(stellenlokationen=[], homeofficeprozent=None))
    omitted = omitted.model_copy(update={"home_office_possible": None, "home_office_type": None})
    record, _ = upsert_job(db, omitted, score)
    assert record.workplace_lat == 50.1061435 and record.home_office_percent == 20
    assert record.home_office_possible is False and record.workplace_count == 0


def test_migration_adds_and_drops_the_fact_columns(tmp_path):
    path = tmp_path / "m.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    upgrade(cfg, "d5f1a7c3e9b2")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (fingerprint, source, title, company, location, url, "
                "description, skills_json, data_confidence, must_have_skills_json, "
                "nice_to_have_skills_json, score, recommendation, status, first_seen_at, "
                "last_seen_at, source_reference) VALUES ('f', 'bundesagentur', 'T', 'C', '', "
                "'https://x.de', '', '[]', 0.5, '[]', '[]', 1, 'SKIP', 'NEW', '2026-10-30', "
                "'2026-10-30', '10001-1-S')"
            )
        )
    upgrade(cfg, "e3b7c9d1f4a6")
    columns = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert {"workplace_lat", "home_office_possible", "quereinstieg_suitable"} <= columns
    downgrade(cfg, "d5f1a7c3e9b2")
    columns = {c["name"] for c in inspect(engine).get_columns("jobs")}
    assert "workplace_lat" not in columns and "source_reference" in columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT source_reference FROM jobs")).scalar_one() == "10001-1-S"
    engine.dispose()


# --- geography -------------------------------------------------------------------------


def test_straight_line_distance_is_plausible():
    lat, lon, _ = CITIES["Frankfurt am Main"]
    assert 8 < straight_line_km(HEUSENSTAMM.lat, HEUSENSTAMM.lon, lat, lon) < 12
    lat, lon, _ = CITIES["Berlin"]
    assert 410 < straight_line_km(HEUSENSTAMM.lat, HEUSENSTAMM.lon, lat, lon) < 440


@pytest.mark.parametrize(
    ("city", "band"),
    [
        ("Frankfurt am Main", "WITHIN"),
        ("Offenbach am Main", "WITHIN"),
        ("Hanau", "WITHIN"),
        ("Darmstadt", "WITHIN"),
        ("Gießen", "WITHIN"),
        ("Wiesbaden", "WITHIN"),
        ("Mainz", "WITHIN"),
        ("Würzburg", "NEAR_LIMIT"),
        ("Koblenz", "NEAR_LIMIT"),
        ("Stuttgart", "BEYOND"),
        ("Kassel", "BEYOND"),
        ("München", "BEYOND"),
        ("Berlin", "BEYOND"),
    ],
)
def test_estimate_bands_from_heusenstamm(city, band):
    lat, lon, _ = CITIES[city]
    assert estimate_commute(HEUSENSTAMM.lat, HEUSENSTAMM.lon, lat, lon, 100).band == band


def test_neighbour_states_are_commutable_others_not():
    assert commutable_state("HESSEN") and commutable_state("RHEINLAND_PFALZ")
    assert commutable_state("BAYERN") and commutable_state("BADEN_WUERTTEMBERG")
    assert commutable_state("BERLIN") is False and commutable_state("SACHSEN") is False
    assert commutable_state(None) is None
    assert state_key("BADEN_WUERTTEMBERG") == state_key("Baden-Württemberg")


def _job(city="Darmstadt", **overrides):
    lat, lon, region = CITIES[city]
    values = dict(
        id=1,
        fingerprint="f" * 64,
        source="bundesagentur",
        title="Python Backend Developer (m/w/d)",
        company="Example GmbH",
        location=city,
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1-S",
        description="",
        score=60,
        recommendation="APPLY",
        workplace_lat=lat,
        workplace_lon=lon,
        workplace_region=region,
        workplace_count=1,
    )
    values.update(overrides)
    return JobRecord(**values)


def _location(job, settings=SETTINGS):
    return fit.location_criterion(job, settings, fit.remote_criterion("", settings, job), None)


@pytest.mark.parametrize(
    ("city", "status"),
    [
        ("Frankfurt am Main", "ESTIMATE_WITHIN"),
        ("Gießen", "ESTIMATE_WITHIN"),
        ("Mainz", "ESTIMATE_WITHIN"),
        ("Würzburg", "ESTIMATE_NEAR_LIMIT"),
        ("Stuttgart", "ESTIMATE_BEYOND"),
        ("Berlin", "ESTIMATE_BEYOND"),
        ("Kassel", "ESTIMATE_BEYOND_RELOCATION"),  # Hessen: relocation would be possible
    ],
)
def test_location_estimate_from_ba_coordinates(city, status):
    criterion = _location(_job(city))
    assert criterion.status == status
    assert "straight line" in criterion.reason and "car route" in criterion.reason


def test_without_coordinates_or_origin_the_old_rule_applies():
    assert _location(_job(workplace_lat=None, workplace_lon=None)).status == "DISTANCE_UNVERIFIED"
    no_origin = SETTINGS.model_copy(update={"commute_origin": None})
    assert _location(_job(), no_origin).status == "DISTANCE_UNVERIFIED"


@pytest.mark.parametrize(
    "overrides",
    [
        {"company": "Top Personalservice GmbH"},  # agency office, not the workplace
        {"workplace_count": 3},
        {"description": "Ihr Arbeitsort: Bruchsal. Wir bieten ..."},
        {"description": "Die Stelle ist ausschließlich in Hannover zu besetzen."},
    ],
)
def test_a_doubtful_workplace_is_location_unverified(overrides):
    criterion = _location(_job("Wiesbaden", **overrides))
    assert criterion.status == "LOCATION_UNVERIFIED"
    assert "unverified" in criterion.reason


def test_a_workplace_naming_its_own_town_is_not_doubted():
    job = _job("Darmstadt", description="Arbeitsort: Darmstadt, hybrides Arbeiten")
    assert _location(job).status == "ESTIMATE_WITHIN"
    assert (
        stated_workplace_towns("an unseren Standorten in Frankfurt, Standort Rhein-Main") == set()
    )


def test_a_stored_car_route_still_decides():
    from tests.test_stage17_job_fit import _check

    criterion = fit.location_criterion(
        _job("Stuttgart", location="Darmstadt"), SETTINGS, fit.remote_criterion("", SETTINGS), None
    )
    assert criterion.status == "ESTIMATE_BEYOND"
    routed = _job("Darmstadt")
    check = _check(85)
    check.origin_label, check.origin_lat, check.origin_lon = "Heusenstamm", 50.0559, 8.8011
    assert (
        fit.location_criterion(routed, SETTINGS, fit.remote_criterion("", SETTINGS), check).status
        == "PASS"
    )


def _assessment(location_status, skills="STRONG"):
    return fit.FitAssessment(
        job_id=1,
        company="C",
        title="T",
        criteria=[
            fit.Criterion("skills", skills, "x"),
            fit.Criterion("role", "PRIMARY", "x"),
            fit.Criterion("location", location_status, "x"),
            fit.Criterion("entry_level", "ENTRY_LEVEL", "x"),
            fit.Criterion("duplicate", "CLEAR", "x"),
        ],
    )


def test_estimate_within_ranks_but_never_clears_the_pre_bewerbung_check():
    assessment = _assessment("ESTIMATE_WITHIN")
    assert assessment.fit_tier == "GOOD_MATCH"
    assert assessment.decision == "NEEDS_VERIFICATION"  # the car route must still be checked
    assert _assessment("PASS").decision == "CANDIDATE"


@pytest.mark.parametrize(
    ("status", "tier"),
    [
        ("ESTIMATE_BEYOND", "NOT_SUITABLE"),
        ("ESTIMATE_NEAR_LIMIT", "NEEDS_OPERATOR"),
        ("ESTIMATE_BEYOND_RELOCATION", "NEEDS_OPERATOR"),
        ("LOCATION_UNVERIFIED", "NEEDS_OPERATOR"),
        ("DISTANCE_UNVERIFIED", "NEEDS_OPERATOR"),
    ],
)
def test_other_estimates_keep_the_operator_in_the_loop(status, tier):
    assessment = _assessment(status)
    assert assessment.fit_tier == tier
    assert assessment.decision == "NEEDS_VERIFICATION"


# --- remote ----------------------------------------------------------------------------


def _remote(text_="", **facts):
    return fit.remote_criterion(text_, SETTINGS, _job(**facts))


def test_ba_home_office_flag_fills_only_a_silent_text():
    assert _remote(home_office_possible=True, home_office_type="NACH_VEREINBARUNG").status == (
        "PARTIAL_REMOTE"
    )
    assert "up to 100 %" in _remote(home_office_possible=True, home_office_percent=100).reason
    assert _remote(home_office_possible=False).status == "ONSITE"
    assert _remote(home_office_possible=None).status == "UNKNOWN"
    # the text wins: hybrid stays hybrid, fully remote stays a text-only verdict
    assert (
        _remote("Hybrides Arbeiten, 2 Tage vor Ort", home_office_possible=False).status == "HYBRID"
    )
    assert _remote("100% Remote, deutschlandweit", home_office_possible=False).status == (
        "COMPATIBLE"
    )


def test_the_remote_search_is_nationwide_with_the_home_office_flag():
    class _S:
        bundesagentur_search_keywords = "Python"

    commute, remote = bundesagentur_queries(_S(), SETTINGS)
    assert remote.home_office_only and remote.working_time == "" and remote.location == ""
    assert not commute.home_office_only


def _page(postings, total):
    return {"ergebnisliste": postings, "maxErgebnisse": total}


def test_the_collector_keeps_only_home_office_postings_and_reports_truncation():
    home = _posting()
    office = _posting(referenznummer="10001-2-S", homeofficemoeglich=False)
    silent = _posting(referenznummer="10001-3-S")
    silent.pop("homeofficemoeglich")
    pages = []

    def handler(request):
        pages.append(int(request.url.params["page"]))
        assert "arbeitszeit" not in request.url.params
        return httpx.Response(200, json=_page([home, office, silent], 9999))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = BundesagenturCollector(
                api_key="k", home_office_only=True, max_pages=2, page_size=3, http_client=client
            )
            return collector, await collector.fetch()

    collector, jobs = asyncio.run(go())
    assert pages == [1, 2]
    assert [j.source_reference for j in jobs] == ["10001-1000911926-S"] * 2
    assert collector.skipped_not_home_office == 4 and collector.truncated
    assert collector.skipped_invalid_count == 0


def test_a_complete_result_is_not_truncated():
    def handler(request):
        return httpx.Response(200, json=_page([_posting()], 1))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = BundesagenturCollector(api_key="k", http_client=client)
            return collector, await collector.fetch()

    collector, jobs = asyncio.run(go())
    assert len(jobs) == 1 and not collector.truncated and collector.skipped_not_home_office == 0


def test_an_empty_remote_query_is_reported_not_mistaken_for_no_jobs(db, monkeypatch, caplog):
    from app.services import collector_runner

    class _Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.skipped_invalid_count = 0
            self.skipped_not_home_office = 37 if kwargs["home_office_only"] else 0
            self.truncated = False

        async def fetch(self):
            return [] if self.kwargs["home_office_only"] else [_map_posting(_posting())]

        async def fetch_detail(self, ref):
            return None

    monkeypatch.setattr(collector_runner, "BundesagenturCollector", _Fake)
    monkeypatch.setattr(collector_runner, "_candidate_search_settings", lambda _db: SETTINGS)
    from tests.test_stage17_job_fit import _settings

    settings = _settings(bundesagentur_api_key="real-looking-key-123")
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(collector_runner.run_bundesagentur(db, settings))
    assert result["fetched"] == 1
    assert "bundesagentur_remote_query_empty term=Python skipped_not_home_office=37" in caplog.text


# --- matcher tiers (Stage 22 P1) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "status"),
    [
        ("Aktuar (m/w/d)", "OFF_ROLE"),
        ("Elektroingenieur (m/w/d)", "OFF_ROLE"),
        ("Python Developer (m/w/d)", "OTHER"),  # unconfigured but a software role
        ("Data Analyst", "OTHER"),
    ],
)
def test_explicitly_unrelated_professions_are_off_role(title, status):
    assert (
        fit.role_criterion(
            title, CandidateSearchSettings(primary_roles=["Backend Developer"])
        ).status
        == status
    )


@pytest.mark.parametrize(
    ("title", "status"),
    [
        ("Senior Java Developer", "SENIOR_TITLE"),
        ("Lead Software Architect", "SENIOR_TITLE"),
        ("Junior Python Developer", "ENTRY_LEVEL"),
        ("Python Developer", "OPEN"),
    ],
)
def test_a_senior_title_is_its_own_status(title, status):
    assert fit.entry_level_criterion(title, "").status == status


@pytest.mark.parametrize(
    ("text_", "quereinstieg", "status"),
    [
        ("Abgeschlossenes Studium der Informatik.", None, "IT_DEGREE_REQUIRED"),
        ("Bachelor in Computer Science", None, "IT_DEGREE_REQUIRED"),
        (
            "Studium der Informatik oder eine vergleichbare Qualifikation.",
            None,
            "DEGREE_OR_EQUIVALENT",
        ),
        ("Studium (Informatik, Ingenieurwesen oder ähnlich).", None, "DEGREE_OR_EQUIVALENT"),
        ("Informatikstudium oder Ausbildung zum Fachinformatiker.", None, "DEGREE_OR_EQUIVALENT"),
        ("Ein Studium der Informatik ist von Vorteil.", None, "OPTIONAL"),
        ("Quereinsteiger willkommen; Studium der Informatik.", None, "QUEREINSTIEG"),
        ("Python, SQL, Docker.", None, "NO_REQUIREMENT"),
        ("Abgeschlossenes Studium der Informatik.", True, "QUEREINSTIEG"),
    ],
)
def test_education_requirement(text_, quereinstieg, status):
    assert fit.education_criterion(text_, quereinstieg).status == status


@pytest.mark.parametrize(
    ("text_", "strong"),
    [
        ("Verhandlungssichere Deutschkenntnisse (zwingend).", True),
        ("Deutsch fließend in Wort und Schrift erforderlich.", True),
        ("Deutsch auf Muttersprachniveau.", True),
        ("Deutsch C1 erforderlich.", True),
        ("Sehr gute Deutschkenntnisse.", False),
        ("Gute Deutschkenntnisse.", False),
        ("Fließende Deutschkenntnisse wünschenswert.", False),  # optional
    ],
)
def test_strong_german_wording(text_, strong):
    assert fit.german_requirement_is_strong(text_) is strong


BASE = {
    "skills": "STRONG",
    "role": "PRIMARY",
    "experience": "UNKNOWN",
    "language": "OK",
    "location": "PASS",
    "remote": "HYBRID",
    "salary": "MATCH",
    "entry_level": "ENTRY_LEVEL",
    "education": "NO_REQUIREMENT",
    "start_date": "OK",
    "duplicate": "CLEAR",
}


def _tier_of(*, strong=False, basis="INFERRED_REQUIREMENT", missing=False, years=None, **statuses):
    values = {**BASE, **statuses}
    assessment = fit.FitAssessment(
        job_id=1,
        company="C",
        title="T",
        criteria=[fit.Criterion(n, s, "r") for n, s in values.items()],
        german_requirement_basis=basis,
        german_level_confirmed=True,
        experience_missing_in_profile=missing,
        german_requirement_strong=strong,
        required_years=years,
    )
    return assessment.fit_tier


@pytest.mark.parametrize(
    ("options", "tier"),
    [
        ({}, "GOOD_MATCH"),
        ({"role": "OFF_ROLE"}, "NOT_SUITABLE"),
        ({"entry_level": "SENIOR_TITLE"}, "NOT_SUITABLE"),
        ({"language": "MAJOR_GAP", "strong": True}, "NOT_SUITABLE"),  # "verhandlungssicher"
        ({"language": "MAJOR_GAP", "basis": "EXPLICIT_REQUIREMENT"}, "NOT_SUITABLE"),  # "C1"
        ({"language": "GAP_TO_VERIFY"}, "STRETCH"),  # "sehr gute"
        ({"language": "GAP", "basis": "EXPLICIT_REQUIREMENT"}, "STRETCH"),  # B2 with B1
        ({"experience": "GAP", "missing": True, "years": 3}, "NOT_SUITABLE"),
        ({"experience": "GAP", "years": 3}, "STRETCH"),  # no analysis says MISSING
        ({"education": "IT_DEGREE_REQUIRED"}, "STRETCH"),
        ({"skills": "WEAK"}, "STRETCH"),
        ({"education": "DEGREE_OR_EQUIVALENT"}, "GOOD_MATCH"),
        ({"skills": "PARTIAL"}, "PARTIAL_MATCH"),
        ({"role": "OTHER"}, "PARTIAL_MATCH"),
        ({"location": "LOCATION_UNVERIFIED", "education": "IT_DEGREE_REQUIRED"}, "NEEDS_OPERATOR"),
    ],
)
def test_tier_rules(options, tier):
    assert _tier_of(**options) == tier


def test_off_role_keyword_overlap_never_becomes_good_match():
    # "Aktuar" postings list Python and SQL: strong skills, still not a fit
    assert _tier_of(skills="STRONG", role="OFF_ROLE") == "NOT_SUITABLE"


def test_the_cache_accepts_stretch_after_the_migration(tmp_path):
    path = tmp_path / "c.db"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    insert_job = (
        "INSERT INTO jobs (id, fingerprint, source, title, company, location, url, description, "
        "skills_json, data_confidence, must_have_skills_json, nice_to_have_skills_json, score, "
        "recommendation, status, first_seen_at, last_seen_at) VALUES (1, 'f', 's', 'T', 'C', '', "
        "'https://x.de', '', '[]', 0.5, '[]', '[]', 1, 'SKIP', 'NEW', '2026-10-30', '2026-10-30')"
    )
    insert_row = (
        "INSERT INTO job_fit_assessments (job_id, input_key, rules_hash, fit_tier, decision, "
        "criteria_json, tier_reasons_json, work_mode, applied, computed_at) VALUES (1, 'k', 'r', "
        ":tier, 'CANDIDATE', '[]', '[]', 'UNKNOWN', 0, '2026-10-30')"
    )
    with engine.begin() as conn:
        conn.execute(text(insert_job))
        conn.execute(text(insert_row), {"tier": "STRETCH"})
    downgrade(cfg, "d5f1a7c3e9b2")  # cache recreated with the old tier list (derived rows)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM job_fit_assessments")).scalar_one() == 0
        with pytest.raises(Exception):  # noqa: B017 -- the old CHECK refuses STRETCH
            conn.execute(text(insert_row), {"tier": "STRETCH"})
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
    engine.dispose()


@pytest.mark.parametrize(
    ("title", "status"),
    [
        ("PhD Candidate / Doctoral Researcher Lipidomics", "OFF_ROLE"),
        ("Doktorand/in (m/w/d) Raumfahrtphysik", "OFF_ROLE"),
        ("Trainer für Schülerkurse in Teilzeit", "OFF_ROLE"),
        ("Office Manager (m/f/x)", "OFF_ROLE"),
        ("Lagerist (m/w/d)", "NO_IT_SIGNAL"),
        ("Junior Sales Manager", "OFF_ROLE"),
        ("Account Manager IT", "OTHER"),  # an IT word: reviewable, not off-role
        ("IT-Administrator im Bankenumfeld", "OTHER"),  # IT support/admin is an allowed target
        ("Trainee IT (m/w/d)", "OTHER"),
        ("Versuchsingenieur (m/w/d)", "OTHER"),
        ("Fachinformatiker Anwendungsentwicklung", "OTHER"),
    ],
)
def test_role_gate_after_the_quality_check(title, status):
    settings = CandidateSearchSettings(primary_roles=["Backend Developer"])
    assert fit.role_criterion(title, settings).status == status


@pytest.mark.parametrize(
    ("posting_type", "status"),
    [
        ("ARBEIT", "EMPLOYMENT"),
        ("PRAKTIKUM_TRAINEE", "TRAINEE_OR_INTERNSHIP"),
        ("SELBSTAENDIGKEIT", "SELF_EMPLOYMENT"),
        ("AUSBILDUNG", "APPRENTICESHIP"),
        (None, "UNKNOWN"),
    ],
)
def test_posting_type_criterion(posting_type, status):
    assert fit.posting_type_criterion(posting_type).status == status


@pytest.mark.parametrize(
    ("options", "tier"),
    [
        ({"role": "NO_IT_SIGNAL"}, "NEEDS_OPERATOR"),
        ({"posting_type": "SELF_EMPLOYMENT"}, "NOT_SUITABLE"),
        ({"posting_type": "APPRENTICESHIP"}, "NOT_SUITABLE"),
        ({"posting_type": "TRAINEE_OR_INTERNSHIP"}, "GOOD_MATCH"),  # trainee is a target
        ({"entry_level": "STUDENT_JOB"}, "STRETCH"),
    ],
)
def test_posting_type_and_student_tiers(options, tier):
    assert _tier_of(**options) == tier


def test_a_werkstudent_title_is_a_student_job():
    assert fit.entry_level_criterion("Werkstudent Data Analytics", "").status == "STUDENT_JOB"


# --- Codex P1 round 1 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "1st Level Service Desk",
        "Trainee Anwendungsbetreuung",
        "IT-Support für Vertriebssoftware",
        "Software Developer – Sales Applications",
    ],
)
def test_support_trainee_and_mixed_titles_are_never_off_role(title):
    status = fit.role_criterion(title, CandidateSearchSettings(primary_roles=["Backend"])).status
    assert status == "OTHER"


def test_a_title_outside_the_vocabulary_asks_the_operator_not_a_barrier():
    status = fit.role_criterion("Lagerist (m/w/d)", CandidateSearchSettings()).status
    assert status == "NOT_CONFIGURED" or status == "NO_IT_SIGNAL"
    assert _tier_of(role="NO_IT_SIGNAL") == "NEEDS_OPERATOR"


@pytest.mark.parametrize(
    "title", ["Trainee IT Security Expert", "IT-Support Experte (Quereinstieg m/w/d)"]
)
def test_entry_wording_beats_the_seniority_classifier(title):
    assert fit.entry_level_criterion(title, "").status != "SENIOR_TITLE"


@pytest.mark.parametrize(
    ("text_", "status"),
    [
        ("Ein Studium der Informatik ist nicht erforderlich.", "NO_REQUIREMENT"),
        (
            "Studium der Informatik (z. B. Bachelor) oder vergleichbare Qualifikation.",
            "DEGREE_OR_EQUIVALENT",
        ),
    ],
)
def test_education_respects_negation_and_abbreviations(text_, status):
    assert fit.education_criterion(text_).status == status


@pytest.mark.parametrize(
    "text_",
    [
        "Verhandlungssichere Deutschkenntnisse zwingend erforderlich.",
        "Deutsch fließend in Wort und Schrift.",
        "Deutsch auf Muttersprachniveau.",
    ],
)
def test_strong_german_wording_is_a_barrier_end_to_end(text_):
    language = fit.language_criterion(text_, "B1")
    assert language.status == "MAJOR_GAP"
    criteria = {**BASE, "language": language.status}
    assessment = fit.FitAssessment(
        job_id=1,
        company="C",
        title="T",
        criteria=[fit.Criterion(n, s_, "r") for n, s_ in criteria.items()],
        german_requirement_basis="INFERRED_REQUIREMENT",
        german_level_confirmed=True,
        german_requirement_strong=fit.german_requirement_is_strong(text_),
    )
    assert assessment.fit_tier == "NOT_SUITABLE"
    assert fit.language_criterion("Sehr gute Deutschkenntnisse.", "B1").status == "GAP_TO_VERIFY"


def test_optional_experience_is_never_a_barrier():
    from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch

    def match(importance):
        requirement = RequirementMatch.model_construct(
            requirement="3 years of experience with Python",
            requirement_type="EXPERIENCE",
            importance=importance,
            match_status="MISSING",
        )
        return CandidateJobMatch.model_construct(missing_requirements=[requirement])

    assert fit._experience_missing(match("REQUIRED"))
    assert not fit._experience_missing(match("PREFERRED"))


@pytest.mark.parametrize(
    ("description", "location", "status"),
    [
        ("Arbeitsort: 60311 Frankfurt am Main", "Berlin", "LOCATION_UNVERIFIED"),
        ("Arbeitsort: Hauptstraße 5, 64283 Darmstadt", "Darmstadt", "ESTIMATE_WITHIN"),
    ],
)
def test_address_shaped_workplace_statements(description, location, status):
    city = "Berlin" if location == "Berlin" else "Darmstadt"
    job = _job(city, location=location, description=description)
    assert _location(job).status == status


# --- Codex P1 round 2 --------------------------------------------------------------------


def test_english_fluency_next_to_german_is_not_german_fluency():
    text_ = "Sehr gute Deutschkenntnisse und fliessende Englischkenntnisse erforderlich."
    assert fit.language_criterion(text_, "B1").status == "GAP_TO_VERIFY"
    assert not fit.german_requirement_is_strong(text_)


@pytest.mark.parametrize("town", ["69115 Heidelberg", "Heidelberg", "96047 Bamberg"])
def test_towns_ending_like_streets_are_towns(town):
    job = _job("Berlin", location="Berlin", description=f"Arbeitsort: {town}")
    assert _location(job).status == "LOCATION_UNVERIFIED"


def test_teaching_wins_over_a_configured_role():
    settings = CandidateSearchSettings(primary_roles=["Python Developer"])
    assert fit.role_criterion("Dozent Python Developer", settings).status == "OFF_ROLE"


def test_a_leading_kein_negates_the_degree_requirement():
    assert fit.education_criterion("Kein Studium der Informatik erforderlich.").status == (
        "NO_REQUIREMENT"
    )


# --- Codex P1 round 3 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text_",
    [
        "Sehr gute Deutschkenntnisse mit fliessenden Englischkenntnissen erforderlich.",
        "Sehr gute Deutschkenntnisse, fliessendes Deutsch wuenschenswert.",
    ],
)
def test_strong_wording_comes_from_the_selected_german_requirement(text_):
    assert fit.language_criterion(text_, "B1").status == "GAP_TO_VERIFY"
    assert not fit.german_requirement_is_strong(text_)


def test_assessment_uses_the_analysis_of_the_current_posting(db):
    from app.db.candidate_profile_repository import get_or_create_candidate_profile
    from app.services.candidate_preparation import prepare_candidate_job_match

    get_or_create_candidate_profile(db)
    db.commit()
    score = JobScore(score=50, recommendation="MAYBE")
    job = Job(
        source="bundesagentur",
        title="Python Developer",
        company="ACME",
        url="https://example.com/a",
        description="Version A: Python, SQL.",
    )
    record, _ = upsert_job(db, job, score)
    prepare_candidate_job_match(db, record.id, force_recompute=False)  # analysis of A
    upsert_job(db, job.model_copy(update={"description": "Version B: Java, Kotlin."}), score)
    prepare_candidate_job_match(db, record.id, force_recompute=False)  # newest row: B
    upsert_job(db, job, score)  # the posting is back to A
    assessment = fit.assess_job(db, record.id)
    assert assessment.criterion("skills").status != "NOT_EVALUATED"


# --- Codex P1 round 4 --------------------------------------------------------------------


def test_fluency_wording_wins_independent_of_sentence_order():
    first = "Sehr gute Deutschkenntnisse erforderlich. Fliessende Deutschkenntnisse erforderlich."
    second = "Fliessende Deutschkenntnisse erforderlich. Sehr gute Deutschkenntnisse erforderlich."
    assert fit.language_criterion(first, "B1").status == "MAJOR_GAP"
    assert fit.language_criterion(second, "B1").status == "MAJOR_GAP"


def test_the_cache_key_covers_every_analysis_row(db):
    from app.db.candidate_profile_repository import get_or_create_candidate_profile
    from app.services import fit_cache
    from app.services.candidate_preparation import prepare_candidate_job_match

    get_or_create_candidate_profile(db)
    db.commit()
    score = JobScore(score=50, recommendation="MAYBE")
    job = Job(source="bundesagentur", title="Dev", company="ACME", url="https://example.com/k")
    record, _ = upsert_job(db, job, score)
    prepare_candidate_job_match(db, record.id, force_recompute=False)
    one = fit_cache.latest_match_keys(db, [record.id])[record.id]
    upsert_job(db, job.model_copy(update={"description": "changed"}), score)
    prepare_candidate_job_match(db, record.id, force_recompute=False)
    two = fit_cache.latest_match_keys(db, [record.id])[record.id]
    assert one != two and two.startswith(one + ";")  # an older row stays in the key
