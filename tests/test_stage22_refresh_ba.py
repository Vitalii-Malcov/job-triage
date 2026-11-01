"""Stage 22 T6: the Bundesagentur refresh CLI (`scripts/stage22_refresh_ba.py`).

Every dangerous path is exercised against a throw-away SQLite file and a
`httpx.MockTransport`; nothing here reaches the network.
"""

import hashlib
import imaplib
import json
import smtplib
import sqlite3
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.collectors.bundesagentur import BundesagenturCollector, _map_posting
from app.core.config import get_settings
from app.core.database_target import DatabaseKind
from app.db.models import Base, JobRecord
from app.db.repositories import STRUCTURED_JOB_FACTS, refresh_job_structured_facts, upsert_job
from app.models.job import Job, JobScore
from app.services.readiness import expected_alembic_heads
from app.services.telegram import TelegramNotifier
from scripts import stage22_refresh_ba as cli
from scripts.stage21_assess_vacancies import SnapshotError

SCORE = JobScore(score=42, recommendation="MAYBE")
REF_A, REF_B, REF_C = "10001-1000000001-S", "10001-1000000002-S", "10001-1000000003-S"


# --- fixtures and helpers ---------------------------------------------------------------


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BUNDESAGENTUR_API_KEY", "test-key")
    monkeypatch.setattr("app.collectors.bundesagentur.asyncio.sleep", _no_sleep)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "refresh.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    session = sessionmaker(engine)()
    from app.db.candidate_profile_repository import get_or_create_candidate_profile

    get_or_create_candidate_profile(session)  # --create-new needs an existing profile
    session.close()
    engine.dispose()
    return path


def posting(ref=REF_A, title="Python Entwickler", company="ACME GmbH", **extra) -> dict:
    data = {
        "referenznummer": ref,
        "stellenangebotsTitel": title,
        "firma": company,
        "stellenlokationen": [
            {
                "breite": 50.1,
                "laenge": 8.7,
                "adresse": {"ort": "Frankfurt", "plz": "60311", "region": "HESSEN"},
            }
        ],
        "homeofficemoeglich": True,
        "homeofficetyp": "TEILWEISE",
        "quereinstiegGeeignet": False,
    }
    data.update(extra)
    return data


def stored_job(raw: dict, *, with_reference=True) -> Job:
    """What an earlier (pre-T6) collector run stored: no structured facts."""
    job = _map_posting(raw)
    blank = {name: None for name in STRUCTURED_JOB_FACTS}
    blank["source_reference"] = job.source_reference if with_reference else None
    return job.model_copy(update=blank)


def seed(db_path, jobs: list[Job]) -> list[int]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    session = sessionmaker(engine, expire_on_commit=False)()
    ids = [upsert_job(session, job, SCORE)[0].id for job in jobs]
    session.close()
    engine.dispose()
    return ids


class Api:
    """Mock BA API: search pages (by query term) and job details."""

    def __init__(self, results, details=None, fail=None):
        self.results = results  # {keyword: [posting, ...]} or a list for every keyword
        self.details = details or {}
        self.fail = fail or {}  # keyword -> callable(request) -> response/raise
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "/pc/v4/jobdetails/" in request.url.path:
            return httpx.Response(200, json={"stellenangebotsBeschreibung": "Python, SQL, pytest"})
        keyword = request.url.params.get("was", "")
        if keyword in self.fail:
            return self.fail[keyword](request)
        items = self.results[keyword] if isinstance(self.results, dict) else self.results
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("size", "100"))
        chunk = items[(page - 1) * size : page * size]
        return httpx.Response(
            200, json={"ergebnisliste": chunk, "maxErgebnisse": len(items), "page": page}
        )

    def factory(self, **collector_overrides):
        def make(query):
            return BundesagenturCollector(
                api_key="test-key",
                keywords=query.keywords,
                location=query.location,
                radius_km=query.radius_km,
                home_office_only=query.home_office_only,
                max_pages=collector_overrides.get("max_pages", query.max_pages),
                page_size=collector_overrides.get("page_size", 100),
                max_retries=3,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
            )

        return make


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table(path: Path, sql: str):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def run(db_path, api, *args, snapshot=None, **overrides):
    argv = ["--database", str(db_path), *args]
    if "--apply" in args and snapshot is not None:
        argv += ["--snapshot", str(snapshot)]
    return cli.run(argv, collector_factory=api.factory(**overrides))


