"""TRACK B: nationwide fully-remote discovery.

Network-free: the Bundesagentur API is a `httpx.MockTransport`. Proves that the search is
really nationwide (no `wo` / `umkreis`), bounded and paced, that only an explicit "no home
office" flag drops a posting, that both tracks share one row per vacancy, that a failed run does
not advance the watermark, and that the feature is off by default and sends nothing.
"""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.collectors.bundesagentur import (
    BundesagenturAPIError,
    BundesagenturCollector,
    RequestGate,
    structured_facts,
)
from app.core.config import Settings
from app.db.base import Base
from app.db.models import AutomationRunRecord, JobRecord
from app.models.job import Job
from app.services import collector_runner
from app.services.collector_runner import CollectorNotConfiguredError, run_bundesagentur
from app.services.job_fit_assessment import work_time_criterion
from app.services.nationwide_discovery import (
    STEP_NAME,
    last_success_at,
    nationwide_due,
    run_nationwide_remote_discovery,
)
from app.services.search_strategy import (
    NATIONWIDE_TERMS,
    nationwide_remote_queries,
    nationwide_window_days,
)

ACCOUNT = "me@example.com"
GOOD = "Junior Python Developer (m/w/d)"


def posting(ref, title, *, company="Remote GmbH", home_office=None, full_time=None, city="Berlin"):
    address = {"plz": "10115", "ort": city, "region": "BERLIN"}
    item = {
        "referenznummer": ref,
        "stellenangebotsTitel": title,
        "firma": company,
        "stellenangebotsart": "ARBEIT",
        "stellenlokationen": [{"adresse": address, "breite": 52.5, "laenge": 13.4}],
    }
    if home_office is not None:
        item["homeofficemoeglich"] = home_office
    if full_time is not None:
        item["arbeitszeitVollzeit"] = full_time
    return item


class FakeBA:
    """A scripted Bundesagentur: one list of postings per search term, 100 per page."""

    def __init__(self, by_term, *, fail_terms=(), details=None):
        self.details = details or {}
        self.by_term = by_term
        self.fail_terms = set(fail_terms)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        if "jobdetails" in request.url.path:
            ref = base64.b64decode(unquote(request.url.path.rsplit("/", 1)[1])).decode()
            return httpx.Response(
                200, json={"stellenangebotsBeschreibung": self.details.get(ref, "")}
            )
        if params.get("was") in self.fail_terms:
            return httpx.Response(500, json={})
        rows = self.by_term.get(params.get("was"), [])
        page = int(params.get("page", 1))
        size = int(params.get("size", 100))
        return httpx.Response(
            200,
            json={
                "maxErgebnisse": len(rows),
                "page": page,
                "ergebnisliste": rows[(page - 1) * size : page * size],
            },
        )

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class _NoTelegram:
    sent = 0

    def __init__(self, *args, **kwargs):
        pass

    async def send_job(self, *args, **kwargs):
        _NoTelegram.sent += 1
        return True


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr("app.collectors.bundesagentur.asyncio.sleep", instant)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'nationwide.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def settings(**overrides):
    values = {
        "bundesagentur_api_key": "jobboerse-jobsuche",
        "nationwide_remote_discovery_enabled": True,
        "nationwide_remote_request_delay_seconds": 0.0,
        "telegram_vacancy_feed_enabled": False,
        "min_job_score_to_notify": 101,
    }
    return Settings(_env_file=None, **{**values, **overrides})


@pytest.fixture()
def wired(monkeypatch):
    """Route every collector the runner builds to the scripted API."""

    def install(fake: FakeBA):
        real = collector_runner.ba_query_collector

        def build(cfg, query, gate=None):
            collector = real(cfg, query, gate)
            collector._injected_client = fake.client()
            return collector

        monkeypatch.setattr(collector_runner, "ba_query_collector", build)
        monkeypatch.setattr(collector_runner, "TelegramNotifier", _NoTelegram)
        return fake

    return install


# --- the search itself ---------------------------------------------------------------------


def test_queries_are_nationwide_bounded_and_employment_only():
    queries = nationwide_remote_queries()
    assert [q.keywords for q in queries] == list(NATIONWIDE_TERMS)
    for query in queries:
        assert query.location == "" and query.radius_km == 0
        assert query.offer_type == 1
        assert query.target_titles_only
        assert not query.home_office_only  # the BA flag is a helper, never the filter
    assert len(set(NATIONWIDE_TERMS)) == len(NATIONWIDE_TERMS)


