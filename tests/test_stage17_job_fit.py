"""Stage 17C: independent fit criteria, car routes, search strategy, guard.

Throwaway SQLite databases; routing and BA HTTP are mocked -- no network.
"""

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from alembic.command import upgrade
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.agents.job_fit_signals import (
    classify_remote,
    entry_level_signals,
    extract_german_requirement,
    extract_salary,
)
from app.collectors.bundesagentur import BundesagenturCollector
from app.core.config import Settings
from app.db.base import Base
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.candidate_search_repository import apply_config_plan, plan_config_change
from app.db.job_commute_repository import CommuteCheckInputError, record_commute_check
from app.db.models import CandidateProfileRecord, JobCommuteCheckRecord, JobRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.candidate_search import CandidateConfigSpec, CandidateSearchSettings, GeoPoint
from app.providers.routing.osrm import (
    CarRoute,
    OsrmRouteProvider,
    RouteUnavailableError,
    straight_line_km,
)
from app.services import job_fit_assessment as fit
from app.services.application_guard import AlreadyAppliedError, ensure_not_already_applied
from app.services.application_tracker import ApplicationTracker
from app.services.search_strategy import ba_radius_for, bundesagentur_queries
from scripts import stage17_job_fit as cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
ORIGIN = GeoPoint(lat=50.1, lon=8.75)

SETTINGS = CandidateSearchSettings(
    home_city="Musterstadt",
    home_postal_code="12345",
    commute_origin_label="Musterstadt Bahnhof",
    commute_origin=ORIGIN,
    max_commute_km=100,
    commute_measure="CAR_ROUTE",
    relocation_regions=["Hessen"],
    remote_work_scope="GERMANY",
    remote_work_from_region="Hessen",
    minimum_salary=45000,
    maximum_salary=50000,
    salary_negotiable=True,
    earliest_start_date=date(2027, 3, 1),
)


# --- signals ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "mode", "region"),
    [
        ("100% Remote innerhalb Deutschlands möglich", "FULLY_REMOTE", None),
        ("Full Remote aus ganz Deutschland", "FULLY_REMOTE", None),
        ("Remote (deutschlandweit)", "FULLY_REMOTE", None),
        ("100% remote, nur aus Bayern", "FULLY_REMOTE", "Bayern"),
        ("Bis zu 100% Homeoffice möglich", "PARTIAL_REMOTE", None),
        ("Hybrid: 2 Tage im Büro", "HYBRID", None),
        ("Homeoffice nach Absprache", "PARTIAL_REMOTE", None),
        ("Kein Homeoffice möglich", "ONSITE", None),
        ("Wir bieten flexible Arbeitszeiten", "UNKNOWN", None),
    ],
)
def test_classify_remote(text, mode, region):
    signal = classify_remote(text)
    assert (signal.mode, signal.region_restriction) == (mode, region)


@pytest.mark.parametrize(
    ("text", "level", "mandatory"),
    [
        ("Deutsch auf C1-Niveau zwingend erforderlich.", "C1", True),
        ("Deutschkenntnisse (B2) wünschenswert.", "B2", False),
        ("Sehr gute Deutschkenntnisse", "C1", True),
        ("Sehr gute Englisch- und gute Deutschkenntnisse.", "B2", True),
        ("Fließend Deutsch in Wort und Schrift.", "C1", True),
        ("Deutsch: Grundkenntnisse ausreichend", "A2", True),
        ("Standort in Deutschland, gute Anbindung.", None, None),
    ],
)
def test_extract_german_requirement(text, level, mandatory):
    requirement = extract_german_requirement(text)
    assert (requirement.level, requirement.mandatory) == (level, mandatory)


@pytest.mark.parametrize(
    ("text", "low", "high", "period", "tariff"),
    [
        ("Gehalt: 45.000 - 55.000 € brutto/Jahr", 45000, 55000, "YEAR", False),
        ("3.500 € brutto monatlich", 42000, 42000, "MONTH", False),
        ("ab 50k EUR p.a.", 50000, 50000, "YEAR", False),
        ("Vergütung nach TV-L E9", None, None, None, True),
        ("Stundenlohn 15 €", None, None, None, False),
    ],
)
def test_extract_salary(text, low, high, period, tariff):
    info = extract_salary(text)
    assert (info.yearly_min, info.yearly_max, info.period, info.tariff) == (
        low,
        high,
        period,
        tariff,
    )