# --- dry run ---------------------------------------------------------------------------------


def test_dry_run_reports_everything_and_writes_nothing(db_path, capsys):
    seed(
        db_path,
        [
            stored_job(posting(REF_A)),
            stored_job(posting(REF_B, title="Other")),
            stored_job(posting(REF_C, title="Gone")),
        ],
    )
    api = Api([posting(REF_A), posting(REF_B, title="Other"), posting("10001-1000000009-S")])
    before = sha(db_path)

    assert run(db_path, api, "--json") == 0

    out = capsys.readouterr().out.splitlines()
    report = json.loads(out[0])
    assert (report["found"], report["existing"], report["new"], report["to_update"]) == (3, 2, 1, 2)
    assert report["fields_added"]["workplace_lat"] == 2
    assert report["fields_added"]["home_office_possible"] == 2
    assert report["fields_added"].get("source_reference") is None  # already stored
    assert report["reference_conflicts"] == 0
    assert report["not_returned"] == 1 and report["stored_ba_rows"] == 3
    assert out[-1] == "DRY RUN: nothing written"
    assert sha(db_path) == before


def test_dry_run_is_the_default_and_dry_run_flag_is_accepted(db_path):
    seed(db_path, [stored_job(posting())])
    before = sha(db_path)
    assert run(db_path, Api([posting()])) == 0
    assert run(db_path, Api([posting()]), "--dry-run") == 0
    assert sha(db_path) == before


def test_missing_from_results_is_never_treated_as_closed(db_path, capsys):
    ids = seed(db_path, [stored_job(posting(REF_C, title="Gone"))])
    engine = sqlite3.connect(db_path)
    engine.execute(
        "INSERT INTO applications (source, source_reference, job_id, company_name, role_title, "
        "application_channel, employer_domains_json, current_status, last_status_changed_at, "
        "version, created_at, updated_at) VALUES ('bundesagentur', ?, ?, 'ACME', 'x', 'EMAIL', "
        "'[]', 'SUBMITTED', '2026-10-01', 1, '2026-10-01', '2026-10-01')",
        (REF_C, ids[0]),
    )
    engine.commit()
    engine.close()
    api = Api([posting(REF_A)])
    assert run(db_path, api, "--apply", "--create-new", snapshot=db_path.parent / "s") == 0
    out = capsys.readouterr().out
    assert "1 with an application" in out and "NOT evidence" in out
    assert table(db_path, "select status from jobs where title = 'Gone'") == [("NEW",)]
    assert table(db_path, "select count(*) from applications") == [(1,)]


# --- apply ---------------------------------------------------------------------------------


def test_apply_needs_a_snapshot_and_refuses_before_any_request(db_path, capsys):
    seed(db_path, [stored_job(posting())])
    api = Api([posting()])
    before = sha(db_path)
    assert cli.run(["--database", str(db_path), "--apply"], collector_factory=api.factory()) == 2
    assert "needs --snapshot" in capsys.readouterr().err
    assert api.requests == [] and sha(db_path) == before


def test_apply_fills_facts_only_and_keeps_everything_else(db_path, tmp_path, capsys):
    seed(db_path, [stored_job(posting(REF_A), with_reference=False)])
    row_before = table(
        db_path,
        "select id, fingerprint, title, company, url, description, score, recommendation, "
        "status, first_seen_at, last_seen_at, skills_json from jobs",
    )
    api = Api([posting(REF_A)])
    assert run(db_path, api, "--apply", snapshot=tmp_path / "snap") == 0
    out = capsys.readouterr().out
    assert "updated: 1 vacancies" in out and "protected tables unchanged" in out
    assert "new vacancies not stored" in out and out.rstrip().endswith("APPLIED")

    lat, lon, plz, region, count, ho, ho_type, quer, ref = table(
        db_path,
        "select workplace_lat, workplace_lon, workplace_postal_code, workplace_region, "
        "workplace_count, home_office_possible, home_office_type, quereinstieg_suitable, "
        "source_reference from jobs",
    )[0]
    assert (lat, lon, plz, region, count, ho, ho_type, quer, ref) == (
        50.1,
        8.7,
        "60311",
        "HESSEN",
        1,
        1,
        "TEILWEISE",
        0,
        REF_A,
    )
    assert (
        table(
            db_path,
            "select id, fingerprint, title, company, url, description, score, recommendation, "
            "status, first_seen_at, last_seen_at, skills_json from jobs",
        )
        == row_before
    )
    snapshots = list((tmp_path / "snap").glob("*.db"))
    assert len(snapshots) == 1
    assert table(snapshots[0], "select workplace_lat from jobs") == [(None,)]  # pre-write copy


