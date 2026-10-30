"""Stage 22 P0-1: the full Bundesagentur reference is kept (`jobs.source_reference`)."""

import hashlib
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.collectors.ba_reference import (
    MAX_REFERENCE_LENGTH,
    ba_reference_from_url,
    extract_ba_references,
    is_valid_ba_reference,
    storable_reference,
)
from app.collectors.bundesagentur import _map_posting
from app.core.database_target import DatabaseKind
from app.db.gmail_analysis_repository import compute_context_fingerprint, get_job_candidates
from app.db.models import Base, JobRecord
from app.db.repositories import upsert_job
from app.models.application_destination import VacancyIdentity, vacancy_identity_matches
from app.models.application_lifecycle import Application
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.job import Job, JobScore
from app.services.application_duplicates import (
    VacancyView,
    assess_vacancy,
    bundesagentur_reference,
    job_association_mismatches,
)
from app.services.email_matching import JobCandidate, match_email_to_job
from app.services.readiness import expected_alembic_heads
from app.services.send_readiness import _identity_matches_job
from scripts import backfill_source_reference as backfill_cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BA = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
# Real reference shapes from the 2026-10-30 catalogue.
UNDERSCORE = "12265-444700_JB5270016-S"
DOTTED = "13410-k13572.15943-S"
SLASHED = "13319-896770/1_626382LS-S"
NUMERIC = "10001-1003457257-S"
SCORE = JobScore(score=50, recommendation="MAYBE")


# --- reference shapes -------------------------------------------------------------


@pytest.mark.parametrize("reference", [UNDERSCORE, DOTTED, SLASHED, NUMERIC, "12456-1640355-1-S"])
def test_every_observed_shape_is_valid_and_survives_the_url(reference):
    assert is_valid_ba_reference(reference)
    assert ba_reference_from_url(BA + reference) == reference
    assert ba_reference_from_url(BA + reference + "/") == reference


@pytest.mark.parametrize(
    "url",
    [
        "https://www.get-in-it.de/jobsuche/p254924",  # externeURL: no BA reference
        "https://jobs.example.de/jobsuche/jobdetail/10001-1-S",  # not the BA host
        "https://www.arbeitsagentur.de/jobsuche/suche?was=python",
        BA + "12265-444700",  # truncated token, not the BA shape
        "https://evil-arbeitsagentur.de.example.com/jobsuche/jobdetail/10001-1-S",
        "",
        None,
    ],
)
def test_urls_without_a_full_reference_give_none(url):
    assert ba_reference_from_url(url) is None


def test_a_percent_encoded_reference_is_decoded():
    assert ba_reference_from_url(BA + "13319-896770%2F1_626382LS-S") == SLASHED


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  " + UNDERSCORE + " ", UNDERSCORE),
        (UNDERSCORE, UNDERSCORE),
        ("P254924", "P254924"),  # any safe source reference may be stored
        (12345, "12345"),
        ("", None),
        ("   ", None),
        (None, None),
        (True, None),
        ("x" * (MAX_REFERENCE_LENGTH + 1), None),
        ("10001-12 34-S", None),
        ("10001-<script>-S", None),
    ],
)
def test_storable_reference(value, expected):
    assert storable_reference(value) == expected


def test_references_in_text_are_strict_and_bounded():
    text_ = (
        f"Ihre Bewerbung (Referenznummer {UNDERSCORE}). Link: {BA}{SLASHED} -- "
        f"Ref {DOTTED}, am 2026-10-30, PLZ 63150, Tel 069-1234-5, ID 12265-444700."
    )
    assert extract_ba_references(text_) == {UNDERSCORE, SLASHED, DOTTED}
    assert extract_ba_references("abc" + NUMERIC) == frozenset()  # glued to a word
    many = " ".join(f"10001-{n:010d}-S" for n in range(20))
    assert len(extract_ba_references(many)) == 8
    assert extract_ba_references(None) == frozenset()