def test_entry_level_signals():
    senior = entry_level_signals("Senior Python Developer", "mindestens 5 Jahre Berufserfahrung")
    assert senior.negative == ["senior"] and senior.required_years == 5
    junior = entry_level_signals("Junior Backend Developer", "Berufseinsteiger willkommen")
    assert "junior" in junior.positive and not junior.negative


# --- criteria (pure) -----------------------------------------------------------------


def _job(**overrides) -> JobRecord:
    values = dict(
        id=1,
        fingerprint="f" * 64,
        source="bundesagentur",
        title="Python Backend Developer (m/w/d)",
        company="Example GmbH",
        location="Darmstadt",
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-1-S",
        description="",
        score=60,
        recommendation="APPLY",
    )
    values.update(overrides)
    return JobRecord(**values)


def _check(km: float, region: str | None = None) -> JobCommuteCheckRecord:
    return JobCommuteCheckRecord(
        job_id=1,
        origin_label="Musterstadt Bahnhof",
        origin_lat=ORIGIN.lat,
        origin_lon=ORIGIN.lon,
        destination_label="Darmstadt, Rheinstr. 1",
        destination_region=region,
        car_route_km=km,
        car_route_minutes=55,
        method="MANUAL",
        source_note="measured",
        checked_at=NOW,
    )


def _remote(text: str = "") -> fit.Criterion:
    return fit.remote_criterion(text, SETTINGS)


def test_85_km_by_car_passes():
    assert fit.location_criterion(_job(), SETTINGS, _remote(), _check(85)).status == "PASS"


def test_car_route_above_the_limit_fails_and_mentions_relocation_within_hessen():
    criterion = fit.location_criterion(_job(), SETTINGS, _remote(), _check(130, "Hessen"))
    assert criterion.status == "FAIL" and "relocation within Hessen" in criterion.reason
    other = fit.location_criterion(_job(), SETTINGS, _remote(), _check(130, "Bayern"))
    assert other.status == "FAIL" and "relocation" not in other.reason


def test_without_a_car_route_the_distance_stays_unverified_even_if_close():
    # e.g. 85 km straight line is NOT a car route: never PASS on an estimate
    criterion = fit.location_criterion(_job(), SETTINGS, _remote(), None)
    assert criterion.status == "DISTANCE_UNVERIFIED"


def test_unknown_workplace_is_unknown_not_pass_or_fail():
    assert fit.location_criterion(_job(location=""), SETTINGS, _remote(), None).status == "UNKNOWN"


def test_unconfigured_commute_limit_is_reported():
    criterion = fit.location_criterion(_job(), CandidateSearchSettings(), _remote(), None)
    assert criterion.status == "NOT_CONFIGURED"


def test_fully_remote_from_hessen_needs_no_commute():
    remote = _remote("100% Remote, deutschlandweit")
    assert remote.status == "COMPATIBLE"
    assert fit.location_criterion(_job(location="Berlin"), SETTINGS, remote, None).status == (
        "REMOTE_OK"
    )


def test_fully_remote_restricted_to_another_state_is_incompatible():
    assert _remote("100% remote, nur aus Bayern").status == "INCOMPATIBLE"


def test_up_to_100_percent_remote_is_not_confirmed():
    assert _remote("Bis zu 100% remote").status == "PARTIAL_REMOTE"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Deutsch C1 zwingend erforderlich", "MAJOR_GAP"),
        ("Gute Deutschkenntnisse (B2)", "GAP"),
        ("Deutsch B2 von Vorteil", "MINOR_GAP"),
        ("Deutschkenntnisse B1", "OK"),
        ("Englisch fließend", "NO_REQUIREMENT"),
    ],
)
def test_language_against_a_confirmed_b1(text, status):
    assert fit.language_criterion(text, "B1").status == status


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Gehalt 48.000 - 55.000 € pro Jahr", "MATCH"),
        ("Gehalt 38.000 - 42.000 € pro Jahr", "BELOW"),
        ("Bezahlung nach TVöD", "TARIFF"),
        ("Attraktive Vergütung", "UNKNOWN"),
    ],
)
def test_salary_criterion(text, status):
    assert fit.salary_criterion(text, SETTINGS).status == status