def test_second_apply_changes_nothing(db_path, tmp_path, capsys):
    seed(db_path, [stored_job(posting(REF_A)), stored_job(posting(REF_B, title="Two"))])
    api = Api([posting(REF_A), posting(REF_B, title="Two")])
    assert run(db_path, api, "--apply", snapshot=tmp_path / "s1") == 0
    capsys.readouterr()
    after_first = table(db_path, "select * from jobs order by id")
    assert run(db_path, api, "--apply", "--json", snapshot=tmp_path / "s2") == 0
    out = capsys.readouterr().out
    assert json.loads(out.splitlines()[0])["to_update"] == 0
    assert "updated: 0 vacancies" in out
    assert table(db_path, "select * from jobs order by id") == after_first


def test_limit_applies_a_subset_and_reports_the_rest(db_path, tmp_path, capsys):
    seed(db_path, [stored_job(posting(r, title=r)) for r in (REF_A, REF_B, REF_C)])
    api = Api([posting(r, title=r) for r in (REF_A, REF_B, REF_C)])
    assert run(db_path, api, "--apply", "--limit", "2", snapshot=tmp_path / "s") == 0
    out = capsys.readouterr().out
    assert "updated: 2 vacancies" in out and "deferred updates=1" in out
    assert table(db_path, "select count(*) from jobs where workplace_lat is not null") == [(2,)]
    assert run(db_path, api, "--apply", snapshot=tmp_path / "s") == 0  # the rest
    assert table(db_path, "select count(*) from jobs where workplace_lat is not null") == [(3,)]


def test_a_stated_fact_replaces_the_stored_one_but_an_omitted_one_keeps_it(db_path, tmp_path):
    job = stored_job(posting()).model_copy(update={"workplace_postal_code": "99999"})
    seed(db_path, [job])
    changed = posting(stellenlokationen=[{"breite": 51.0, "laenge": 9.0, "adresse": {"ort": "X"}}])
    assert run(db_path, Api([changed]), "--apply", snapshot=tmp_path / "s") == 0
    lat, plz, ho = table(
        db_path, "select workplace_lat, workplace_postal_code, home_office_possible from jobs"
    )[0]
    assert (lat, plz, ho) == (51.0, "99999", 1)  # plz omitted -> kept; lat stated -> replaced


# --- new vacancies and conflicts -------------------------------------------------------------


def test_new_vacancies_are_only_counted_without_create_new(db_path, tmp_path, capsys):
    api = Api([posting(REF_A)])
    assert run(db_path, api, "--apply", snapshot=tmp_path / "s") == 0
    assert table(db_path, "select count(*) from jobs") == [(0,)]
    assert not any("jobdetails" in r.url.path for r in api.requests)


def test_create_new_stores_through_the_collector_path_and_is_idempotent(db_path, tmp_path, capsys):
    api = Api([posting(REF_A)])
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert "created: 1" in capsys.readouterr().out
    row = table(
        db_path,
        "select source_reference, description, workplace_lat, status from jobs",
    )
    assert row == [(REF_A, "Python, SQL, pytest", 50.1, "NEW")]
    assert table(db_path, "select count(*) from job_reference_tokens")[0][0] >= 0
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert "created: 0" in capsys.readouterr().out
    assert table(db_path, "select count(*) from jobs") == [(1,)]


def test_reference_with_another_fingerprint_is_a_conflict_not_a_new_row(db_path, tmp_path, capsys):
    # stored under the employer's own URL; today the same BA reference comes with the BA URL
    other = stored_job(posting(REF_A, externeURL="https://jobs.example.de/python"))
    seed(db_path, [other])
    api = Api([posting(REF_A)])
    before = table(db_path, "select * from jobs")
    assert run(db_path, api, "--apply", "--create-new", "--json", snapshot=tmp_path / "s") == 0
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["reference_conflicts"] == 1 and report["new"] == 0
    conflict = report["conflicts"][0]
    assert conflict["kind"] == "REFERENCE_EXISTS_WITH_OTHER_FINGERPRINT"
    assert conflict["reference"] == REF_A and conflict["existing_job_ids"] == [1]
    assert table(db_path, "select * from jobs") == before  # nothing created, nothing merged


