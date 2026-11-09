"""Manual review queue: vacancies that wait for the operator's decision.

The queue only lists, explains and links. Being in it never clears a vacancy for an automatic
application: nothing here prepares, approves, sends, creates an application or changes a status.
"""

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.models import ApplicationRecord, Base, JobRecord
from app.services import review_queue as rq
from app.services.job_fit_assessment import Criterion, FitAssessment, assess_job
from app.services.shortlist_gate import automatic_block_reason
from app.services.triage_category import THIN_SKILL_EVIDENCE
from app.ui import queries
from app.ui.readonly import build_readonly_engine, get_ui_db, readonly_sessionmaker

API_KEY = "queue-test-key-0123456789"
NOW = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


def _assessment(
    title,
    *,
    role="OTHER",
    skills="WEAK",
    language="NO_REQUIREMENT",
    location="REMOTE_OK",
    remote="COMPATIBLE",
    duplicate="CLEAR",
    skills_reason="s",
):
    a = FitAssessment(job_id=1, company="Example GmbH", title=title)
    a.criteria = [
        Criterion("role", role, "r"),
        Criterion("skills", skills, skills_reason),
        Criterion("location", location, "l"),
        Criterion("remote", remote, "r"),
        Criterion("entry_level", "ENTRY_LEVEL", "e"),
        Criterion("language", language, "g"),
        Criterion("salary", "UNKNOWN", "p"),
        Criterion("start_date", "UNKNOWN", "d"),
        Criterion("experience", "UNKNOWN", "x"),
        Criterion("work_time", "UNKNOWN", "w"),
        Criterion("duplicate", duplicate, "d"),
    ]
    return a


def _job(job_id=1, title="Junior Engineer (m/w/d) Schwerpunkt Python", **overrides):
    values = dict(
        id=job_id,
        title=title,
        company="Example GmbH",
        source="bundesagentur",
        first_seen_at=NOW,
        score=81,
        recommendation="MAYBE",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# --- membership and explanation ------------------------------------------------------------


def test_bfi_style_vacancy_is_in_the_queue_with_its_reasons():
    a = _assessment("Junior Engineer (m/w/d) Schwerpunkt Python", language="GAP")
    item = rq.review_item(_job(), a, "https://example.invalid/job")
    assert item is not None
    assert item.reason_code == THIN_SKILL_EVIDENCE
    assert rq.LANGUAGE_REVIEW in item.also
    assert item.junior_python is True
    assert any("requirements" in line for line in item.clarify)
    assert any("German" in line for line in item.clarify)


def test_being_in_the_queue_does_not_allow_automatic_preparation():
    a = _assessment("Junior Engineer (m/w/d) Schwerpunkt Python", language="GAP")
    assert rq.review_item(_job(), a, None) is not None
    assert automatic_block_reason(a) is not None


def test_title_only_stub_is_available_for_analysis_and_nothing_is_invented():
    a = _assessment("Python Automation Specialist", skills="NOT_EVALUATED")
    stub = _job(2, "Python Automation Specialist", recommendation="NEEDS_ENRICHMENT", score=0)
    item = rq.review_item(stub, a, None)
    assert item is not None
    assert item.reason_code == rq.NEEDS_ENRICHMENT
    assert "nothing is known" in item.reason
    assert item.work_time == "UNKNOWN"


def test_a_missing_field_does_not_hide_a_vacancy():
    a = _assessment("Junior Engineer (m/w/d) Schwerpunkt Python", location="UNKNOWN")
    a.criteria = [c for c in a.criteria if c.name != "work_time"]
    item = rq.review_item(_job(first_seen_at=None, score=0), a, None)
    assert item is not None
    assert item.work_time == "UNKNOWN"
    assert item.first_seen_at is None


@pytest.mark.parametrize(
    "title",
    ["IT Support Engineer Python (m/w/d)", "Systemadministrator Python", "Helpdesk Mitarbeiter"],
)
def test_admin_and_support_are_not_in_the_queue(title):
    a = _assessment(title, role="OFF_ROLE", skills="STRONG")
    assert rq.review_item(_job(3, title), a, None) is None


def test_a_confirmed_barrier_is_not_in_the_queue():
    barrier = _assessment("Senior Python Entwickler", role="OFF_ROLE", skills="STRONG")
    assert barrier.fit_tier == "NOT_SUITABLE"
    assert rq.review_item(_job(4, "Senior Python Entwickler"), barrier, None) is None


def test_an_already_applied_vacancy_is_not_in_the_queue():
    a = _assessment("Junior Engineer Schwerpunkt Python", duplicate="ALREADY_APPLIED")
    assert rq.review_item(_job(), a, None) is None


def test_a_negative_skill_gap_is_not_labelled_thin_evidence():
    a = _assessment(
        "Junior Engineer Schwerpunkt Python",
        skills_reason="required skill score 10/100" + "; missing required: Java, Spring",
    )
    assert rq.review_item(_job(), a, None) is None


# --- ordering ------------------------------------------------------------------------------


def test_junior_python_comes_before_other_developer_and_adjacent_roles():
    junior = rq.review_item(
        _job(1, "Junior Engineer (m/w/d) Schwerpunkt Python"),
        _assessment("Junior Engineer (m/w/d) Schwerpunkt Python", language="GAP"),
        None,
    )
    adjacent = rq.review_item(
        _job(3, "Datenanalyst (m/w/d)", score=99),
        _assessment("Datenanalyst (m/w/d)", skills="PARTIAL", location="UNKNOWN", remote="UNKNOWN"),
        None,
    )
    assert junior is not None and adjacent is not None
    assert rq.sort_key(junior) < rq.sort_key(adjacent)
    assert adjacent.family == "ADJACENT"


def test_developer_and_adjacent_vacancies_are_marked_apart():
    dev = rq.review_item(
        _job(5, "Software Entwickler (m/w/d)", score=60),
        _assessment("Software Entwickler (m/w/d)", skills="PARTIAL", location="UNKNOWN"),
        None,
    )
    adj = rq.review_item(
        _job(6, "Datenanalyst (m/w/d)", score=60),
        _assessment("Datenanalyst (m/w/d)", skills="PARTIAL", location="UNKNOWN"),
        None,
    )
    assert dev is not None and adj is not None
    assert (dev.family, adj.family) == ("DEVELOPER", "ADJACENT")
    assert rq.sort_key(dev) < rq.sort_key(adj)


def test_a_hybrid_admin_title_is_never_ranked_as_a_developer_or_junior_python():
    title = "IT-Systemadministrator / Softwareentwickler (m/w/d)"
    hybrid = rq.review_item(_job(7, title, score=99), _assessment(title, skills="PARTIAL"), None)
    plain = rq.review_item(
        _job(8, "Software Entwickler (m/w/d)", score=10),
        _assessment("Software Entwickler (m/w/d)", skills="PARTIAL", location="UNKNOWN"),
        None,
    )
    assert hybrid is not None and plain is not None
    assert hybrid.family == "HYBRID_ADMIN"
    assert hybrid.junior_python is False
    assert rq.sort_key(plain) < rq.sort_key(hybrid)


def test_junior_python_title_test_matches_what_the_ui_selects_in_sql():
    assert rq.is_junior_python_title("(Junior) Backend Engineer (m/w/d)")
    assert rq.is_junior_python_title("Berufseinsteiger Python Entwickler")
    assert not rq.is_junior_python_title("Senior Python Developer")
    assert not rq.is_junior_python_title("Junior Buchhalter")


# --- the page: real database, read-only engine ---------------------------------------------


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'queue.db').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