def test_search_requests_carry_no_location_and_no_radius(db, wired):
    fake = wired(FakeBA({"Python Entwickler": [posting("1", GOOD)]}))
    asyncio.run(run_nationwide_remote_discovery(db, settings()))
    searches = [p for p in fake.requests if "was" in p]
    assert searches
    for params in searches:
        assert "wo" not in params and "umkreis" not in params
        assert params["angebotsart"] == "1"


def test_window_is_used_only_for_the_effective_ba_values():
    assert nationwide_window_days(None) is None
    assert nationwide_window_days(1.0) == 7
    assert nationwide_window_days(6.0) == 7
    assert nationwide_window_days(7.5) == 14
    assert nationwide_window_days(13.0) == 14
    assert nationwide_window_days(14.5) is None  # longer gap: no window, everything again


def test_collector_never_filters_on_the_home_office_flag_but_on_the_title():
    fake = FakeBA(
        {
            "x": [
                posting("a", GOOD, home_office=True),
                posting("b", GOOD, home_office=None),
                posting("c", GOOD, home_office=False),
                posting("d", "Systemadministrator (m/w/d)", home_office=True),
                posting("e", "Koch (m/w/d)", home_office=None),
            ]
        }
    )
    collector = BundesagenturCollector(
        "k", keywords="x", target_titles_only=True, http_client=fake.client()
    )
    jobs = asyncio.run(collector.fetch())
    assert [j.source_reference for j in jobs] == ["a", "b", "c"]
    assert collector.skipped_not_target_title == 2
    assert collector.skipped_invalid_count == 0


def test_request_budget_and_pagination_are_bounded():
    rows = [posting(str(i), "Python Developer (m/w/d)") for i in range(450)]
    fake = FakeBA({"x": rows})
    gate = RequestGate(0.0, 2)
    collector = BundesagenturCollector(
        "k", keywords="x", max_pages=5, gate=gate, http_client=fake.client()
    )
    jobs = asyncio.run(collector.fetch())
    assert len(fake.requests) == 2 and len(jobs) == 200
    assert gate.exhausted and collector.truncated