# --- collector and persistence ----------------------------------------------------


def _posting(reference, url=None):
    posting = {"referenznummer": reference, "stellenangebotsTitel": "Python Dev", "firma": "ACME"}
    if url:
        posting["externeURL"] = url
    return posting


def test_the_collector_keeps_the_full_reference_also_with_an_external_url():
    job = _map_posting(_posting(UNDERSCORE, "https://www.get-in-it.de/jobsuche/p254924"))
    assert job.source_reference == UNDERSCORE
    assert str(job.url) == "https://www.get-in-it.de/jobsuche/p254924"
    assert str(_map_posting(_posting(SLASHED)).url) == BA + SLASHED


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "p0.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:  # the CLIs require the Alembic head
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for head in expected_alembic_heads():
            connection.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
    engine.dispose()
    return path


@pytest.fixture()
def db(db_path):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    session = sessionmaker(engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _job(reference, *, url=None, title="Python Dev", company="ACME", source="bundesagentur"):
    return Job(
        source=source,
        title=title,
        company=company,
        url=url or BA + (reference or "10001-1-S"),
        source_reference=reference,
    )


def test_upsert_stores_the_reference_and_never_blanks_or_replaces_it(db, caplog):
    external = "https://www.get-in-it.de/jobsuche/p254924"
    record, created = upsert_job(db, _job(UNDERSCORE, url=external), SCORE)
    assert created and record.source_reference == UNDERSCORE

    record, created = upsert_job(db, _job(None, url=external), SCORE)  # omitted
    assert not created and record.source_reference == UNDERSCORE
    record, _ = upsert_job(db, _job("   ", url=external), SCORE)  # blank
    assert record.source_reference == UNDERSCORE

    with caplog.at_level(logging.WARNING):
        record, _ = upsert_job(db, _job(NUMERIC, url=external), SCORE)  # conflict
    assert record.source_reference == UNDERSCORE
    assert "job_source_reference_conflict" in caplog.text


def test_a_later_observation_fills_a_missing_reference(db):
    external = "https://www.yourfirm.de/job/42"
    record, _ = upsert_job(db, _job(None, url=external), SCORE)
    assert record.source_reference is None
    record, created = upsert_job(db, _job(DOTTED, url=external), SCORE)
    assert not created and record.source_reference == DOTTED


def test_an_unstorable_reference_does_not_lose_the_job(db):
    record, created = upsert_job(db, _job("x" * 80, url="https://example.com/j/1"), SCORE)
    assert created and record.id is not None and record.source_reference is None


# --- migration ----------------------------------------------------------------------


def _cfg(path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


def test_migration_adds_the_column_keeps_rows_and_downgrades_cleanly(tmp_path):
    path = tmp_path / "migrate.db"
    cfg = _cfg(path)
    upgrade(cfg, "c7e2a9d4f1b3")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (fingerprint, source, title, company, location, url, "
                "description, skills_json, data_confidence, must_have_skills_json, "
                "nice_to_have_skills_json, score, recommendation, status, first_seen_at, "
                "last_seen_at) VALUES ('f1', 'bundesagentur', 'T', 'C', '', :url, '', '[]', "
                "0.5, '[]', '[]', 1, 'SKIP', 'NEW', '2026-10-30', '2026-10-30')"
            ),
            {"url": BA + UNDERSCORE},
        )
        conn.execute(
            text(
                "INSERT INTO job_reference_tokens (job_id, token, created_at) "
                "VALUES (1, 'TOKEN1', '2026-10-30')"
            )
        )
    upgrade(cfg, "head")
    inspector = inspect(engine)
    assert "source_reference" in {c["name"] for c in inspector.get_columns("jobs")}
    assert "ix_jobs_source_source_reference" in {i["name"] for i in inspector.get_indexes("jobs")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT source_reference FROM jobs")).scalar_one() is None
    downgrade(cfg, "c7e2a9d4f1b3")
    inspector = inspect(engine)
    assert "source_reference" not in {c["name"] for c in inspector.get_columns("jobs")}
    with engine.connect() as conn:  # rows and dependent rows survive both directions
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM job_reference_tokens")).scalar_one() == 1
    engine.dispose()


# --- backfill -------------------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _catalogue(db):
    for reference, url in [
        (None, BA + UNDERSCORE),
        (None, BA + SLASHED),
        (None, BA + DOTTED),
        (None, "https://www.get-in-it.de/jobsuche/p1"),
        (NUMERIC, "https://www.yourfirm.de/job/2"),
    ]:
        upsert_job(db, _job(reference, url=url, title=url), SCORE)
    upsert_job(db, _job(None, url="https://www.xing.com/jobs/1", source="xing"), SCORE)


def test_backfill_dry_run_apply_and_rerun(db, db_path, capsys):
    _catalogue(db)
    db.close()
    before = _sha(db_path)
    assert backfill_cli.run(["--database", str(db_path), "--json"]) == 0
    out = capsys.readouterr().out
    assert '"to_fill": 3' in out and '"not_derivable": 1' in out and '"already_set": 1' in out
    assert _sha(db_path) == before  # dry run writes nothing

    assert backfill_cli.run(["--database", str(db_path), "--apply"]) == 0
    assert "APPLIED: 3 rows written" in capsys.readouterr().out
    assert backfill_cli.run(["--database", str(db_path), "--apply"]) == 0
    assert "APPLIED: 0 rows written" in capsys.readouterr().out

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as conn:
        stored = dict(conn.execute(select(JobRecord.url, JobRecord.source_reference)).all())
    engine.dispose()
    assert stored[BA + SLASHED] == SLASHED and stored[BA + UNDERSCORE] == UNDERSCORE
    assert stored["https://www.yourfirm.de/job/2"] == NUMERIC  # never overwritten
    assert stored["https://www.get-in-it.de/jobsuche/p1"] is None
    assert stored["https://www.xing.com/jobs/1"] is None  # other sources untouched


def test_backfill_never_overwrites_a_reference_set_meanwhile(db):
    record, _ = upsert_job(db, _job(None, url=BA + UNDERSCORE), SCORE)
    plan = backfill_cli.plan(db)
    record.source_reference = "10001-9-S"  # a collector run stored it in between
    db.commit()
    assert backfill_cli.apply(db, plan["updates"]) == 0
    db.commit()
    assert db.get(JobRecord, record.id).source_reference == "10001-9-S"


def test_backfill_reports_one_reference_on_several_rows(db):
    upsert_job(db, _job(None, url=BA + UNDERSCORE, title="A"), SCORE)
    upsert_job(db, _job(UNDERSCORE, url="https://www.get-in-it.de/p9", title="B"), SCORE)
    plan = backfill_cli.plan(db)
    assert plan["shared_references"] == 1
    assert plan["shared_examples"] == [{"reference": UNDERSCORE, "rows": 2}]


def test_backfill_refuses_backups_and_needs_a_snapshot_for_production(
    db, db_path, tmp_path, monkeypatch, capsys
):
    _catalogue(db)
    db.close()
    backup = tmp_path / "copy.backup.db"
    shutil.copyfile(db_path, backup)
    assert backfill_cli.run(["--database", str(backup), "--apply"]) == 2

    monkeypatch.setattr(backfill_cli, "classify_path", lambda _p: DatabaseKind.PRODUCTION_RUNTIME)
    before = _sha(db_path)
    assert backfill_cli.run(["--database", str(db_path), "--apply"]) == 2
    assert "needs --snapshot" in capsys.readouterr().err
    assert _sha(db_path) == before
    snapshots = tmp_path / "snapshots"
    args = ["--database", str(db_path), "--apply", "--snapshot", str(snapshots)]
    assert backfill_cli.run(args) == 0
    (copy,) = snapshots.iterdir()
    assert ".pre-source-reference-" in copy.name


# --- e-mail matching ------------------------------------------------------------------


def _candidate(job_id, reference, url="https://www.get-in-it.de/jobsuche/p254924", status="NEW"):
    return JobCandidate(job_id, "Junior Python Developer", "ACME", "", url, status, reference)


def _match(body, candidates):
    return match_email_to_job(
        subject="Ihre Bewerbung",
        body_plain=body,
        from_address="jobs@example.org",
        job_candidates=candidates,
        thread_prior_matches=[],
    )


def test_a_full_reference_in_the_mail_matches_the_stored_reference():
    result = _match(
        f"Referenznummer: {UNDERSCORE}", [_candidate(1, UNDERSCORE), _candidate(2, None)]
    )
    assert result.matched_job_id == 1 and result.confidence == "HIGH"
    assert result.evidence[0].kind == "JOB_REFERENCE"


def test_the_full_reference_match_is_case_insensitive_and_symmetric():
    assert _match(f"ref {DOTTED.upper()}", [_candidate(4, DOTTED)]).matched_job_id == 4
    assert _match(f"ref {DOTTED}", [_candidate(4, DOTTED.upper())]).matched_job_id == 4


def test_two_jobs_with_the_same_reference_stay_ambiguous():
    result = _match(f"Referenznummer {SLASHED}", [_candidate(1, SLASHED), _candidate(2, SLASHED)])
    assert result.match_type == "AMBIGUOUS" and result.matched_job_id is None
    assert {c.job_id for c in result.candidates} == {1, 2}


def test_a_truncated_reference_does_not_match_a_full_one():
    result = _match("Referenznummer 12265-444700", [_candidate(1, UNDERSCORE)])
    assert result.matched_job_id is None
    assert result.match_type != "AMBIGUOUS"


def test_the_context_fingerprint_is_unchanged_without_stored_references():
    plain = [_candidate(1, None)]
    legacy = [JobCandidate(1, "Junior Python Developer", "ACME", "", plain[0].url, "NEW")]
    assert compute_context_fingerprint(plain, []) == compute_context_fingerprint(legacy, [])
    assert compute_context_fingerprint([_candidate(1, NUMERIC)], []) != compute_context_fingerprint(
        plain, []
    )


def test_candidates_are_found_by_stored_reference_outside_the_recency_window(db):
    old, _ = upsert_job(db, _job(SLASHED, url="https://www.get-in-it.de/p7", title="old"), SCORE)
    for n in range(3):
        upsert_job(db, _job(None, url=f"https://example.com/j/{n}", title=f"new {n}"), SCORE)
    assert old.id not in {c.job_id for c in get_job_candidates(db, limit=2)}
    found = get_job_candidates(db, limit=2, source_references=frozenset({SLASHED.upper()}))
    assert old.id in {c.job_id for c in found}
    assert next(c for c in found if c.job_id == old.id).source_reference == SLASHED


# --- duplicates and send readiness ------------------------------------------------------


def _application(reference, url=None):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    return Application(
        id=7,
        candidate_profile_id=None,
        job_id=None,
        company_name="Other Name GmbH",
        role_title="Something else",
        location=None,
        source="bundesagentur",
        source_reference=reference,
        external_job_id=None,
        vacancy_fingerprint=None,
        application_channel=C.EMAIL,
        destination_url=url,
        employer_domains=(),
        review_package_id=None,
        current_status=S.SUBMITTED,
        submitted_at=now,
        received_at=None,
        last_status_changed_at=now,
        last_employer_contact_at=None,
        next_followup_at=None,
        followup_required=False,
        version=1,
        created_at=now,
        updated_at=now,
    )


def test_a_stored_reference_finds_the_application_behind_an_external_url():
    vacancy = VacancyView(
        3,
        "bundesagentur",
        "ACME",
        "Dev",
        "https://www.get-in-it.de/p1",
        source_reference=UNDERSCORE,
    )
    (finding,) = assess_vacancy(vacancy, [_application(UNDERSCORE)])
    assert "SAME_REFERENCE" in finding.signals and finding.strength == "STRONG"


def test_a_slashed_reference_in_a_ba_url_is_compared_whole():
    assert bundesagentur_reference(BA + SLASHED) == SLASHED.casefold()
    view = VacancyView(3, "bundesagentur", "ACME", "Python Dev", BA + SLASHED)
    common = {"company": "ACME", "role": "Python Dev", "source": "bundesagentur"}
    assert job_association_mismatches(view, reference=SLASHED, **common) == []
    assert "REFERENCE" in job_association_mismatches(view, reference="13319-896770", **common)


def test_send_readiness_binds_an_identity_by_the_stored_reference(db):
    record, _ = upsert_job(db, _job(UNDERSCORE, url="https://www.get-in-it.de/p1"), SCORE)
    identity = VacancyIdentity(source="bundesagentur", source_reference=UNDERSCORE)
    assert _identity_matches_job(identity, record)
    assert not vacancy_identity_matches(
        identity, source="bundesagentur", reference_tokens=frozenset(), fingerprint="0" * 64
    )


# --- Codex round 1 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text_", "expected"),
    [
        ("https://other.example/jobs/10001-123-S.html", set()),  # prefix of another id
        ("https://other.example/jobs/10001-123-S", set()),  # another site's path
        ("Kennung 10001-123-S_x", set()),
        ("Ref 10001-123-S/456-S.", {"10001-123-S/456-S"}),  # whole token, not its prefix
        ("ref 13410-k13572.15943-s.", {"13410-k13572.15943-s"}),  # any case
        ("x.arbeitsagentur.de.evil.example/jobsuche/jobdetail/10001-1-S", set()),
        (f"siehe www.arbeitsagentur.de/jobsuche/jobdetail/{SLASHED}, danke", {SLASHED}),
        (f"({BA}{UNDERSCORE})", {UNDERSCORE}),
    ],
)
def test_text_references_are_whole_tokens_in_any_case(text_, expected):
    assert set(extract_ba_references(text_)) == expected


def test_an_other_sites_url_prefix_does_not_match_a_stored_reference():
    result = _match("https://other.example/jobs/10001-123-S.html", [_candidate(1, "10001-123-S")])
    assert result.matched_job_id is None


def test_a_lowercased_reference_is_retrieved_and_matched(db):
    old, _ = upsert_job(db, _job(DOTTED, url="https://www.get-in-it.de/p8", title="old"), SCORE)
    for n in range(3):
        upsert_job(db, _job(None, url=f"https://example.com/k/{n}", title=f"new {n}"), SCORE)
    mail = f"Ihre Bewerbung {DOTTED.lower()}"
    found = get_job_candidates(db, limit=2, source_references=extract_ba_references(mail))
    assert old.id in {c.job_id for c in found}
    assert _match(mail, found).matched_job_id == old.id


def test_set_once_is_atomic_across_sessions(db_path, caplog):
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    first = sessionmaker(engine, expire_on_commit=False)()
    second = sessionmaker(engine, expire_on_commit=False)()
    external = "https://www.get-in-it.de/jobsuche/p77"
    upsert_job(first, _job(None, url=external), SCORE)
    stale = first.scalar(select(JobRecord))  # first session still sees NULL
    assert stale.source_reference is None
    upsert_job(second, _job(UNDERSCORE, url=external), SCORE)
    with caplog.at_level(logging.WARNING):
        record, _ = upsert_job(first, _job(NUMERIC, url=external), SCORE)
    assert record.source_reference == UNDERSCORE
    assert "job_source_reference_conflict" in caplog.text
    first.close()
    second.close()
    engine.dispose()


@pytest.mark.parametrize(
    ("text_", "expected"),
    [
        ("https://evil-arbeitsagentur.de/jobsuche/jobdetail/10001-123-S", set()),
        ("evil-arbeitsagentur.de/jobsuche/jobdetail/10001-123-S", set()),
        ("https://arbeitsagentur.de.evil.example/jobsuche/jobdetail/10001-1-S", set()),
        (f"{BA}13410-k13572.15943-s", {"13410-k13572.15943-s"}),  # lower-case ending in a URL
        ("jobs.arbeitsagentur.de/jobsuche/jobdetail/10001-1-S", {"10001-1-S"}),
    ],
)
def test_codex_round_2_urls_are_taken_whole_and_checked_by_host(text_, expected):
    assert set(extract_ba_references(text_)) == expected


def test_a_foreign_host_url_never_matches_a_stored_reference():
    url = "https://evil-arbeitsagentur.de/jobsuche/jobdetail/10001-123-S"
    assert _match(url, [_candidate(1, "10001-123-S")]).matched_job_id is None
    assert ba_reference_from_url(f"{BA}13410-k13572.15943-s") is None  # stored values stay strict


@pytest.mark.parametrize(
    ("text_", "expected"),
    [
        ("https://eviläarbeitsagentur.de/jobsuche/jobdetail/10001-123-S", set()),
        ("https://other.example:8443/?ref=10001-123-S", set()),
        ("other.example:8443/?ref=10001-123-S", set()),
        ("www.other.de?ref=10001-123-S", set()),
        ("https://www.arbeitsagentur.de:443/jobsuche/jobdetail/10001-1-S", {"10001-1-S"}),
        ("Referenznummer:12265-444700_JB5270016-S", {UNDERSCORE}),
        (f"Ref {DOTTED}/", set()),  # "/" may continue an identifier: not taken
        ("mailto:jobs@example.de 10001-2-S", {"10001-2-S"}),
    ],
)
def test_codex_round_3_url_chunks_are_parsed_whole(text_, expected):
    assert set(extract_ba_references(text_)) == expected


@pytest.mark.parametrize(
    ("text_", "expected"),
    [
        ("Link:https://other.example/?ref=10001-123-S", set()),
        ("Link:https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-123-S", {"10001-123-S"}),
        ("https://other.example/?ref=(10001-123-S)", set()),
        ("10001-123-Sä", set()),
        ("10001-123-S.html", set()),
        ("Referenznummer: 12265-444700_JB5270016-S (Python)", {UNDERSCORE}),
        ("„10001-9-S“", {"10001-9-S"}),
    ],
)
def test_codex_round_4_url_spans_are_consumed_and_references_stand_alone(text_, expected):
    assert set(extract_ba_references(text_)) == expected


@pytest.mark.parametrize(
    ("text_", "expected"),
    [
        ("other.example/?next=https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-123-S", set()),
        ("https://other.example/?ref='10001-123-S'", set()),
        ("„https://www.arbeitsagentur.de/jobsuche/jobdetail/10001-123-S“", {"10001-123-S"}),
        ("Ref.: 12265-444700_JB5270016-S (Python)", {UNDERSCORE}),
        ("(Ref 10001-1-S).", {"10001-1-S"}),
        (SLASHED, {SLASHED}),
    ],
)
def test_codex_round_5_whitespace_chunks_are_whole(text_, expected):
    assert set(extract_ba_references(text_)) == expected


@pytest.mark.parametrize("prefix", [">", ">>", "> ", ">> > "])
def test_codex_round_6_quoted_reply_lines_keep_their_reference(prefix):
    assert extract_ba_references(f"{prefix}{BA}{UNDERSCORE}") == {UNDERSCORE}
    assert extract_ba_references(f"{prefix}Referenznummer: {DOTTED}") == {DOTTED}