def test_salary_below_is_informational_and_mentions_negotiable():
    criterion = fit.salary_criterion("Gehalt 40.000 €/Jahr", SETTINGS)
    assert "negotiable" in criterion.reason


def test_missing_junior_word_is_not_a_negative_signal():
    assert fit.entry_level_criterion("Python Backend Developer", "").status == "OPEN"
    assert fit.entry_level_criterion("Senior Developer", "").status == "EXPERIENCED"


def test_start_date_criterion():
    assert fit.start_date_criterion("Start ab sofort", SETTINGS).status == "EARLIER_REQUESTED"
    assert fit.start_date_criterion("", SETTINGS).status == "OK"


def test_no_overall_score_hides_a_blocker():
    assessment = fit.FitAssessment(job_id=1, company="x", title="y")
    assessment.criteria = [
        fit.Criterion("skills", "STRONG", "great match"),
        fit.Criterion("location", "FAIL", "130 km"),
    ]
    assert assessment.decision == "BLOCKED"
    assessment.criteria[1] = fit.Criterion("location", "DISTANCE_UNVERIFIED", "check")
    assert assessment.decision == "NEEDS_VERIFICATION"
    assessment.criteria[1] = fit.Criterion("location", "PASS", "85 km")
    assert assessment.decision == "CANDIDATE"


# --- database-backed ------------------------------------------------------------------