def test_reference_derivable_from_a_ba_url_counts_as_taken(db_path, tmp_path, capsys):
    legacy = stored_job(posting(REF_A, title="Old title"), with_reference=False)  # BA url, no ref
    seed(db_path, [legacy])
    api = Api([posting(REF_A, title="New title")])
    assert run(db_path, api, "--apply", "--create-new", "--json", snapshot=tmp_path / "s") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["reference_conflicts"] == 1
    assert table(db_path, "select count(*) from jobs") == [(1,)]


def test_stored_reference_that_differs_blocks_the_update(db_path, tmp_path, capsys):
    job = stored_job(posting(REF_A)).model_copy(update={"source_reference": REF_B})
    seed(db_path, [job])
    assert run(db_path, Api([posting(REF_A)]), "--apply", "--json", snapshot=tmp_path / "s") == 0
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["conflicts"][0]["kind"] == "STORED_REFERENCE_DIFFERS"
    assert table(db_path, "select workplace_lat, source_reference from jobs") == [(None, REF_B)]


def test_same_reference_twice_in_the_results_is_not_created(db_path, tmp_path, capsys):
    api = Api([posting(REF_A, title="One"), posting(REF_A, title="Two")])
    assert run(db_path, api, "--apply", "--create-new", "--json", snapshot=tmp_path / "s") == 0
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["conflicts"][0]["kind"] == "SAME_REFERENCE_TWICE_IN_RESULTS"
    assert table(db_path, "select count(*) from jobs") == [(0,)]


# --- BA API failures and pagination ---------------------------------------------------------


def test_429_is_retried_then_succeeds(db_path, capsys):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ergebnisliste": [posting()], "maxErgebnisse": 1})

    api = Api({}, fail={"": flaky})
    assert run(db_path, api, "--json") == 0
    assert json.loads(capsys.readouterr().out.splitlines()[0])["found"] == 1 and calls["n"] == 2


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(429),
        lambda request: httpx.Response(401),
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request)),
        lambda request: httpx.Response(200, content=b"<html>waf</html>"),
    ],
    ids=["500", "429-exhausted", "401", "timeout", "non-json"],
)
def test_a_failed_search_is_reported_and_apply_writes_nothing(db_path, tmp_path, capsys, failure):
    seed(db_path, [stored_job(posting())])
    before = sha(db_path)
    api = Api({}, fail={"": failure})
    assert run(db_path, api, "--json") == 0  # dry run reports, exit 0
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["failed_queries"] == 1 and report["queries"][0]["error"]

    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 1
    assert "INCOMPLETE: a search failed" in capsys.readouterr().err
    assert sha(db_path) == before
    assert not (tmp_path / "s").exists()  # no snapshot either: nothing was attempted


def test_pagination_cut_off_is_reported(db_path, capsys):
    items = [posting(f"10001-{n:010d}-S", title=f"T{n}") for n in range(5)]
    api = Api(items)
    assert run(db_path, api, "--json", page_size=2, max_pages=2) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["found"] == 4 and report["truncated_queries"] == 1
    assert report["left_out_by_pagination"] == 1
    assert report["queries"][0]["total_results"] == 5


def test_collector_remembers_the_total_result_count():
    api = Api([posting()])
    collector = api.factory()(
        type(
            "Q",
            (),
            dict(keywords="", location="", radius_km=0, home_office_only=False, max_pages=3),
        )()
    )
    import asyncio

    asyncio.run(collector.fetch())
    assert collector.total_results == 1


# --- outbound lock-down -------------------------------------------------------------------------


def test_outbound_flags_are_forced_off_whatever_the_environment_says(db_path, monkeypatch):
    for name in cli.FORCED_OFF_FLAGS:
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret-pass")
    seen = {}
    api = Api([posting()])

    def factory(query):
        settings = get_settings()
        seen["flags"] = [getattr(settings, n.lower()) for n in cli.FORCED_OFF_FLAGS]
        seen["secrets"] = [getattr(settings, n.lower()) for n in cli.FORCED_EMPTY_SECRETS]
        return api.factory()(query)

    assert cli.run(["--database", str(db_path)], collector_factory=factory) == 0
    assert not any(seen["flags"]) and not any(seen["secrets"])
    import os

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "secret-token"  # restored afterwards
    assert os.environ["OUTBOUND_SENDING_ENABLED"] == "true"


