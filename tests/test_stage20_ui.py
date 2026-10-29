"""Stage 20: read-only operator UI (docs/STAGE20_UI_ARCHITECTURE.md)."""

import socket
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, insert, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.models import ApplicationEventRecord, Base, JobRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_correlation import InboundMessage
from app.services.application_tracker import ApplicationTracker
from app.services.job_fit_assessment import Criterion, FitAssessment
from app.ui import auth, queries
from app.ui.readonly import (
    ReadOnlySessionError,
    build_readonly_engine,
    get_ui_db,
    readonly_sessionmaker,
)

API_KEY = "stage20-test-key-0123456789"
APPLICANT = "applicant@example.invalid"
NOW = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)


@pytest.fixture()
def db_url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'stage20_ui.db').as_posix()}"
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


def _job(db, n, *, title=None, url=None, location="Frankfurt am Main", source="bundesagentur"):
    job = JobRecord(
        fingerprint=f"fp-{n:04d}",
        source=source,
        title=title or f"Python Entwickler {n} (m/w/d)",
        company=f"Beispiel GmbH {n}",
        location=location,
        url=url or f"https://jobs.example.invalid/{n}",
        description="Python, FastAPI, SQL. Deutsch B1.",
        score=70,
        recommendation="MAYBE",
        first_seen_at=NOW - timedelta(hours=n),
        last_seen_at=NOW,
    )
    db.add(job)
    db.commit()
    return job


def _application(db, *, ref="REF-1", status=S.SUBMITTED, occurred_at=None, company="Acme GmbH"):
    ApplicationTracker().create_application(
        db,
        company_name=company,
        role_title="Backend Developer (m/w/d)",
        source="bundesagentur",
        source_reference=ref,
        application_channel=C.EMPLOYER_PORTAL,
        initial_status=status,
        origin=O.IMPORT,
        actor="test",
        occurred_at=occurred_at or NOW - timedelta(days=3),
        employer_domains=("acme.example",),
        correlation_key=f"portal-submission:{ref}",
    )
    db.commit()