def _alembic(path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


SPEC = {
    "schema": "jobtriage.candidate-config/1",
    "source": "USER_CONFIRMED",
    "confirmed_on": "2026-10-27",
    "search": json.loads(SETTINGS.model_dump_json()),
    "languages": [{"language": "Deutsch", "level": "B1"}],
}


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fit.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    profile = get_or_create_candidate_profile(session)
    apply_config_plan(
        session,
        plan_config_change(profile, CandidateConfigSpec.model_validate(SPEC)),
        actor="test",
        reason="fixture",
    )
    session.add(
        _job(
            description="Wir suchen Unterstützung. Deutsch C1 zwingend erforderlich. "
            "Gehalt 48.000 € pro Jahr. Hybrid, 2 Tage im Büro.",
        )
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _apply_for(db, *, status=S.SUBMITTED, company="Example GmbH", source="stepstone", ref="X1"):
    return ApplicationTracker().create_application(
        db,
        company_name=company,
        role_title="Python Backend Developer (m/w/d)",
        source=source,
        source_reference=ref,
        application_channel=C.JOB_BOARD,
        initial_status=status,
        origin=O.IMPORT,
        actor="test",
        occurred_at=NOW,
    )


def test_assess_job_end_to_end(db):
    assessment = fit.assess_job(db, 1)
    statuses = {c.name: c.status for c in assessment.criteria}
    assert statuses == {
        "skills": "NOT_EVALUATED",
        "role": "NOT_CONFIGURED",
        "experience": "UNKNOWN",
        "language": "MAJOR_GAP",
        "location": "DISTANCE_UNVERIFIED",
        "remote": "HYBRID",
        "salary": "MATCH",
        "entry_level": "OPEN",
        "start_date": "OK",
        "duplicate": "CLEAR",
    }
    assert assessment.decision == "NEEDS_VERIFICATION"


def test_assess_job_uses_a_stored_car_route(db):
    record_commute_check(
        db,
        job_id=1,
        origin_label="Musterstadt Bahnhof",
        origin_lat=ORIGIN.lat,
        origin_lon=ORIGIN.lon,
        destination_label="Darmstadt",
        car_route_km=41.2,
        car_route_minutes=38,
        method="MANUAL",
        source_note="measured by the operator",
    )
    assert fit.assess_job(db, 1).criterion("location").status == "PASS"


@pytest.mark.parametrize("source", ["stepstone", "xing", "linkedin", "indeed", "bundesagentur"])
def test_a_sent_application_from_any_source_blocks_the_vacancy(db, source):
    _apply_for(db, source=source)
    assessment = fit.assess_job(db, 1)
    assert assessment.criterion("duplicate").status == "ALREADY_APPLIED"
    assert assessment.decision == "BLOCKED"
    with pytest.raises(AlreadyAppliedError):
        ensure_not_already_applied(db, db.get(JobRecord, 1))


def test_an_application_in_preparation_does_not_block_its_own_package(db):
    _apply_for(db, status=S.PACKAGE_READY)
    assert fit.assess_job(db, 1).criterion("duplicate").status == "IN_PREPARATION"
    ensure_not_already_applied(db, db.get(JobRecord, 1))  # no exception


def test_a_different_employer_is_not_a_duplicate(db):
    _apply_for(db, company="Other AG")
    ensure_not_already_applied(db, db.get(JobRecord, 1))


def test_bewerbung_generation_is_refused_for_an_applied_vacancy(db):
    from app.services.bewerbung import BewerbungService

    _apply_for(db)
    with pytest.raises(AlreadyAppliedError):
        asyncio.run(BewerbungService().generate(db, db.get(JobRecord, 1), cv_draft_id=12345))


def test_review_package_is_refused_for_an_applied_vacancy(db):
    from app.services.review_package import ReviewPackageService

    _apply_for(db)
    with pytest.raises(AlreadyAppliedError):
        ReviewPackageService().create(db, db.get(JobRecord, 1), 1, 1)


def test_commute_check_validation(db):
    base = dict(
        job_id=1,
        origin_label="o",
        destination_label="d",
        car_route_km=10,
        method="MANUAL",
        source_note="n",
    )
    for bad in (
        {"job_id": 999},
        {"method": "STRAIGHT_LINE"},
        {"car_route_km": 0},
        {"source_note": " "},
        {"destination_label": ""},
    ):
        with pytest.raises(CommuteCheckInputError):
            record_commute_check(db, **{**base, **bad})
    assert db.scalar(select(func.count()).select_from(JobCommuteCheckRecord)) == 0


# --- OSRM provider ---------------------------------------------------------------------


def _osrm(handler) -> OsrmRouteProvider:
    return OsrmRouteProvider(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_osrm_returns_km_and_minutes_and_sends_only_coordinates():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(
            200, json={"code": "Ok", "routes": [{"distance": 41234.0, "duration": 2280.0}]}
        )

    route = _osrm(handler).car_route(ORIGIN, GeoPoint(lat=49.87, lon=8.65))
    assert route == CarRoute(km=41.2, minutes=38.0)
    assert seen[0].path == "/route/v1/driving/8.750000,50.100000;8.650000,49.870000"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"code": "NoRoute", "routes": []}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"code": "Ok", "routes": [{"distance": "x"}]}),
        httpx.Response(503, text="busy"),
    ],
)
def test_osrm_failures_never_produce_a_distance(response):
    with pytest.raises(RouteUnavailableError):
        _osrm(lambda request: response).car_route(ORIGIN, ORIGIN)


def test_straight_line_is_only_a_lower_bound_helper():
    assert 20 < straight_line_km(ORIGIN, GeoPoint(lat=49.87, lon=8.65)) < 30


# --- CLI --------------------------------------------------------------------------------


@pytest.fixture()
def migrated(tmp_path):
    path = tmp_path / "cli.db"
    upgrade(_alembic(path), "head")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    session = sessionmaker(engine)()
    profile = get_or_create_candidate_profile(session)
    apply_config_plan(
        session,
        plan_config_change(profile, CandidateConfigSpec.model_validate(SPEC)),
        actor="test",
        reason="fixture",
    )
    session.add(_job())
    session.commit()
    session.close()
    engine.dispose()
    return path


class _FakeProvider:
    name = "OSRM"

    def __init__(self, route: CarRoute | None = None):
        self.route = route
        self.calls = []

    def car_route(self, origin, destination):
        self.calls.append((origin, destination))
        if self.route is None:
            raise RouteUnavailableError("down")
        return self.route


def _count(path) -> int:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(JobCommuteCheckRecord))
    finally:
        engine.dispose()


