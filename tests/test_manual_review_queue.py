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
    first = queries.review_page(reader, filters, page_size=5)
    second = queries.review_page(reader, filters, page_size=5)
    assert [i.job_id for i in first.items] == [i.job_id for i in second.items]
    assert first.next_cursor == second.next_cursor
    assert _rows(writer) == before
    assert before["applications"] == 0


def test_a_cold_cache_costs_at_most_the_stale_budget(writer, reader):
    for n in range(1, 40):
        _add_job(writer, n, f"Junior Python Entwickler {n}")
    calls = []

    def counting(db, job_id):
        calls.append(job_id)
        return assess_job(db, job_id)

    page = queries.review_page(reader, queries.ReviewFilters(), assess=counting, stale_budget=10)
    assert len(calls) == 10
    assert page.stale_unassessed == 29
    assert page.live_assessed <= 10


def test_the_stale_budget_goes_to_junior_titles_first(writer, reader):
    for n in range(1, 21):
        _add_job(writer, n, f"Software Entwickler Java {n}")
    _add_job(writer, 21, "Junior Python Entwickler (m/w/d)")
    calls = []

    def counting(db, job_id):
        calls.append(job_id)
        return assess_job(db, job_id)

    queries.review_page(reader, queries.ReviewFilters(), assess=counting, stale_budget=3)
    assert calls[0] == 21 and len(calls) == 3


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
    assert client.get("/ui/review?cursor=1%27%3B--").status_code == 200
    assert client.get("/ui/review?profession=evil").status_code == 422
    assert client.get("/ui/review?profession=junior&reason=NEEDS_ENRICHMENT").status_code == 200
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


def test_hybrid_admin_titles_are_not_in_the_junior_group(writer, reader):
    _add_job(writer, 1, "Junior Python Entwickler / IT-Systemadministrator")
    _add_job(writer, 2, "Junior Python Entwickler (m/w/d)")
    assert rq.is_junior_python_title("Junior Python Entwickler / IT-Systemadministrator") is False
    junior = queries.review_page(reader, queries.ReviewFilters(profession="junior"))
    hybrid = queries.review_page(reader, queries.ReviewFilters(profession="hybrid_admin"))
    assert [i.job_id for i in junior.items] == [2]
    assert [i.job_id for i in hybrid.items] == [1]


def _walk(reader, *, page_size, **filters):
    """Follow the cursor links like the browser does; returns every item id in order."""
    seen, cursor = [], ""
    for _ in range(200):
        page = queries.review_page(
            reader, queries.ReviewFilters(cursor=cursor, **filters), page_size=page_size
        )
        seen += [i.job_id for i in page.items]
        if not page.next_cursor:
            return seen
        cursor = page.next_cursor
    raise AssertionError("pagination did not terminate")


def _seed_mixed(writer):
    titles = {}
    for n in range(1, 31):
        titles[n] = (
            f"Junior Python Entwickler {n}" if n % 7 == 0 else f"Software Entwickler Java {n}"
        )
        _add_job(writer, n, titles[n])
    titles[31] = "Python Entwickler / IT-Systemadministrator (m/w/d)"
    _add_job(writer, 31, titles[31])
    return titles


def test_the_queue_is_sorted_globally_before_it_is_paged(writer, reader):
    titles = _seed_mixed(writer)
    everything = _walk(reader, page_size=1000)
    for size in (1, 3, 4, 7):
        assert _walk(reader, page_size=size) == everything  # same global order on any page size
    junior = [i for i in everything if rq.is_junior_python_title(titles[i])]
    assert junior and everything[: len(junior)] == junior  # junior first, across page borders
    assert everything[-1] == 31  # the admin/developer hybrid is last
    assert len(everything) == len(set(everything)) == 31


