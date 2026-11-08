"""TRACK B: nationwide fully-remote discovery.

Network-free: the Bundesagentur API is a `httpx.MockTransport`. Proves that the search is
really nationwide (no `wo` / `umkreis`), bounded and paced, that only an explicit "no home
office" flag drops a posting, that both tracks share one row per vacancy, that a failed run does
not advance the watermark, and that the feature is off by default and sends nothing.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.collectors.bundesagentur import (
    BundesagenturAPIError,
    BundesagenturCollector,
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

    def __init__(self, by_term, *, fail_terms=()):
        self.by_term = by_term
        self.fail_terms = set(fail_terms)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
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

        def build(cfg, query):
            collector = real(cfg, query)
            collector._injected_client = fake.client()
            return collector

        monkeypatch.setattr(collector_runner, "ba_query_collector", build)
        monkeypatch.setattr(collector_runner, "TelegramNotifier", _NoTelegram)
        return fake

    return install


# --- the search itself ---------------------------------------------------------------------


def test_queries_are_nationwide_bounded_and_employment_only():
    queries = nationwide_remote_queries(settings())
    assert [q.keywords for q in queries] == list(NATIONWIDE_TERMS)
    for query in queries:
        assert query.location == "" and query.radius_km == 0
        assert query.offer_type == 1
        assert query.exclude_home_office_denied and query.target_titles_only
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


def test_collector_keeps_unset_home_office_flag_and_drops_only_an_explicit_no():
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
        "k",
        keywords="x",
        exclude_home_office_denied=True,
        target_titles_only=True,
        http_client=fake.client(),
    )
    jobs = asyncio.run(collector.fetch())
    assert [j.source_reference for j in jobs] == ["a", "b"]
    assert collector.skipped_home_office_denied == 1
    assert collector.skipped_not_target_title == 2
    assert collector.skipped_invalid_count == 0


def test_request_budget_and_pagination_are_bounded():
    rows = [posting(str(i), "Python Developer (m/w/d)") for i in range(450)]
    fake = FakeBA({"x": rows})
    collector = BundesagenturCollector(
        "k", keywords="x", max_pages=5, max_requests=2, http_client=fake.client()
    )
    jobs = asyncio.run(collector.fetch())
    assert len(fake.requests) == 2 and len(jobs) == 200
    assert collector.request_budget_exhausted and collector.truncated


def test_requests_are_paced(monkeypatch):
    pauses: list[float] = []

    async def record(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("app.collectors.bundesagentur.asyncio.sleep", record)
    fake = FakeBA({"x": [posting(str(i), "Python Developer (m/w/d)") for i in range(250)]})
    collector = BundesagenturCollector(
        "k", keywords="x", request_delay_seconds=1.5, http_client=fake.client()
    )
    asyncio.run(collector.fetch())
    assert pauses == [1.5, 1.5]  # three pages, a pause before each page after the first


def test_a_search_with_no_budget_left_cannot_make_requests():
    fake = FakeBA({"x": [posting("1", GOOD)]})
    collector = BundesagenturCollector("k", keywords="x", max_requests=0, http_client=fake.client())
    assert asyncio.run(collector.fetch()) == []
    assert fake.requests == [] and collector.request_budget_exhausted


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
    rows = [posting("s1", "Senior Python Developer (m/w/d)"), posting("j1", GOOD)]
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
    assert result["searches"] == 3 and result["failed_queries"] == len(NATIONWIDE_TERMS) - 3


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