def test_cli_check_commute_dry_run_then_apply(migrated, capsys):
    provider = _FakeProvider(CarRoute(km=87.5, minutes=61))
    args = [
        "--database",
        str(migrated),
        "check-commute",
        "1",
        "--dest-lat",
        "49.87",
        "--dest-lon",
        "8.65",
        "--destination",
        "Darmstadt",
    ]
    assert cli.run(args, route_provider=provider) == 0
    assert "DRY RUN" in capsys.readouterr().out and _count(migrated) == 0
    assert cli.run([*args, "--apply"], route_provider=provider) == 0
    assert _count(migrated) == 1
    assert cli.run(["--database", str(migrated), "assess", "1"]) == 0
    out = capsys.readouterr().out
    assert "87.5 km by car" in out and "PASS" in out


def test_cli_check_commute_failure_stores_nothing(migrated, capsys):
    args = ["--database", str(migrated), "check-commute", "1", "--dest-lat", "49.8"]
    args += ["--dest-lon", "8.6", "--destination", "x", "--apply"]
    assert cli.run(args, route_provider=_FakeProvider()) == 1
    assert "stays unverified" in capsys.readouterr().err and _count(migrated) == 0


def test_cli_record_commute_and_json_assessment(migrated, capsys):
    args = ["--database", str(migrated), "record-commute", "1", "--car-km", "120"]
    args += ["--destination", "Fulda", "--region", "Hessen", "--source-note", "measured"]
    assert cli.run([*args, "--apply"]) == 0
    capsys.readouterr()
    assert cli.run(["--database", str(migrated), "assess", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    location = next(c for c in payload["criteria"] if c["name"] == "location")
    assert location["status"] == "FAIL" and payload["decision"] == "BLOCKED"


def test_cli_unknown_job_is_refused(migrated, capsys):
    assert cli.run(["--database", str(migrated), "assess", "999"]) == 2


# --- search strategy ----------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        bundesagentur_search_keywords="Python",
        bundesagentur_search_location="Frankfurt am Main",
        bundesagentur_search_radius_km=50,
    )
    values.update(overrides)
    return Settings(**values)


def test_legacy_settings_query_without_a_configuration():
    queries = bundesagentur_queries(_settings(), None)
    assert [(q.label, q.location, q.radius_km) for q in queries] == [
        ("settings", "Frankfurt am Main", 50)
    ]


def test_configured_search_uses_the_home_place_and_a_superset_radius_plus_remote():
    commute, remote = bundesagentur_queries(_settings(), SETTINGS)
    assert (commute.location, commute.radius_km, commute.working_time) == (
        "12345 Musterstadt",
        150,
        "",
    )
    assert (remote.location, remote.radius_km, remote.working_time) == ("", 0, "ho")
    assert remote.keywords == commute.keywords == "Python"


def test_radius_is_never_below_the_car_route_limit():
    for limit in (10, 25, 50, 95, 100, 125, 175):
        assert ba_radius_for(limit) >= limit
    assert ba_radius_for(100) == 150


def test_no_remote_query_without_the_remote_rule():
    onsite = SETTINGS.model_copy(update={"remote_work_scope": "UNKNOWN"})
    assert [q.label for q in bundesagentur_queries(_settings(), onsite)] == ["commute"]


def test_collector_sends_the_home_office_filter():
    collector = BundesagenturCollector(api_key="k", keywords="Python", working_time="ho")
    params = collector._build_params(None)
    assert params["arbeitszeit"] == "ho" and "wo" not in params


def test_runner_runs_every_query_and_processes_a_posting_once(db, monkeypatch):
    from app.models.job import Job
    from app.services import collector_runner

    made = []

    def _posting(ref: str) -> Job:
        return Job(
            title="Python Developer",
            company="Example GmbH",
            location="Darmstadt",
            url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
            description="Python FastAPI SQL",
            source="bundesagentur",
            source_reference=ref,
        )

    class _Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.skipped_invalid_count = 0
            made.append(self)

        async def fetch(self):
            if self.kwargs["working_time"] == "ho":
                return [_posting("10001-A-S"), _posting("10001-B-S")]
            return [_posting("10001-A-S")]

        async def fetch_detail(self, ref):
            return None

    monkeypatch.setattr(collector_runner, "BundesagenturCollector", _Fake)
    result = asyncio.run(
        collector_runner.run_bundesagentur(
            db, _settings(bundesagentur_api_key="real-looking-key-123")
        )
    )
    assert [(m.kwargs["location"], m.kwargs["working_time"]) for m in made] == [
        ("12345 Musterstadt", ""),
        ("", "ho"),
    ]
    assert result["fetched"] == 2
    assert db.get(CandidateProfileRecord, 1) is not None


# --- Codex Stage 17 review regressions -------------------------------------------------


def test_a_route_from_another_origin_is_stale():
    check = _check(85)
    check.origin_label = "Frankfurt Hbf"
    criterion = fit.location_criterion(_job(), SETTINGS, _remote(), check)
    assert criterion.status == "DISTANCE_UNVERIFIED" and "re-check" in criterion.reason


def test_an_imported_language_level_is_not_treated_as_confirmed(db):
    from app.db.models import CandidateLanguageRecord

    german = db.scalars(select(CandidateLanguageRecord)).one()
    german.level, german.source = "C1", "IMPORTED"
    db.commit()
    assert fit.assess_job(db, 1).criterion("language").reason.endswith("confirmed level UNKNOWN")


def test_osrm_route_entry_that_is_not_an_object():
    response = httpx.Response(200, json={"code": "Ok", "routes": [None]})
    with pytest.raises(RouteUnavailableError):
        _osrm(lambda request: response).car_route(ORIGIN, ORIGIN)


def test_the_straight_line_value_is_never_stored(migrated, capsys):
    args = ["--database", str(migrated), "check-commute", "1", "--dest-lat", "49.87"]
    args += ["--dest-lon", "8.65", "--destination", "Darmstadt", "--apply"]
    assert cli.run(args, route_provider=_FakeProvider(CarRoute(km=44.0, minutes=40))) == 0
    assert "straight line" in capsys.readouterr().out
    engine = create_engine(f"sqlite:///{migrated.as_posix()}")
    try:
        with engine.connect() as conn:
            note = conn.scalar(select(JobCommuteCheckRecord.source_note))
    finally:
        engine.dispose()
    assert "straight" not in note.lower()


def test_ba_radius_refuses_a_limit_it_cannot_cover():
    with pytest.raises(ValueError):
        ba_radius_for(176)


def test_a_failing_remote_query_keeps_the_commute_results(db, monkeypatch):
    from app.models.job import Job
    from app.services import collector_runner

    class _Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.skipped_invalid_count = 0

        async def fetch(self):
            if self.kwargs["working_time"] == "ho":
                raise RuntimeError("remote search down")
            return [
                Job(
                    title="Python Developer",
                    company="Example GmbH",
                    location="Darmstadt",
                    url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-C-S",
                    description="Python",
                    source="bundesagentur",
                    source_reference="10001-C-S",
                )
            ]

        async def fetch_detail(self, ref):
            return None

    monkeypatch.setattr(collector_runner, "BundesagenturCollector", _Fake)
    result = asyncio.run(
        collector_runner.run_bundesagentur(
            db, _settings(bundesagentur_api_key="real-looking-key-123")
        )
    )
    assert result["fetched"] == 1 and result["created"] == 1


def test_configured_search_keeps_the_collector_seniority_gate(db):
    from app.db.candidate_profile_repository import get_candidate_profile

    # the configuration never writes profile.target_roles (Stage 11A/11B gates)
    assert json.loads(get_candidate_profile(db).target_roles_json) == []


def test_a_route_from_moved_origin_coordinates_is_stale():
    check = _check(85)
    check.origin_lat = 49.0
    assert fit.location_criterion(_job(), SETTINGS, _remote(), check).status == (
        "DISTANCE_UNVERIFIED"
    )


def test_one_commute_and_one_remote_search_per_configured_term():
    configured = SETTINGS.model_copy(update={"search_keywords": ["Python", "Softwaretester"]})
    queries = bundesagentur_queries(_settings(), configured)
    assert [(q.label, q.keywords) for q in queries] == [
        ("commute", "Python"),
        ("commute", "Softwaretester"),
        ("remote", "Python"),
        ("remote", "Softwaretester"),
    ]


def test_check_commute_stores_the_origin_coordinates(migrated):
    args = ["--database", str(migrated), "check-commute", "1", "--dest-lat", "49.87"]
    args += ["--dest-lon", "8.65", "--destination", "Darmstadt", "--apply"]
    assert cli.run(args, route_provider=_FakeProvider(CarRoute(km=44.0, minutes=40))) == 0
    engine = create_engine(f"sqlite:///{migrated.as_posix()}")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(JobCommuteCheckRecord.origin_lat, JobCommuteCheckRecord.origin_lon)
            ).one()
    finally:
        engine.dispose()
    assert tuple(row) == (ORIGIN.lat, ORIGIN.lon)