def test_pagination_has_no_duplicates_and_survives_a_vacancy_arriving_meanwhile(writer, reader):
    _seed_mixed(writer)
    first = queries.review_page(reader, queries.ReviewFilters(), page_size=5)
    _add_job(writer, 99, "Junior Python Entwickler brandneu")  # arrives between two requests
    rest, cursor = [], first.next_cursor
    while cursor:
        page = queries.review_page(reader, queries.ReviewFilters(cursor=cursor), page_size=5)
        rest += [i.job_id for i in page.items]
        cursor = page.next_cursor
    seen = [i.job_id for i in first.items] + rest
    assert len(seen) == len(set(seen))  # never twice
    # every original vacancy is still reachable exactly once; the newcomer sorts before the
    # cursor, so it is not forced into the middle of the walk
    assert set(range(1, 32)) <= set(seen)


def test_a_malformed_cursor_is_ignored_not_trusted(writer, reader):
    _seed_mixed(writer)
    base = queries.review_page(reader, queries.ReviewFilters(), page_size=5)
    for bad in ("x", "1.2.3", "1;DROP", "9" * 80, "1.2.3.4.5.6"):
        page = queries.review_page(reader, queries.ReviewFilters(cursor=bad), page_size=5)
        assert [i.job_id for i in page.items] == [i.job_id for i in base.items]


def test_profession_and_reason_filters_select_inside_the_global_order(writer, reader):
    titles = _seed_mixed(writer)
    everything = _walk(reader, page_size=1000)
    junior = _walk(reader, page_size=2, profession="junior")
    assert junior == [i for i in everything if rq.is_junior_python_title(titles[i])]
    developer = _walk(reader, page_size=1000, profession="developer")
    assert set(junior) <= set(developer) and 31 not in developer  # junior is a subset
    nothing = queries.review_page(reader, queries.ReviewFilters(reason="STATED_GAP"))
    assert nothing.items == [] and nothing.total == 0


def _cache_row(db, job_id, key, tier, applied=False):
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
            applied=applied,
            computed_at=NOW,
        )
    )
    db.commit()


def _refresh_cache(db):
    """Store a fresh cache row per vacancy, exactly like scripts/stage21_assess_vacancies.py."""
    from app.services import fit_cache

    inputs = fit_cache.global_inputs(db)
    ids = [j for (j,) in db.execute(select(JobRecord.id))]
    keys = fit_cache.current_keys(db, ids, inputs)
    for job_id in ids:
        fit_cache.upsert_assessment(
            db, job_id=job_id, key=keys[job_id], inputs=inputs, assessment=assess_job(db, job_id)
        )
    db.commit()


def _raising_assess(db, job_id):
    raise AssertionError(f"job {job_id} was assessed live although its cache row is fresh")


def test_fresh_cache_rows_are_read_without_any_live_assessment(writer, reader):
    _seed_mixed(writer)
    live = queries.review_page(reader, queries.ReviewFilters(), page_size=1000)
    _refresh_cache(writer)
    cached = queries.review_page(
        reader, queries.ReviewFilters(), page_size=1000, assess=_raising_assess
    )
    assert cached.from_cache and cached.live_assessed == 0 and cached.stale_unassessed == 0
    assert [i.job_id for i in cached.items] == [i.job_id for i in live.items]
    assert [(i.family, i.reason_code, i.work_format) for i in cached.items] == [
        (i.family, i.reason_code, i.work_format) for i in live.items
    ]


def test_a_changed_vacancy_turns_its_cache_row_stale_and_is_assessed_live(writer, reader):
    _seed_mixed(writer)
    _refresh_cache(writer)
    job = writer.get(JobRecord, 5)
    job.description = "Python, Django. Deutsch C2 verhandlungssicher."
    writer.commit()
    assessed = []

    def counting(db, job_id):
        assessed.append(job_id)
        return assess_job(db, job_id)

    page = queries.review_page(reader, queries.ReviewFilters(), assess=counting, page_size=1000)
    assert assessed == [5]  # only the changed vacancy; every other row is still fresh
    assert page.fresh >= 29 and page.stale_unassessed == 0