def test_smtp_imap_and_foreign_hosts_are_blocked_while_the_tool_runs():
    with cli.outbound_guard():
        with pytest.raises(cli.OutboundBlockedError):
            smtplib.SMTP("smtp.example.com")
        with pytest.raises(cli.OutboundBlockedError):
            imaplib.IMAP4_SSL("imap.gmail.com")
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        with pytest.raises(cli.OutboundBlockedError):
            client.get("https://api.telegram.org/bot1/sendMessage")
        with pytest.raises(cli.OutboundBlockedError):
            client.get("https://www.employer.example/apply")
        assert client.get("https://rest.arbeitsagentur.de/x").status_code == 200
    assert smtplib.SMTP.__name__ == "SMTP"  # restored


def test_no_notification_is_sent_even_for_a_top_scoring_new_vacancy(db_path, tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("Telegram must not be used by the refresh tool")

    monkeypatch.setattr(TelegramNotifier, "send_job", boom)
    api = Api([posting()])
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert {r.url.host for r in api.requests} == {"rest.arbeitsagentur.de"}


# --- database protection -------------------------------------------------------------------


def test_apply_to_a_database_not_at_alembic_head_is_refused(db_path, tmp_path, capsys):
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE alembic_version SET version_num = 'old'")
    connection.commit()
    connection.close()
    before = sha(db_path)
    assert run(db_path, Api([posting()]), "--apply", snapshot=tmp_path / "s") == 2
    assert "REFUSED" in capsys.readouterr().err and sha(db_path) == before


def test_a_backup_copy_is_never_written(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "classify_path", lambda _p: DatabaseKind.BACKUP)
    before = sha(db_path)
    assert run(db_path, Api([posting()]), "--apply", snapshot=tmp_path / "s") == 2
    assert sha(db_path) == before


def test_foreign_key_violation_refuses_the_run(db_path, capsys):
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO job_reference_tokens (job_id, token, created_at) "
        "VALUES (999, 'ORPHAN', '2026-10-31')"
    )
    connection.commit()
    connection.close()
    assert run(db_path, Api([posting()])) == 2
    assert "foreign_key_check" in capsys.readouterr().err


def test_a_failed_snapshot_verification_stops_before_the_first_write(
    db_path, tmp_path, monkeypatch, capsys
):
    seed(db_path, [stored_job(posting())])

    def broken(_database, _copy):
        raise SnapshotError("snapshot differs")

    monkeypatch.setattr(cli, "_verify_snapshot", broken)
    before = sha(db_path)
    assert run(db_path, Api([posting()]), "--apply", snapshot=tmp_path / "s") == 2
    assert "snapshot differs" in capsys.readouterr().err and sha(db_path) == before