@pytest.fixture()
def writer(db_url):
    engine = create_engine(db_url)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def reader(db_url):
    engine = build_readonly_engine(db_url)
    session = readonly_sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _add_job(db, n, title):
    db.add(
        JobRecord(
            fingerprint=f"fp-{n:04d}",
            source="bundesagentur",
            title=title,
            company=f"Beispiel GmbH {n}",
            location="Berlin",
            url=f"https://jobs.example.invalid/{n}",
            description="Python, FastAPI, SQL. Deutsch B1.",
            score=70,
            recommendation="MAYBE",
            first_seen_at=NOW - timedelta(hours=n),
            last_seen_at=NOW,
        )
    )
    db.commit()


def _rows(db):
    return {
        "jobs": db.scalar(select(func.count()).select_from(JobRecord)),
        "applications": db.scalar(select(func.count()).select_from(ApplicationRecord)),
        "status": sorted(
            (j.id, j.status, j.score, j.recommendation) for j in db.scalars(select(JobRecord))
        ),
    }


def test_opening_the_page_twice_is_stable_and_writes_nothing(writer, reader):
    for n in range(1, 13):
        _add_job(writer, n, f"Junior Python Entwickler {n}" if n % 2 else f"Backend Engineer {n}")
    before = _rows(writer)
    filters = queries.ReviewFilters()
    first = queries.review_page(reader, filters, page_size=5, scan_budget=20)
    second = queries.review_page(reader, filters, page_size=5, scan_budget=20)
    assert [i.job_id for i in first.items] == [i.job_id for i in second.items]
    assert first.scanned == second.scanned
    assert _rows(writer) == before
    assert before["applications"] == 0


def test_opening_the_page_never_assesses_more_than_the_scan_budget(writer, reader):
    for n in range(1, 40):
        _add_job(writer, n, f"Junior Python Entwickler {n}")
    calls = []

    def counting(db, job_id):
        calls.append(job_id)
        return assess_job(db, job_id)

    page = queries.review_page(reader, queries.ReviewFilters(), assess=counting, scan_budget=10)
    assert len(calls) <= 10
    assert page.scanned <= 10