def _settings(**overrides):
    values = dict(
        api_key=API_KEY,
        telegram_bot_token="",
        ui_enabled=True,
        ui_cookie_secure=True,
        gmail_username="",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture()
def client_factory(db_url, monkeypatch):
    from app.main import app
    from app.security import rate_limit

    engine = build_readonly_engine(db_url)
    factory = readonly_sessionmaker(engine)
    clients = []

    def override():
        db = factory()
        try:
            yield db
        finally:
            db.rollback()
            db.close()

    def make(**overrides):
        settings = _settings(**overrides)
        for target in ("app.ui.auth", "app.ui.routes", "app.security.rate_limit"):
            monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
        rate_limit._ui_login_limiter.buckets.clear()
        app.dependency_overrides[get_ui_db] = override
        # https base URL so the Secure cookie is sent back by the test client
        client = TestClient(app, base_url="https://testserver")
        clients.append(client)
        return client

    yield make
    app.dependency_overrides.pop(get_ui_db, None)
    engine.dispose()


def _login(client):
    response = client.post("/ui/login", data={"api_key": API_KEY}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response


# --- flag, login, session ------------------------------------------------------


def test_ui_is_404_when_disabled(client_factory):
    client = client_factory(ui_enabled=False)
    for path in ("/ui/", "/ui/login", "/ui/vacancies", "/ui/static/style.css"):
        assert client.get(path, follow_redirects=False).status_code == 404, path
    response = client.post("/ui/login", data={"api_key": API_KEY}, follow_redirects=False)
    assert response.status_code == 404


def test_pages_redirect_to_login_without_session(client_factory):
    client = client_factory()
    for path in ("/ui/", "/ui/vacancies", "/ui/applications", "/ui/inbox", "/ui/follow-ups"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/ui/login"


def test_login_sets_strict_cookie_and_wrong_key_is_refused(client_factory):
    client = client_factory()
    bad = client.post("/ui/login", data={"api_key": "wrong"}, follow_redirects=False)
    assert bad.status_code == 401
    assert "Invalid key" in bad.text
    assert "set-cookie" not in bad.headers

    cookie = _login(client).headers["set-cookie"].lower()
    for flag in ("httponly", "samesite=strict", "path=/ui", "secure"):
        assert flag in cookie
    assert API_KEY.lower() not in cookie
    assert client.get("/ui/", follow_redirects=False).status_code == 200


def test_empty_api_key_refuses_every_login(client_factory):
    client = client_factory(api_key="")
    response = client.post("/ui/login", data={"api_key": ""}, follow_redirects=False)
    assert response.status_code == 401
    assert not auth.session_token_valid(auth.issue_session_token("", hours=1), "")


def test_cross_origin_login_is_refused(client_factory):
    client = client_factory()
    response = client.post(
        "/ui/login",
        data={"api_key": API_KEY},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    opaque = client.post(
        "/ui/login",
        data={"api_key": API_KEY},
        headers={"Origin": "null"},
        follow_redirects=False,
    )
    assert opaque.status_code == 403
    same = client.post(
        "/ui/login",
        data={"api_key": API_KEY},
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )
    assert same.status_code == 303


def test_origin_check_survives_tls_proxy_and_honours_public_origin(client_factory):
    client = client_factory()
    # behind the TLS proxy Uvicorn sees http://, the browser sends an https Origin
    plain = TestClient(client.app, base_url="http://testserver")
    behind_proxy = plain.post(
        "/ui/login",
        data={"api_key": API_KEY},
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )
    assert behind_proxy.status_code == 303
    other_host = plain.post(
        "/ui/login",
        data={"api_key": API_KEY},
        headers={"Origin": "https://testserver.evil.example"},
        follow_redirects=False,
    )
    assert other_host.status_code == 403
    assert plain.post("/ui/logout", headers={"Origin": "null"}).status_code == 403
    assert plain.post("/ui/logout", headers={"Origin": "https://["}).status_code == 403

    configured = client_factory(ui_public_origin="https://jobs.example.org")
    rewritten = configured.post(
        "/ui/login",
        data={"api_key": API_KEY},
        headers={"Origin": "https://jobs.example.org", "Host": "127.0.0.1:8000"},
        follow_redirects=False,
    )
    assert rewritten.status_code == 303


def test_login_body_is_bounded(client_factory):
    client = client_factory()
    declared = client.post("/ui/login", content=b"api_key=" + b"x" * 5000, follow_redirects=False)
    assert declared.status_code == 413

    def chunks():
        for _ in range(10):
            yield b"x" * 1000

    streamed = client.post("/ui/login", content=chunks(), follow_redirects=False)
    assert streamed.status_code == 413


def test_login_stops_reading_an_oversized_stream_early(client_factory):
    """Direct ASGI call (TestClient buffers bodies): no Content-Length, 1000-byte
    chunks; the route must answer 413 after reading about 4 KiB, not drain
    the whole stream."""
    import asyncio

    client_factory()
    from app.main import app

    received = 0
    sent = []

    async def receive():
        nonlocal received
        received += 1
        return {"type": "http.request", "body": b"x" * 1000, "more_body": received < 100}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/ui/login",
        "raw_path": b"/ui/login",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413
    assert received <= 5


def test_malformed_cookie_redirects_instead_of_failing(client_factory):
    client = client_factory()
    future = "9999999999"

    def page(value: str):
        # raw header: the cookie jar would drop or re-encode odd values
        header = f"{auth.COOKIE_NAME}={value}".encode("latin-1")
        return client.get("/ui/", headers={"Cookie": header}, follow_redirects=False)

    for value in (
        f"{future}.{'é' * 64}",
        f"²{future}.{'a' * 64}",
        f"{future}.{'A' * 64}",
        f"{future}.{'a' * 63}",
        f"{'9' * 40}.{'a' * 64}",
    ):
        assert page(value).status_code == 303, value
    # positive control: a well-formed token sent the same way is accepted
    assert page(auth.issue_session_token(API_KEY, hours=1)).status_code == 200


def test_login_is_rate_limited(client_factory):
    client = client_factory()
    codes = [
        client.post("/ui/login", data={"api_key": "x"}, follow_redirects=False).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


def test_session_token_expiry_tampering_and_key_rotation():
    token = auth.issue_session_token(API_KEY, hours=1, now=1_000_000)
    assert auth.session_token_valid(token, API_KEY, now=1_000_000 + 3599)
    assert not auth.session_token_valid(token, API_KEY, now=1_000_000 + 3600)
    assert not auth.session_token_valid(token, "rotated-key", now=1_000_000)
    expiry, _, signature = token.partition(".")
    forged = f"{int(expiry) + 3600}.{signature}"
    assert not auth.session_token_valid(forged, API_KEY, now=1_000_000)
    for junk in (None, "", "abc", "123.", ".abc", "-5.abc"):
        assert not auth.session_token_valid(junk, API_KEY, now=1_000_000)


def test_logout_clears_cookie(client_factory):
    client = client_factory()
    _login(client)
    response = client.post("/ui/logout", follow_redirects=False)
    assert response.status_code == 303
    assert 'jobtriage_ui=""' in response.headers["set-cookie"]


def test_security_headers_on_every_ui_response(client_factory):
    client = client_factory()
    for response in (client.get("/ui/login"), client.get("/ui/", follow_redirects=False)):
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src" not in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        # same-origin, not no-referrer: no-referrer makes browsers send
        # `Origin: null` on the login form POST, which is refused below
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"
    # the API keeps its own headers
    assert "content-security-policy" not in client.get("/api/v1/health").headers


# --- read-only guarantee ----------------------------------------------------------


def test_readonly_session_refuses_orm_and_core_writes(writer, reader):
    _job(writer, 1)
    reader.add(
        JobRecord(
            fingerprint="x",
            source="s",
            title="t",
            company="c",
            url="u",
            score=1,
            recommendation="SKIP",
        )
    )
    with pytest.raises(ReadOnlySessionError):
        reader.flush()
    reader.rollback()
    with pytest.raises(Exception, match="readonly|read-only|query_only"):
        reader.execute(
            insert(JobRecord).values(
                fingerprint="y",
                source="s",
                title="t",
                company="c",
                url="u",
                score=1,
                recommendation="SKIP",
                status="NEW",
                data_confidence=0.0,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
    reader.rollback()
    # still read-only after a rollback (connection-level setting)
    with pytest.raises(Exception, match="readonly|read-only|query_only"):
        reader.execute(
            insert(JobRecord).values(
                fingerprint="z",
                source="s",
                title="t",
                company="c",
                url="u",
                score=1,
                recommendation="SKIP",
                status="NEW",
                data_confidence=0.0,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
    reader.rollback()
    assert writer.scalar(select(func.count(JobRecord.id))) == 1


def test_readonly_engine_rejects_unknown_database_kinds():
    with pytest.raises(ValueError):
        build_readonly_engine("mysql://u@h/db")


def test_pages_make_no_network_connection_and_write_nothing(writer, client_factory, monkeypatch):
    for n in range(1, 4):
        _job(writer, n)
    _application(writer)
    client = client_factory(gmail_username=APPLICANT)
    _login(client)

    real_connect = socket.socket.connect
    outbound = []

    def guarded_connect(sock, address):
        # the Windows event loop pairs two loopback sockets internally
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return real_connect(sock, address)
        outbound.append(address)
        raise AssertionError("the UI must not open a network connection")

    def refuse(address, *_args, **_kwargs):
        outbound.append(address)
        raise AssertionError("the UI must not open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    before = writer.scalar(select(func.count(ApplicationEventRecord.id)))
    for path in (
        "/ui/",
        "/ui/vacancies",
        "/ui/vacancies?fit_tier=GOOD_MATCH",
        "/ui/vacancies/1",
        "/ui/applications",
        "/ui/applications/1",
        "/ui/inbox",
        "/ui/follow-ups",
    ):
        assert client.get(path).status_code == 200, path
    writer.expire_all()
    assert writer.scalar(select(func.count(ApplicationEventRecord.id))) == before
    assert outbound == []


# --- vacancies --------------------------------------------------------------------


def test_vacancy_list_escapes_html_and_links_only_http_urls(writer, client_factory):
    _job(writer, 1, title="<script>alert(1)</script> Dev", url="javascript:alert(1)")
    _job(writer, 2, url="https://jobs.example.invalid/ok")
    client = client_factory()
    _login(client)
    page = client.get("/ui/vacancies").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page

    detail_bad = client.get("/ui/vacancies/1").text
    assert 'href="javascript:' not in detail_bad
    assert "not an http(s) link" in detail_bad
    detail_ok = client.get("/ui/vacancies/2").text
    assert 'href="https://jobs.example.invalid/ok"' in detail_ok
    assert 'rel="noopener noreferrer nofollow"' in detail_ok


def test_tables_label_every_cell_for_the_narrow_screen_layout(writer, client_factory):
    # style.css stacks .stack tables into cards below 760px and shows each
    # cell's data-label; a cell without one would lose its column name.
    _job(writer, 1)
    _application(writer, ref="A")
    client = client_factory()
    _login(client)
    for path in ("/ui/vacancies", "/ui/vacancies/1", "/ui/applications", "/ui/applications/1"):
        page = client.get(path).text
        assert 'stack">' in page
        body = page.split("<tbody>", 1)[1]
        assert "<td>" not in body, path
    assert 'data-label="Why"' in client.get("/ui/vacancies").text


def test_vacancy_detail_shows_tier_criteria_and_404(writer, client_factory):
    _job(writer, 1)
    client = client_factory()
    _login(client)
    page = client.get("/ui/vacancies/1").text
    for text in ("Fit tier", "Criteria", "skills", "location", "duplicate", "Why this tier"):
        assert text in page
    assert client.get("/ui/vacancies/999").status_code == 404


def test_keyset_pagination_and_sql_filters(writer, reader):
    for n in range(1, 31):
        _job(writer, n, location="Darmstadt" if n % 2 else "Frankfurt am Main")
    first = queries.list_vacancies(reader, queries.VacancyFilters())
    assert [r.job.id for r in first.rows] == list(range(30, 5, -1))
    assert first.next_before_id == 6
    second = queries.list_vacancies(reader, queries.VacancyFilters(before_id=6))
    assert [r.job.id for r in second.rows] == [5, 4, 3, 2, 1]
    assert second.next_before_id is None

    darmstadt = queries.list_vacancies(reader, queries.VacancyFilters(city="darmst"))
    assert {r.job.id % 2 for r in darmstadt.rows} == {1}
    # LIKE wildcards in the search text are literal
    assert queries.list_vacancies(reader, queries.VacancyFilters(q="%")).rows == []


def _fake_assess(tiers):
    def assess(_db, job_id):
        tier = tiers(job_id)
        assessment = FitAssessment(job_id=job_id, company="c", title="t")
        assessment.criteria = [
            Criterion("remote", "HYBRID" if job_id % 2 else "ONSITE", "r"),
            Criterion("duplicate", "CLEAR", "d"),
        ]
        # the tier rule itself is covered by Stage 19; here only grouping matters
        assessment._tier = lambda: (tier, [f"why {job_id}"])
        return assessment

    return assess


def test_computed_filter_respects_scan_budget_and_says_so(writer, reader):
    for n in range(1, 21):
        _job(writer, n)
    good_ids = {3, 4}
    assess = _fake_assess(lambda i: "GOOD_MATCH" if i in good_ids else "PARTIAL_MATCH")
    page = queries.list_vacancies(
        reader,
        queries.VacancyFilters(fit_tier="GOOD_MATCH"),
        assess=assess,
        page_size=5,
        scan_budget=8,
    )
    assert page.rows == []
    assert page.scanned == 8
    assert page.budget_exhausted
    assert page.next_before_id == 13  # continue from the last scanned id

    rest = queries.list_vacancies(
        reader,
        queries.VacancyFilters(fit_tier="GOOD_MATCH", before_id=page.next_before_id),
        assess=assess,
        page_size=5,
        scan_budget=20,
    )
    assert [r.job.id for r in rest.rows] == [4, 3]
    assert not rest.budget_exhausted
    assert rest.next_before_id is None


def test_work_mode_filter(writer, reader):
    for n in range(1, 7):
        _job(writer, n)
    assess = _fake_assess(lambda _i: "PARTIAL_MATCH")
    page = queries.list_vacancies(
        reader, queries.VacancyFilters(mode="hybrid"), assess=assess, page_size=10
    )
    assert [r.job.id for r in page.rows] == [5, 3, 1]


# --- dashboard ---------------------------------------------------------------------


def test_dashboard_unknown_instead_of_zero(writer, reader):
    for n in range(1, 6):
        _job(writer, n)
    settings = _settings()
    zone = queries.operator_zone("Europe/Berlin")
    over_budget = queries.dashboard_summary(
        reader,
        settings,
        zone,
        now=NOW,
        assess=_fake_assess(lambda _i: "GOOD_MATCH"),
        scan_budget=3,
    )
    assert over_budget.tiers["GOOD_MATCH"].state == "UNKNOWN"
    assert over_budget.tiers["GOOD_MATCH"].value is None
    assert over_budget.inbox_review.state == "NOT_CONFIGURED"
    assert over_budget.inbox_review.value is None

    within = queries.dashboard_summary(
        reader, settings, zone, now=NOW, assess=_fake_assess(lambda _i: "GOOD_MATCH")
    )
    assert within.tiers["GOOD_MATCH"].value == 5
    assert within.tiers["NOT_SUITABLE"].value == 0
    assert within.jobs_total == 5
    assert within.jobs_last_24h == 5


def test_dashboard_page_renders_not_configured(writer, client_factory):
    _job(writer, 1)
    client = client_factory()
    _login(client)
    page = client.get("/ui/").text
    assert "NOT_CONFIGURED" in page
    assert "GMAIL_USERNAME is not set" in page


# --- applications / follow-ups / inbox ---------------------------------------------


def test_applications_list_detail_and_filter(writer, client_factory):
    _application(writer, ref="A", company="Alpha <b>GmbH</b>")
    _application(writer, ref="B", status=S.PACKAGE_READY, company="Beta AG")
    client = client_factory()
    _login(client)
    page = client.get("/ui/applications").text
    assert "Alpha &lt;b&gt;GmbH&lt;/b&gt;" in page and "Beta AG" in page
    only = client.get("/ui/applications?status=PACKAGE_READY").text
    assert "Beta AG" in only and "Alpha" not in only
    detail = client.get("/ui/applications/1").text
    assert "History" in detail and "CREATED" in detail
    assert client.get("/ui/applications/99").status_code == 404


def test_follow_up_buckets_use_operator_local_date(writer, reader):
    # next_followup_at = submission + 14 days = 2026-10-25T23:30Z = 26 Oct 00:30 Berlin
    _application(writer, ref="due", occurred_at=datetime(2026, 10, 11, 23, 30, tzinfo=UTC))
    _application(writer, ref="late", occurred_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
    _application(writer, ref="soon", occurred_at=datetime(2026, 10, 15, 9, 0, tzinfo=UTC))
    _application(writer, ref="prep", status=S.PACKAGE_READY)
    result = queries.follow_ups(reader, queries.operator_zone("Europe/Berlin"), now=NOW)
    refs = lambda items: [f.application.source_reference for f in items]  # noqa: E731
    assert refs(result.due_today) == ["due"]
    assert refs(result.overdue) == ["late"]
    assert refs(result.next_7_days) == ["soon"]
    assert result.due_count == 2
    # the same instant in UTC would still be "yesterday"
    utc = queries.follow_ups(reader, queries.operator_zone("UTC"), now=NOW)
    assert refs(utc.overdue) == ["late", "due"]


def test_inbox_is_a_dry_run_over_stored_mail(writer, reader, monkeypatch):
    _application(writer, ref="A")
    message = InboundMessage(
        account_key=APPLICANT,
        message_key="mid:abc",
        message_id_header="<abc@acme.example>",
        reply_to_ids=(),
        from_address="jobs@acme.example",
        from_display_name="Acme GmbH",
        to_addresses=(APPLICANT,),
        subject="Ihre Bewerbung als Backend Developer",
        body=(
            "Leider müssen wir Ihnen mitteilen, dass wir uns für andere Bewerber entschieden haben."
        ),
        has_html=False,
        occurred_at=NOW - timedelta(days=1),
    )
    calls = []

    def stored(db, settings, *, since):
        calls.append(since)
        return [message]

    monkeypatch.setattr(queries, "stored_inbound_messages", stored)
    before = writer.scalar(select(func.count(ApplicationEventRecord.id)))
    box = queries.inbox(reader, _settings(gmail_username=APPLICANT), now=NOW)
    assert box.state == "OK"
    assert calls == [NOW - timedelta(days=queries.INBOX_LOOKBACK_DAYS)]
    [result] = box.results
    assert result.dry_run
    assert not result.event_recorded
    writer.expire_all()
    assert writer.scalar(select(func.count(ApplicationEventRecord.id))) == before


def test_safe_external_url():
    assert queries.safe_external_url("https://a.example/x") == "https://a.example/x"
    assert queries.safe_external_url("HTTP://a.example") == "HTTP://a.example"
    for bad in (None, "", "javascript:alert(1)", "data:text/html,x", "//a.example", "https://"):
        assert queries.safe_external_url(bad) is None


def test_long_words_wrap_instead_of_widening_the_page():
    # Found on real data: a 64-character URL in a posting widened a 390px
    # screen. The description must break anywhere; the rest of main breaks
    # long words without changing table column sizing.
    from app.ui.routes import UI_DIR

    css = (UI_DIR / "static" / "style.css").read_text(encoding="utf-8")
    description = next(line for line in css.splitlines() if line.startswith(".description {"))
    assert "overflow-wrap: anywhere" in description
    main = next(line for line in css.splitlines() if line.startswith("main {"))
    assert "overflow-wrap: break-word" in main