def test_a_competing_writer_blocks_the_apply(db_path, tmp_path, capsys):
    seed(db_path, [stored_job(posting())])
    other = sqlite3.connect(db_path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        assert run(db_path, Api([posting()]), "--apply", snapshot=tmp_path / "s") == 2
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert "write lock" in capsys.readouterr().err
    assert table(db_path, "select workplace_lat from jobs") == [(None,)]


def test_only_one_refresh_runs_at_a_time(db_path):
    with cli.exclusive_run_lock(db_path):
        with pytest.raises(cli.RefusedError):
            with cli.exclusive_run_lock(db_path):
                pass
    with cli.exclusive_run_lock(db_path):  # released afterwards
        pass


def test_a_change_in_a_protected_table_is_detected_and_reported(
    db_path, tmp_path, monkeypatch, capsys
):
    seed(db_path, [stored_job(posting(REF_A))])
    real = cli.write_updates

    def sneaky(engine, updates):
        connection = sqlite3.connect(db_path)
        connection.execute(
            "INSERT INTO user_profiles (name, skills_json, updated_at) "
            "VALUES ('intruder', '[]', '2026-10-31')"
        )
        connection.commit()
        connection.close()
        return real(engine, updates)

    monkeypatch.setattr(cli, "write_updates", sneaky)
    assert run(db_path, Api([posting(REF_A)]), "--apply", snapshot=tmp_path / "s") == 3
    err = capsys.readouterr().err
    assert "ALERT: protected tables changed: ['user_profiles']" in err


def test_a_failing_create_is_counted_and_the_run_is_incomplete(
    db_path, tmp_path, monkeypatch, capsys
):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("scoring bug")

    monkeypatch.setattr("app.services.collector_runner.enrich_and_persist_ba_job", boom)
    assert run(db_path, Api([posting()]), "--apply", "--create-new", snapshot=tmp_path / "s") == 1
    assert "failed=1" in capsys.readouterr().out


def test_option_validation(db_path, capsys):
    assert cli.run(["--database", str(db_path), "--limit", "0"]) == 2
    assert cli.run(["--database", str(db_path), "--create-new"]) == 2
    with pytest.raises(SystemExit):
        cli.run(["--database", str(db_path), "--apply", "--dry-run"])


# --- repository helper -------------------------------------------------------------------------


def test_refresh_helper_never_touches_score_status_or_an_existing_reference(db_path):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    session = sessionmaker(engine, expire_on_commit=False)()
    record, _ = upsert_job(session, stored_job(posting(REF_A)), SCORE)
    fresh = _map_posting(posting(REF_A)).model_copy(update={"source_reference": REF_B})
    changed = refresh_job_structured_facts(session, record, fresh)
    session.commit()
    row = session.get(JobRecord, record.id)
    assert "source_reference" not in changed and row.source_reference == REF_A
    assert (row.score, row.recommendation, row.status) == (42, "MAYBE", "NEW")
    assert row.workplace_lat == 50.1 and "workplace_lat" in changed
    assert refresh_job_structured_facts(session, row, fresh) == []  # idempotent
    session.close()
    engine.dispose()


# --- Codex round 1 ------------------------------------------------------------------------


def test_a_matched_row_never_takes_a_reference_another_row_owns(db_path, tmp_path, capsys):
    owner = stored_job(posting(REF_A, externeURL="https://jobs.example.de/a", title="Owner"))
    twin = stored_job(posting(REF_A, title="Twin"), with_reference=False)
    seed(db_path, [owner, twin])
    # a stored row with the BA url and no reference derives REF_A itself: conflict with the owner
    api = Api([posting(REF_A, title="Twin")])
    assert run(db_path, api, "--apply", "--json", snapshot=tmp_path / "s") == 0
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert report["conflicts"][0]["kind"] == "REFERENCE_EXISTS_WITH_OTHER_FINGERPRINT"
    assert report["to_update"] == 0
    assert table(db_path, "select workplace_lat, source_reference from jobs order by id") == [
        (None, REF_A),
        (None, None),
    ]


def test_a_reference_taken_after_the_plan_is_not_assigned(db_path, tmp_path):
    from app.services import ba_refresh

    ids = seed(db_path, [stored_job(posting(REF_A), with_reference=False)])
    job = _map_posting(posting(REF_A))
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    session = sessionmaker(engine)()
    planned = ba_refresh.PlannedUpdate(ids[0], job, ["source_reference"], [])
    # another row now owns the reference
    upsert_job(
        session,
        stored_job(posting(REF_A, externeURL="https://jobs.example.de/x", title="Late")),
        SCORE,
    )
    assert ba_refresh.apply_updates(session, [planned]) == 0
    session.commit()
    assert session.get(JobRecord, ids[0]).source_reference is None
    session.close()
    engine.dispose()


def test_create_new_without_a_candidate_profile_is_refused_before_any_write(
    db_path, tmp_path, capsys
):
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    for name in ("candidate_job_preferences", "candidate_skills", "candidate_languages"):
        connection.execute(f"DELETE FROM {name}")
    connection.execute("DELETE FROM candidate_profiles")
    connection.commit()
    connection.close()
    api = Api([posting(stellenangebotsart="AUSBILDUNG")])
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 2
    assert "candidate profile" in capsys.readouterr().err
    assert table(db_path, "select count(*) from candidate_profiles") == [(0,)]
    assert table(db_path, "select count(*) from jobs") == [(0,)]


def test_an_apprenticeship_is_stored_without_touching_the_profile(db_path, tmp_path, capsys):
    profiles = table(db_path, "select * from candidate_profiles")
    api = Api([posting(stellenangebotsart="AUSBILDUNG")])
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert "protected tables unchanged" in capsys.readouterr().out
    assert table(db_path, "select posting_type from jobs") == [("AUSBILDUNG",)]
    assert table(db_path, "select * from candidate_profiles") == profiles


def test_a_row_inserted_while_details_are_fetched_is_not_overwritten(db_path, tmp_path, capsys):
    rival = stored_job(posting(REF_A))

    class RacingApi(Api):
        def handler(self, request):
            if "/pc/v4/jobdetails/" in request.url.path and not getattr(self, "raced", False):
                self.raced = True
                seed(db_path, [rival.model_copy(update={"description": "ORIGINAL"})])
            return super().handler(request)

    api = RacingApi([posting(REF_A)])
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 0
    assert "skipped_conflict=1" in capsys.readouterr().out
    assert table(db_path, "select count(*), max(description), max(score) from jobs") == [
        (1, "ORIGINAL", 42)
    ]


def test_a_rejected_second_run_does_not_free_the_lock_of_the_first(db_path):
    with cli.exclusive_run_lock(db_path):
        with pytest.raises(cli.RefusedError):
            with cli.exclusive_run_lock(db_path):
                pass
        assert db_path.with_name(db_path.name + ".refresh-ba.lock").exists()
        with pytest.raises(cli.RefusedError):  # still held by the first
            with cli.exclusive_run_lock(db_path):
                pass


def test_snapshots_are_never_overwritten(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_stamp", lambda: "FIXED")
    first = cli.take_snapshot(db_path, tmp_path / "s")
    content = first.read_bytes()
    with pytest.raises(SnapshotError, match="never overwritten"):
        cli.take_snapshot(db_path, tmp_path / "s")
    assert first.read_bytes() == content


def test_a_profile_deleted_during_the_run_is_never_recreated(db_path, tmp_path, capsys):
    class DeletingApi(Api):
        def handler(self, request):
            if "/pc/v4/jobdetails/" in request.url.path:
                connection = sqlite3.connect(db_path)
                connection.execute("PRAGMA foreign_keys = OFF")
                for name in (
                    "candidate_job_preferences",
                    "candidate_skills",
                    "candidate_languages",
                ):
                    connection.execute(f"DELETE FROM {name}")
                connection.execute("DELETE FROM candidate_profiles")
                connection.commit()
                connection.close()
            return super().handler(request)

    api = DeletingApi([posting(stellenangebotsart="AUSBILDUNG")])
    # exit 3: the test itself deleted the profile (the digest reports that change);
    # the tool must not have re-created it or stored the vacancy
    assert run(db_path, api, "--apply", "--create-new", snapshot=tmp_path / "s") == 3
    assert "failed=1" in capsys.readouterr().out
    assert table(db_path, "select count(*) from candidate_profiles") == [(0,)]
    assert table(db_path, "select count(*) from jobs") == [(0,)]


def test_a_different_reference_stored_after_the_plan_blocks_the_facts(db_path):
    from app.services import ba_refresh

    ids = seed(db_path, [stored_job(posting(REF_A), with_reference=False)])
    planned = ba_refresh.PlannedUpdate(ids[0], _map_posting(posting(REF_A)), ["workplace_lat"], [])
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    session = sessionmaker(engine)()
    session.get(JobRecord, ids[0]).source_reference = REF_B  # set by another run meanwhile
    session.commit()
    assert ba_refresh.apply_updates(session, [planned]) == 0
    session.commit()
    assert session.get(JobRecord, ids[0]).workplace_lat is None
    session.close()
    engine.dispose()


# --- Stage 24: --discovery ------------------------------------------------------


def test_discovery_refuses_without_a_configured_search(db_path, capsys):
    api = Api([])
    assert (
        cli.run(["--database", str(db_path), "--discovery"], collector_factory=api.factory()) == 2
    )
    assert "discovery needs a configured commute search" in capsys.readouterr().err
    assert api.requests == []


def test_discovery_dry_run_uses_the_discovery_queries_and_writes_nothing(
    db_path, monkeypatch, capsys
):
    from app.models.candidate_search import CandidateSearchSettings, GeoPoint

    configured = CandidateSearchSettings(
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
    monkeypatch.setattr(
        "app.services.collector_runner._candidate_search_settings", lambda session: configured
    )
    seen: list[tuple[str, int | None, int | None]] = []

    def factory(query):
        seen.append((query.label, query.offer_type, query.published_within_days))
        return Api([]).factory()(query)

    before = sha(db_path)
    assert cli.run(["--database", str(db_path), "--discovery"], collector_factory=factory) == 0
    assert sha(db_path) == before
    assert {label for label, _, _ in seen} == {
        "discovery-commute",
        "discovery-remote",
        "discovery-remote-weekly",
    }
    assert all(offer == 1 for _, offer, _ in seen)
    assert {days for _, _, days in seen} == {None, 7}
    assert "DRY RUN" in capsys.readouterr().out