def test_the_read_only_session_cannot_write_what_the_writer_can(writer, reader):
    values = dict(
        source="bundesagentur",
        title="Probe",
        company="c",
        location="l",
        url="https://example.invalid/p",
        description="d",
        score=1,
        recommendation="MAYBE",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    writer.add(JobRecord(fingerprint="writer-ok", **values))
    writer.commit()  # the same valid row is accepted through the writer ...
    with pytest.raises(Exception, match="(?i)readonly|read-only|read only"):
        reader.add(JobRecord(fingerprint="reader-no", **values))
        reader.commit()  # ... and refused by the read-only engine


# --- the route -----------------------------------------------------------------------------


@pytest.fixture()
def client(db_url, monkeypatch):
    from app.main import app
    from app.security import rate_limit

    engine = build_readonly_engine(db_url)
    factory = readonly_sessionmaker(engine)

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.rollback()
            db.close()

    settings = Settings(
        _env_file=None,
        api_key=API_KEY,
        telegram_bot_token="",
        ui_enabled=True,
        ui_cookie_secure=True,
        gmail_username="",
    )
    for target in ("app.ui.auth", "app.ui.routes", "app.security.rate_limit"):
        monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
    rate_limit._ui_login_limiter.buckets.clear()
    app.dependency_overrides[get_ui_db] = override
    test_client = TestClient(app, base_url="https://testserver")
    response = test_client.post("/ui/login", data={"api_key": API_KEY}, follow_redirects=False)
    assert response.status_code == 303
    yield test_client
    app.dependency_overrides.pop(get_ui_db, None)
    engine.dispose()


def test_the_page_requires_a_session(db_url, monkeypatch):
    from app.main import app

    settings = Settings(_env_file=None, api_key=API_KEY, ui_enabled=True, ui_cookie_secure=True)
    monkeypatch.setattr("app.ui.auth.get_settings", lambda: settings)
    monkeypatch.setattr("app.ui.routes.get_settings", lambda: settings)
    anonymous = TestClient(app, base_url="https://testserver")
    response = anonymous.get("/ui/review", follow_redirects=False)
    assert response.status_code == 303


def test_the_page_renders_and_offers_no_action(client, writer):
    _add_job(writer, 1, "Junior Python Entwickler (m/w/d)")
    before = _rows(writer)
    response = client.get("/ui/review")
    assert response.status_code == 200
    html = response.text
    assert "Needs review" in html
    assert "does not allow an automatic application" in html
    # the only form that posts is the layout's log-out; the queue itself has none
    posts = re.findall(r'<form[^>]*method="post"[^>]*action="([^"]+)"', html, re.IGNORECASE)
    assert posts == ["/ui/logout"]
    for word in ("Approve", "Send", "Prepare", "Bewerbung erstellen"):
        assert f">{word}<" not in html
    assert _rows(writer) == before


def test_the_page_has_no_way_to_change_anything(client):
    for method in (client.post, client.put, client.delete, client.patch):
        assert method("/ui/review").status_code in {403, 405}


def test_a_garbage_filter_value_is_ignored_not_trusted(client, writer):
    _add_job(writer, 1, "Junior Python Entwickler (m/w/d)")
    assert client.get("/ui/review?reason=%27%3B+DROP+TABLE+jobs%3B--").status_code == 200
    assert client.get("/ui/review?phase=other&before_id=1").status_code == 200
    assert client.get("/ui/review?phase=evil").status_code == 422
    assert writer.scalar(select(func.count()).select_from(JobRecord)) == 1


# --- review findings: visibility, phases, traversal, cache --------------------------------


def test_a_target_title_with_missing_skill_analysis_stays_visible_whatever_the_recommendation():
    a = _assessment("Junior Engineer Schwerpunkt Python", skills="NOT_EVALUATED")
    item = rq.review_item(_job(recommendation="MAYBE"), a, None)
    assert item is not None
    assert item.reason_code == rq.NEEDS_ENRICHMENT


def test_a_non_target_title_without_skill_analysis_is_still_excluded():
    a = _assessment("Buchhalter (m/w/d)", skills="NOT_EVALUATED")
    assert rq.review_item(_job(9, "Buchhalter (m/w/d)", recommendation="MAYBE"), a, None) is None


def test_hybrid_admin_titles_are_not_in_the_junior_group_in_python_or_sql(writer, reader):
    _add_job(writer, 1, "Junior Python Entwickler / IT-Systemadministrator")
    _add_job(writer, 2, "Junior Python Entwickler (m/w/d)")
    assert rq.is_junior_python_title("Junior Python Entwickler / IT-Systemadministrator") is False
    junior = queries.review_page(reader, queries.ReviewFilters(phase="junior"))
    other = queries.review_page(reader, queries.ReviewFilters(phase="other"))
    junior_ids = {i.job_id for i in junior.items}
    other_ids = {i.job_id for i in other.items}
    assert 1 not in junior_ids and 2 not in other_ids
    assert junior_ids.isdisjoint(other_ids)
    # every queue member is reachable in exactly one phase
    assert junior_ids == {2}
    assert other_ids == {1}


def _collect(reader, *, page_size, scan_budget):
    """Follow the page links like the browser does; returns every item id in order."""
    seen, phase, before = [], "junior", None
    for _ in range(200):
        page = queries.review_page(
            reader,
            queries.ReviewFilters(phase=phase, before_id=before),
            page_size=page_size,
            scan_budget=scan_budget,
        )
        seen += [i.job_id for i in page.items]
        if page.next_before_id:
            before = page.next_before_id
        elif page.next_phase:
            phase, before = page.next_phase, None
        else:
            return seen
    raise AssertionError("pagination did not terminate")


def test_following_the_links_visits_every_vacancy_once_across_both_phases(writer, reader):
    titles = {}
    for n in range(1, 31):
        titles[n] = (
            f"Junior Python Entwickler {n}" if n % 3 == 0 else f"Software Entwickler Java {n}"
        )
        _add_job(writer, n, titles[n])
    titles[31] = "Python Entwickler / IT-Systemadministrator (m/w/d)"
    _add_job(writer, 31, titles[31])
    everything = _collect(reader, page_size=50, scan_budget=10_000)
    # independent expectation: all 31 vacancies wait for review in this data (no route or
    # profile is configured), and the hybrid admin title is among them but last
    assert sorted(everything) == list(range(1, 32))
    assert everything[-1] == 31
    for page_size, budget in ((3, 7), (5, 5), (4, 1000)):
        walked = _collect(reader, page_size=page_size, scan_budget=budget)
        assert sorted(walked) == sorted(everything)
        assert len(walked) == len(set(walked))
    junior_first = [i for i in everything if rq.is_junior_python_title(titles[i])]
    assert junior_first, "the data must contain Junior Python titles"
    assert set(everything[: len(junior_first)]) == set(junior_first)


def _cache_row(db, job_id, key, tier):
    from app.db.models import JobFitAssessmentRecord

    db.add(
        JobFitAssessmentRecord(
            job_id=job_id,
            input_key=key,
            rules_hash="r" * 64,
            profile_version=None,
            fit_tier=tier,
            decision="BLOCKED",
            criteria_json="[]",
            tier_reasons_json="[]",
            work_mode="UNKNOWN",
            applied=False,
            computed_at=NOW,
        )
    )
    db.commit()


def test_a_stale_cached_barrier_never_hides_a_vacancy_but_a_fresh_one_is_skipped(writer, reader):
    from app.services import fit_cache

    _add_job(writer, 1, "Junior Python Entwickler stale")
    _add_job(writer, 2, "Junior Python Entwickler fresh")
    _cache_row(writer, 1, "0" * 64, "NOT_SUITABLE")  # stale: key differs from the current one
    fresh_key = fit_cache.current_keys(writer, [2])[2]
    _cache_row(writer, 2, fresh_key, "NOT_SUITABLE")
    assert fit_cache.cache_in_use(reader)
    assessed = []

    def counting(db, job_id):
        assessed.append(job_id)
        return assess_job(db, job_id)

    queries.review_page(reader, queries.ReviewFilters(), assess=counting)
    assert 1 in assessed  # stale row: assessed live, never trusted
    assert 2 not in assessed  # fresh confirmed barrier: no live assessment needed


def test_the_page_is_read_again_without_the_cache_when_the_inputs_change_meanwhile(
    writer, reader, monkeypatch
):
    from app.services import fit_cache

    _add_job(writer, 1, "Junior Python Entwickler")
    fresh_key = fit_cache.current_keys(writer, [1])[1]
    _cache_row(writer, 1, fresh_key, "NOT_SUITABLE")
    real = fit_cache.global_inputs
    calls = {"n": 0}

    def drifting(session):
        calls["n"] += 1
        value = real(session)
        if calls["n"] >= 2:  # the end-of-page re-check sees a different state
            return dataclasses.replace(value, profile_version=(value.profile_version or 0) + 1)
        return value

    monkeypatch.setattr(fit_cache, "global_inputs", drifting)
    assessed = []

    def counting(db, job_id):
        assessed.append(job_id)
        return assess_job(db, job_id)

    page = queries.review_page(reader, queries.ReviewFilters(), assess=counting)
    assert 1 in assessed  # the cached barrier was not trusted after the inputs moved
    assert page.from_cache is False