def test_configured_terms_are_used_even_without_a_commute_configuration():
    terms_only = CandidateSearchSettings(search_keywords=["Python", "Softwaretester"])
    queries = bundesagentur_queries(_settings(), terms_only)
    assert [(q.label, q.keywords, q.location) for q in queries] == [
        ("settings", "Python", "Frankfurt am Main"),
        ("settings", "Softwaretester", "Frankfurt am Main"),
    ]


def test_a_failing_later_term_is_reported_and_keeps_other_results(db, monkeypatch):
    from app.models.job import Job
    from app.services import collector_runner

    configured = json.loads(SETTINGS.model_dump_json())
    configured["search_keywords"] = ["Python", "Softwaretester"]
    spec = {**SPEC, "search": {"search_keywords": ["Python", "Softwaretester"]}}
    profile = db.get(CandidateProfileRecord, 1)
    apply_config_plan(
        db,
        plan_config_change(profile, CandidateConfigSpec.model_validate(spec)),
        actor="t",
        reason="r",
    )

    class _Fake:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.skipped_invalid_count = 0

        async def fetch(self):
            if self.kwargs["keywords"] == "Softwaretester":
                raise RuntimeError("term search down")
            ref = f"10001-{self.kwargs['keywords']}-{self.kwargs['working_time'] or 'x'}-S"
            return [
                Job(
                    title="Python Developer",
                    company="Example GmbH",
                    location="Darmstadt",
                    url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
                    description="Python",
                    source="bundesagentur",
                    source_reference=ref,
                )
            ]

        async def fetch_detail(self, ref):
            return None

    monkeypatch.setattr(collector_runner, "BundesagenturCollector", _Fake)
    result = asyncio.run(
        collector_runner.run_bundesagentur(
            db, _settings(bundesagentur_api_key="real-looking-key-123")
        )
    )
    # Python commute + Python remote processed; both Softwaretester searches failed
    assert result["fetched"] == 2 and result["failed_queries"] == 2