def test_requests_are_paced_across_collectors_and_detail_calls(monkeypatch):
    pauses: list[float] = []

    async def record(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("app.collectors.bundesagentur.asyncio.sleep", record)
    fake = FakeBA({"x": [posting("1", GOOD)], "y": [posting("2", GOOD)]})
    gate = RequestGate(1.5)
    first = BundesagenturCollector("k", keywords="x", gate=gate, http_client=fake.client())
    second = BundesagenturCollector("k", keywords="y", gate=gate, http_client=fake.client())
    asyncio.run(first.fetch())
    asyncio.run(first.fetch_detail("1"))
    asyncio.run(second.fetch())
    assert len(fake.requests) == 3
    assert len(pauses) == 2 and all(0 < p <= 1.5 for p in pauses)  # none before the first


def test_failed_attempts_and_retries_are_charged_to_the_budget():
    fake = FakeBA({}, fail_terms={"x"})
    gate = RequestGate(0.0, 2)
    collector = BundesagenturCollector(
        "k", keywords="x", max_retries=3, gate=gate, http_client=fake.client()
    )
    asyncio.run(collector.fetch())  # budget ends the retries; the search yields nothing
    assert len(fake.requests) == 2 and gate.search_attempts == 2 and collector.truncated


def test_a_search_with_no_budget_left_cannot_make_requests():
    fake = FakeBA({"x": [posting("1", GOOD)]})
    gate = RequestGate(0.0, 0)
    collector = BundesagenturCollector("k", keywords="x", gate=gate, http_client=fake.client())
    assert asyncio.run(collector.fetch()) == []
    assert fake.requests == [] and gate.exhausted


# --- structured facts ----------------------------------------------------------------------


def test_full_time_flag_is_a_structured_fact_and_unset_stays_unset():
    assert structured_facts({"arbeitszeitVollzeit": True})["full_time_offered"] is True
    assert structured_facts({"arbeitszeitVollzeit": False})["full_time_offered"] is False
    assert "full_time_offered" not in structured_facts({})
    assert "full_time_offered" not in structured_facts({"arbeitszeitVollzeit": "yes"})


def _row(flag):
    class Row:
        full_time_offered = flag

    return Row()


def test_source_flag_speaks_only_where_the_text_is_silent():
    silent = "Wir suchen einen Entwickler."
    assert work_time_criterion(silent, _row(True)).status == "FULL_TIME"
    assert work_time_criterion("Teilzeit, 20 Stunden pro Woche.", _row(True)).status == "PART_TIME"
    assert work_time_criterion(silent, _row(False)).status == "PART_TIME"
    assert work_time_criterion("Vollzeit.", _row(False)).status == "FULL_TIME"
    assert work_time_criterion(silent, _row(None)).status == "UNKNOWN"
    assert work_time_criterion(silent, None).status == "UNKNOWN"


# --- the run: shared identity, caps, failure handling --------------------------------------


def test_a_posting_found_by_both_tracks_is_one_row_and_history_is_kept(db, wired):
    shared = posting("77", GOOD, home_office=True, full_time=True)
    cfg = settings(bundesagentur_search_keywords="Python")
    wired(FakeBA({"Python": [shared], "Python Entwickler": [shared]}))
    asyncio.run(run_bundesagentur(db, cfg))
    row = db.scalars(select(JobRecord)).one()
    row.status = "APPLIED"  # the operator's decision on the existing row
    db.commit()
    before = (row.id, row.status, row.first_seen_at)

    result = asyncio.run(run_nationwide_remote_discovery(db, cfg))

    assert db.scalar(select(func.count()).select_from(JobRecord)) == 1
    again = db.scalars(select(JobRecord)).one()
    assert (again.id, again.status, again.first_seen_at) == before
    assert result["created"] == 0 and result["updated"] >= 1


def test_new_vacancy_outside_hessen_is_stored_with_its_flags(db, wired):
    ad = posting("90", GOOD, home_office=True, full_time=True, city="Hamburg")
    wired(FakeBA({"Python Entwickler": [ad]}))
    result = asyncio.run(run_nationwide_remote_discovery(db, settings()))
    row = db.scalars(select(JobRecord)).one()
    assert row.source_reference == "90" and row.full_time_offered is True
    assert row.home_office_possible is True and row.location == "Hamburg"
    assert result["created"] == 1 and result["searches"] == len(NATIONWIDE_TERMS)


def test_cap_on_new_vacancies_defers_the_rest_and_prefers_entry_level(db, wired):
    rows = [
        posting("s1", "Senior Python Developer (m/w/d)", home_office=True),
        posting("j1", GOOD, home_office=True),
    ]
    wired(FakeBA({"Python Entwickler": rows}))
    cfg = settings(nationwide_remote_max_new_per_run=1)
    result = asyncio.run(run_nationwide_remote_discovery(db, cfg))
    assert [r.source_reference for r in db.scalars(select(JobRecord)).all()] == ["j1"]
    assert result["created"] == 1 and result["deferred"] == 1


def test_request_budget_spans_all_terms(db, wired):
    pages = {term: [posting(term, "Python Developer (m/w/d)")] for term in NATIONWIDE_TERMS}
    fake = wired(FakeBA(pages))
    cfg = settings(nationwide_remote_max_requests_per_run=3)
    result = asyncio.run(run_nationwide_remote_discovery(db, cfg))
    assert len([p for p in fake.requests if "was" in p]) == 3
    assert result["requests"] == 3 and result["truncated_searches"] == len(NATIONWIDE_TERMS) - 3


def test_one_failing_search_is_isolated_but_all_failing_raises(db, wired):
    ad = posting("5", "Python Developer (m/w/d)", home_office=True)
    wired(FakeBA({"Python Backend": [ad]}, fail_terms={"Python Entwickler"}))
    result = asyncio.run(run_nationwide_remote_discovery(db, settings()))
    assert result["created"] == 1 and result["failed_queries"] == 1

    wired(FakeBA({}, fail_terms=set(NATIONWIDE_TERMS)))
    with pytest.raises(BundesagenturAPIError):
        asyncio.run(run_nationwide_remote_discovery(db, settings()))


def test_missing_key_is_not_configured(db):
    with pytest.raises(CollectorNotConfiguredError):
        asyncio.run(run_nationwide_remote_discovery(db, settings(bundesagentur_api_key="")))


def test_nothing_is_sent_by_the_run(db, wired):
    _NoTelegram.sent = 0
    wired(FakeBA({"Python Entwickler": [posting("9", GOOD, home_office=True)]}))
    asyncio.run(run_nationwide_remote_discovery(db, settings()))
    assert _NoTelegram.sent == 0  # min_job_score_to_notify is out of reach, feed is off


# --- scheduling ----------------------------------------------------------------------------


def test_feature_flags_default_to_safe_values():
    defaults = Settings(_env_file=None)
    assert defaults.nationwide_remote_discovery_enabled is False
    assert defaults.local_discovery_enabled is True  # today's behaviour is unchanged


def _finished_run(db, *, status, days_ago):
    finished = datetime.now(UTC) - timedelta(days=days_ago)
    db.add(
        AutomationRunRecord(
            account_key=ACCOUNT,
            status="COMPLETED",
            started_at=finished - timedelta(minutes=5),
            finished_at=finished,
            results_json=json.dumps({STEP_NAME: {"status": status}}),
        )
    )
    db.commit()


def test_due_logic_uses_the_last_successful_run_only(db):
    cfg = settings(nationwide_remote_discovery_interval_seconds=86_400)
    assert nationwide_due(db, ACCOUNT, cfg) == (True, None)  # never ran: full backfill

    _finished_run(db, status="failed", days_ago=0.1)
    assert nationwide_due(db, ACCOUNT, cfg) == (True, None)  # a failure moves nothing

    _finished_run(db, status="ok", days_ago=0.5)
    assert nationwide_due(db, ACCOUNT, cfg) == (False, None)  # inside the interval
    assert last_success_at(db, ACCOUNT) is not None


def test_due_after_the_interval_asks_for_a_window(db):
    cfg = settings(nationwide_remote_discovery_interval_seconds=86_400)
    _finished_run(db, status="ok", days_ago=2)
    assert nationwide_due(db, ACCOUNT, cfg) == (True, 7)


def test_disabled_feature_is_never_due(db):
    cfg = settings(nationwide_remote_discovery_enabled=False)
    assert nationwide_due(db, ACCOUNT, cfg) == (False, None)


def test_job_model_carries_the_flag():
    job = Job(
        source="bundesagentur", title="t", company="c", location="", url="https://example.org/1"
    )
    assert job.full_time_offered is None
    assert job.model_copy(update={"full_time_offered": True}).full_time_offered is True


# --- only postings that can still qualify are stored ---------------------------------------


def test_new_onsite_and_hybrid_postings_are_not_stored_but_remote_ones_are(db, wired):
    ads = [
        posting("on", GOOD),
        posting("hy", GOOD),
        posting("re", GOOD),
        posting("ho", GOOD, home_office=True),
        posting("si", GOOD),
    ]
    details = {
        "on": "Arbeitsort ist Nuernberg. Vor Ort im Buero. Python, SQL.",
        "hy": "Hybrid: drei Tage pro Woche im Buero. Python, SQL.",
        "re": "100 % Remote aus Deutschland. Vollzeit. Python, SQL.",
        "ho": "Wir bieten spannende Aufgaben mit Python und SQL.",
        "si": "Wir bieten spannende Aufgaben mit Python und SQL.",
    }
    wired(FakeBA({"Python Entwickler": ads}, details=details))
    result = asyncio.run(run_nationwide_remote_discovery(db, settings()))
    stored = {r.source_reference for r in db.scalars(select(JobRecord)).all()}
    assert stored == {"re", "ho"}  # "si" says nothing about remote and has no BA flag
    assert result["created"] == 2 and result["skipped_not_remote_candidate"] == 3


def test_an_already_stored_posting_is_updated_even_if_it_no_longer_qualifies(db, wired):
    ad = posting("on", GOOD, home_office=True)
    cfg = settings(bundesagentur_search_keywords="Python")
    wired(FakeBA({"Python": [ad]}, details={"on": "Vor Ort im Buero. Python."}))
    asyncio.run(run_bundesagentur(db, cfg))
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 1

    wired(FakeBA({"Python Entwickler": [ad]}, details={"on": "Vor Ort im Buero. Python."}))
    result = asyncio.run(run_nationwide_remote_discovery(db, cfg))
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 1
    assert result["created"] == 0 and result["updated"] == 1


# --- the automation cycle ------------------------------------------------------------------


def _cycle(db, cfg, monkeypatch):
    from app.services import automation

    calls: list[str] = []

    async def local(db, settings, *, touched_jobs=None, is_lease_lost=None):
        calls.append("local")
        return {"fetched": 0, "created": 0, "updated": 0, "failed": 0, "skipped_invalid": 0}

    async def xing(db, settings, *, touched_jobs=None, is_lease_lost=None):
        calls.append("xing")
        return {"fetched": 0, "created": 0, "updated": 0, "failed": 0, "skipped_invalid": 0}

    async def nationwide(db, settings, *, touched_jobs=None, is_lease_lost=None, window_days=None):
        calls.append(f"nationwide:{window_days}")
        return {"fetched": 0, "created": 0, "updated": 0, "failed": 0, "skipped_invalid": 0}

    monkeypatch.setattr(automation, "run_bundesagentur", local)
    monkeypatch.setattr(automation, "run_xing", xing)
    monkeypatch.setattr(automation, "run_nationwide_remote_discovery", nationwide)
    run = asyncio.run(automation.run_automation_cycle(db, account_key=ACCOUNT, settings=cfg))
    return run, calls


def test_cycle_default_runs_only_the_existing_steps(db, monkeypatch):
    run, calls = _cycle(db, Settings(_env_file=None), monkeypatch)
    assert calls == ["local", "xing"]
    assert STEP_NAME not in json.loads(run.results_json)


def test_cycle_runs_nationwide_when_enabled_and_records_the_step(db, monkeypatch):
    run, calls = _cycle(db, settings(), monkeypatch)
    assert calls == ["local", "xing", "nationwide:None"]
    assert json.loads(run.results_json)[STEP_NAME]["status"] == "ok"
    # the watermark now holds: a second cycle inside the interval does not search again
    _run, calls_again = _cycle(db, settings(), monkeypatch)
    assert calls_again == ["local", "xing"]


def test_cycle_can_switch_the_local_track_off(db, monkeypatch):
    cfg = settings(local_discovery_enabled=False)
    run, calls = _cycle(db, cfg, monkeypatch)
    assert calls == ["xing", "nationwide:None"]
    assert "bundesagentur" not in json.loads(run.results_json)


def test_the_home_office_flag_is_only_a_helper(db, wired):
    ads = [
        posting("fa", GOOD, home_office=False),  # flag says no, the text says 100 % remote
        posting("fb", GOOD, home_office=False),  # flag says no, the text says nothing
    ]
    details = {"fa": "100 % Remote aus Deutschland. Python.", "fb": "Aufgaben mit Python."}
    wired(FakeBA({"Python Entwickler": ads}, details=details))
    asyncio.run(run_nationwide_remote_discovery(db, settings()))
    assert {r.source_reference for r in db.scalars(select(JobRecord)).all()} == {"fa"}


def test_missing_detail_is_not_evidence_against_remote(db, wired):
    wired(FakeBA({"Python Entwickler": [posting("nd", GOOD)]}, details={}))
    asyncio.run(run_nationwide_remote_discovery(db, settings()))
    row = db.scalars(select(JobRecord)).one()  # stored; the next run enriches it
    assert row.source_reference == "nd" and row.description == ""


def test_rejected_postings_do_not_use_up_the_cap_on_stored_vacancies(db, wired):
    ads = [posting("on", GOOD), posting("re", GOOD)]
    details = {"on": "Vor Ort im Buero in Nuernberg. Python.", "re": "100 % Remote. Python."}
    wired(FakeBA({"Python Entwickler": ads}, details=details))
    cfg = settings(nationwide_remote_max_new_per_run=1)
    result = asyncio.run(run_nationwide_remote_discovery(db, cfg))
    assert {r.source_reference for r in db.scalars(select(JobRecord)).all()} == {"re"}
    assert result["skipped_not_remote_candidate"] == 1 and "deferred" not in result


def test_detail_cap_bounds_the_requests_spent_on_new_postings(db, wired):
    ads = [posting(str(i), GOOD) for i in range(5)]
    fake = wired(FakeBA({"Python Entwickler": ads}, details={str(i): "On-site." for i in range(5)}))
    cfg = settings(nationwide_remote_max_detail_per_run=2)
    result = asyncio.run(run_nationwide_remote_discovery(db, cfg))
    assert len([r for r in fake.requests if "was" not in r]) == 2
    assert result["deferred"] == 3


def _step_run(db, step, *, days_ago):
    finished = datetime.now(UTC) - timedelta(days=days_ago)
    db.add(
        AutomationRunRecord(
            account_key=ACCOUNT,
            status="COMPLETED",
            started_at=finished - timedelta(minutes=5),
            finished_at=finished,
            results_json=json.dumps(step),
        )
    )
    db.commit()


def test_watermark_survives_many_unrelated_runs_and_ignores_incomplete_ones(db):
    _step_run(db, {STEP_NAME: {"status": "ok", "counters": {"deferred": 0}}}, days_ago=2)
    for n in range(80):  # a 15-minute cadence of unrelated cycles since
        _step_run(db, {"bundesagentur": {"status": "ok"}}, days_ago=1.9 - n * 0.01)
    assert last_success_at(db, ACCOUNT) is not None

    _step_run(db, {STEP_NAME: {"status": "ok", "counters": {"deferred": 4}}}, days_ago=0.5)
    _step_run(
        db, {STEP_NAME: {"status": "ok", "counters": {"truncated_searches": 1}}}, days_ago=0.4
    )
    _step_run(
        db, {STEP_NAME: {"status": "partial", "counters": {"failed_queries": 1}}}, days_ago=0.3
    )
    found = last_success_at(db, ACCOUNT)
    assert found is not None and (datetime.now(UTC) - found) > timedelta(days=1.5)