def test_a_fresh_applied_or_barrier_row_is_dropped_and_a_stale_one_never_hides(writer, reader):
    from app.db.models import JobFitAssessmentRecord
    from app.services import fit_cache

    _add_job(writer, 1, "Junior Python Entwickler stale")
    _add_job(writer, 2, "Junior Python Entwickler fresh barrier")
    _add_job(writer, 3, "Junior Python Entwickler fresh applied")
    _cache_row(writer, 1, "0" * 64, "NOT_SUITABLE")  # stale: key differs from the current one
    keys = fit_cache.current_keys(writer, [2, 3])
    _cache_row(writer, 2, keys[2], "NOT_SUITABLE")
    _cache_row(writer, 3, keys[3], "NEEDS_OPERATOR", applied=True)
    assert writer.get(JobFitAssessmentRecord, 3).applied is True
    assessed = []

    def counting(db, job_id):
        assessed.append(job_id)
        return assess_job(db, job_id)

    page = queries.review_page(reader, queries.ReviewFilters(), assess=counting)
    assert assessed == [1]  # the stale row is assessed live, never trusted
    assert [i.job_id for i in page.items] == [1]


def test_the_page_is_built_again_without_the_cache_when_the_inputs_change_meanwhile(
    writer, reader, monkeypatch
):
    from app.services import fit_cache

    _add_job(writer, 1, "Junior Python Entwickler")
    _refresh_cache(writer)
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
    assert assessed == [1]  # the cached row was not trusted after the inputs moved
    assert page.from_cache is False