@pytest.mark.parametrize(
    ("title", "status"),
    [
        ("Python Backend Developer (m/w/d)", "PRIMARY"),
        ("Junior Python Developer", "PRIMARY"),
        ("Second Level Support / Technical Application Support (m/w/d)", "SECONDARY"),
        ("Lagerist (m/w/d)", "OTHER"),
    ],
)
def test_role_priority_is_informational(title, status):
    settings = SETTINGS.model_copy(
        update={
            "primary_roles": ["Junior Python Developer", "Python Backend Developer"],
            "secondary_roles": ["Application Support / Technical Application Support"],
        }
    )
    assert fit.role_criterion(title, settings).status == status
    assessment = fit.FitAssessment(job_id=1, company="x", title=title)
    assessment.criteria = [fit.role_criterion(title, settings)]
    assert assessment.decision == "CANDIDATE"


def test_failed_queries_make_the_automation_step_partial(monkeypatch):
    from app.services import automation

    async def _step(db, settings, *, touched_jobs=None, is_lease_lost=None):
        return {
            "fetched": 3,
            "created": 3,
            "updated": 0,
            "skipped_invalid": 0,
            "failed": 0,
            "failed_queries": 1,
        }

    result = asyncio.run(
        automation._run_step(None, "bundesagentur", _step, None, [], is_lease_lost=lambda: False)
    )
    assert result["status"] == "partial"