def test_xing_stubs_are_listed_for_manual_opening_and_never_fetched(writer, reader, monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("the queue must never open a connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    writer.add(
        JobRecord(
            fingerprint="xing-stub",
            source="xing",
            title="(Junior) Backend Engineer (m/w/d)",
            company="Stub GmbH",
            location="Frankfurt am Main",
            url="https://www.xing.com/m/CrV68YHN5uRnwD67EiFNgM",
            description="Titel - Ort",
            score=45,
            recommendation="NEEDS_ENRICHMENT",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    writer.commit()
    page = queries.review_page(reader, queries.ReviewFilters(profession="junior"))
    assert len(page.items) == 1 and rq.NEEDS_ENRICHMENT in page.items[0].also
    assert page.items[0].url == "https://www.xing.com/m/CrV68YHN5uRnwD67EiFNgM"


def test_the_queue_modules_cannot_reach_gmail_telegram_or_an_employer():
    import ast
    from pathlib import Path

    for name in ("app/ui/review_queue_query.py", "app/services/review_queue.py"):
        tree = ast.parse(Path(name).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        for module in imported:
            assert not any(
                w in module for w in ("telegram", "gmail", "smtp", "imap", "httpx", "requests")
            ), f"{name} imports {module}"


def test_the_number_of_statements_does_not_grow_with_the_number_of_vacancies(writer, reader):
    from sqlalchemy import event

    def count(n_jobs):
        for n in range(1, n_jobs + 1):
            if writer.get(JobRecord, n) is None:
                _add_job(writer, n, f"Software Entwickler Java {n}")
        _refresh_cache(writer)
        seen = []
        engine = reader.get_bind()

        def record(*args):
            seen.append(1)

        event.listen(engine, "before_cursor_execute", record)
        try:
            queries.review_page(reader, queries.ReviewFilters(), assess=_raising_assess)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(seen)

    small = count(20)
    large = count(120)
    assert large <= small + 2, (small, large)  # chunked, not one statement per vacancy


def test_the_route_is_a_thread_pool_handler_that_never_blocks_the_event_loop():
    import inspect

    from app.ui import routes

    assert not inspect.iscoroutinefunction(routes.review)


def test_an_existing_bewerbung_is_shown_on_the_entry():
    a = _assessment("Junior Python Entwickler", duplicate="POSSIBLE_DUPLICATE")
    item = rq.review_item(_job(), a, None)
    assert item is not None and item.duplicate_status == "POSSIBLE_DUPLICATE"
    applied = _assessment("Junior Python Entwickler", duplicate="ALREADY_APPLIED")
    assert rq.review_item(_job(), applied, None) is None  # already applied: not in the queue


def test_the_page_shows_filters_format_and_the_stale_notice(client, writer, monkeypatch):
    from app.ui import review_queue_query

    defaults = {**review_queue_query.review_page.__kwdefaults__, "stale_budget": 1}
    monkeypatch.setattr(review_queue_query.review_page, "__kwdefaults__", defaults)
    for n in range(1, 4):
        _add_job(writer, n, f"Junior Python Entwickler {n}")
    html = client.get("/ui/review?profession=junior").text
    assert 'name="profession"' in html and 'name="reason"' in html
    assert "Work format" in html and "Vollzeit" in html
    assert "not recomputed here" in html  # the cache is empty: the notice says so
    assert "scripts/stage21_assess_vacancies.py" in html


def test_a_changed_rules_hash_makes_every_cached_decision_stale(writer, reader, monkeypatch):
    from app.services import fit_cache

    _seed_mixed(writer)
    _refresh_cache(writer)
    fresh = queries.review_page(
        reader, queries.ReviewFilters(), page_size=1000, assess=_raising_assess
    )
    assert fresh.from_cache and fresh.stale_unassessed == 0
    monkeypatch.setattr(fit_cache, "rules_hash", lambda: "f" * 64)  # the rules changed
    calls = []

    def counting(db, job_id):
        calls.append(job_id)
        return assess_job(db, job_id)

    page = queries.review_page(reader, queries.ReviewFilters(), assess=counting, stale_budget=5)
    assert len(calls) == 5  # no old row is trusted; the live work is bounded
    assert page.stale_unassessed == 26  # and the page says that it is incomplete


# Stage 58: a vacancy the gate holds for unconfirmed hours must not vanish from every list ------


def _primary_with_work_time(status):
    a = _assessment("Python Entwickler (m/w/d)", role="PRIMARY", skills="STRONG")
    a.criteria = [c for c in a.criteria if c.name != "work_time"]
    a.criteria.append(Criterion("work_time", status, "w"))
    a.criteria.append(
        Criterion(
            "work_time_evidence", "CONFIRMED" if status == "FULL_TIME" else "NOT_CONFIRMED", "w"
        )
    )
    return a


def test_a_suitable_developer_role_with_unconfirmed_hours_is_in_the_queue():
    # PART_TIME / CONFLICT already lower the fit tier (STRETCH); UNKNOWN is the case the gate holds
    status = "UNKNOWN"
    a = _primary_with_work_time(status)
    assert automatic_block_reason(a) == "WORK_TIME_NOT_CONFIRMED"  # the gate holds it ...
    item = rq.review_item(_job(title=a.title), a, "https://example.invalid/x")
    assert item is not None  # ... so a human must be able to see it
    assert item.reason_code == rq.WORK_TIME_NOT_CONFIRMED
    assert "unknown" in item.reason.lower()


def test_a_confirmed_full_time_developer_role_does_not_enter_the_queue():
    a = _primary_with_work_time("FULL_TIME")
    assert rq.review_item(_job(title=a.title), a, None) is None


def test_heuristic_full_time_without_evidence_stays_in_the_queue():
    """Stage 60: the classifier's FULL_TIME is not enough; the vacancy waits for a human."""
    a = _primary_with_work_time("FULL_TIME")
    a.criteria = [c for c in a.criteria if c.name != "work_time_evidence"]
    item = rq.review_item(_job(title=a.title), a, None)
    assert item is not None
    assert item.reason_code == "WORK_TIME_NOT_CONFIRMED"


def test_adjacent_heuristic_full_time_without_evidence_stays_in_the_queue():
    """Stage 60 (Codex P2): a newly held ADJACENT vacancy must not vanish from the queue."""
    a = _primary_with_work_time("FULL_TIME")
    a.criteria = [
        Criterion("skills", "STRONG", "s") if c.name == "skills" else c
        for c in a.criteria
        if c.name != "work_time_evidence"
    ]
    a.title = "Junior Data Analyst (m/w/d)"
    item = rq.review_item(_job(title=a.title), a, None)
    assert item is not None
    assert item.reason_code == "WORK_TIME_NOT_CONFIRMED"
