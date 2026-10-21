"""Duplicate-application check before applying (read-only, deterministic).

Synthetic data modelled on the real cases: the ALD Backend Developer
vacancy exists as the employer's BA post, as agency mirrors and on XING;
ALD also has a second, genuinely different role.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.models import ApplicationEventRecord, ApplicationRecord, JobRecord
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_duplicates import (
    VacancyView,
    assess_vacancy,
    bundesagentur_reference,
    normalize_posting_url,
    verdict,
)
from app.services.application_tracker import ApplicationTracker

AT = datetime(2026, 10, 17, 14, 2, 50, tzinfo=UTC)
BA = "https://www.arbeitsagentur.de/jobsuche/jobdetail/"


@pytest.fixture()
def db(tmp_path):
    from app.db.base import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'dups.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    yield session
    session.close()
    engine.dispose()


def _apply(db, *, company, role, ref, job_id=None, source="bundesagentur", url=None, fp=None):
    return (
        ApplicationTracker()
        .create_application(
            db,
            company_name=company,
            role_title=role,
            source=source,
            source_reference=ref,
            application_channel=C.EMPLOYER_PORTAL,
            initial_status=S.SUBMITTED,
            origin=O.OPERATOR,
            actor="test",
            occurred_at=AT,
            job_id=job_id,
            destination_url=url,
            vacancy_fingerprint=fp,
            correlation_key=f"submission:{source}:{ref}",
        )
        .application
    )


@pytest.fixture()
def ald(db):
    backend = _apply(
        db,
        company="ALD Vacuum Technologies GmbH",
        role="Backend Developer (m/w/div.)",
        ref="20385-2347902-S",
        job_id=174,
        url="https://karriere.ald-vt.com/Backend-Developer-mwdiv-de-f962.html",
    )
    prototyping = _apply(
        db,
        company="ALD Vacuum Technologies GmbH",
        role="Software Engineer (m/w/div.) im Bereich Prototyping & Data Analytics",
        ref="20385-2352935-S",
        job_id=169,
    )
    return backend, prototyping


def _apps(db):
    return ApplicationTracker().list_applications(db, limit=500)


def _vacancy(job_id, company, title, url, source="bundesagentur", fp=None):
    return VacancyView(
        job_id=job_id, source=source, company=company, title=title, url=url, fingerprint=fp
    )


def test_exact_duplicate_same_job_row_is_already_applied(db, ald):
    backend, _ = ald
    findings = assess_vacancy(
        _vacancy(
            174,
            "ALD Vacuum Technologies  GmbH",
            "Backend Developer (m/w/div.)",
            BA + "20385-2347902-S",
        ),
        _apps(db),
    )
    assert verdict(findings) == "ALREADY_APPLIED"
    strong = findings[0]
    assert strong.application_id == backend.id and strong.strength == "STRONG"
    assert {"SAME_JOB", "SAME_REFERENCE", "SAME_EMPLOYER_AND_ROLE"} <= set(strong.signals)


def test_same_role_from_another_source_is_already_applied(db, ald):
    """Same employer + same role collected again from XING (new job row)."""
    findings = assess_vacancy(
        _vacancy(
            999,
            "ALD Vacuum Technologies GmbH",
            "Backend Developer (m/w/d)",
            "https://www.xing.com/m/opaque",
            source="xing",
        ),
        _apps(db),
    )
    assert verdict(findings) == "ALREADY_APPLIED"
    assert findings[0].signals == ("SAME_EMPLOYER_AND_ROLE",)


def test_two_ald_roles_are_never_one_application(db, ald):
    backend, prototyping = ald
    findings = assess_vacancy(
        _vacancy(
            169, "ALD Vacuum Technologies GmbH", prototyping.role_title, BA + "20385-2352935-S"
        ),
        _apps(db),
    )
    by_app = {f.application_id: f for f in findings}
    assert by_app[prototyping.id].strength == "STRONG"
    assert by_app[backend.id].strength == "INFO"
    assert by_app[backend.id].signals == ("SAME_EMPLOYER_OTHER_ROLE",)


def test_third_ald_role_is_not_a_duplicate(db, ald):
    findings = assess_vacancy(
        _vacancy(
            177,
            "ALD Vacuum Technologies GmbH",
            "Prozessingenieur (m/w/div.)",
            BA + "20385-2343000-S",
        ),
        _apps(db),
    )
    assert verdict(findings) == "NO_DUPLICATE_FOUND"
    assert {f.strength for f in findings} == {"INFO"}


def test_agency_mirror_with_identical_title_is_a_possible_duplicate(db, ald):
    backend, _ = ald
    for company in ("N.O.C Engineering GmbH", "Jobriver IT Jobbörse"):
        findings = assess_vacancy(
            _vacancy(31, company, "Backend Developer (m/w/div.)", BA + "13552-95JQ-S"),
            _apps(db),
        )
        assert verdict(findings) == "POSSIBLE_DUPLICATE"
        assert [(f.application_id, f.signals) for f in findings] == [
            (backend.id, ("SAME_TITLE_OTHER_EMPLOYER",))
        ]


def test_similar_but_different_role_elsewhere_is_not_a_duplicate(db, ald):
    for title in ("Backend Developer (all genders) 25185", "Senior Backend Developer (m/w/div.)"):
        findings = assess_vacancy(_vacancy(144, "Weber GmbH", title, BA + "x-S"), _apps(db))
        assert findings == [] and verdict(findings) == "NO_DUPLICATE_FOUND"


def test_url_and_fingerprint_are_strong_signals(db):
    app = _apply(
        db,
        company="Acadeo GmbH",
        role="(Trainee) Python Entwickler",
        ref="12152956",
        source="stepstone",
        url="https://www.stepstone.de/stellenangebote--x--12152956-inline.html?utm_source=a",
        fp="f" * 64,
    )
    by_url = assess_vacancy(
        _vacancy(
            5,
            "Other",
            "Other",
            "HTTPS://WWW.STEPSTONE.DE/stellenangebote--x--12152956-inline.html/",
        ),
        _apps(db),
    )
    assert by_url[0].application_id == app.id and by_url[0].signals == ("SAME_URL",)
    by_fp = assess_vacancy(_vacancy(6, "Other", "Other", None, fp="f" * 64), _apps(db))
    assert by_fp[0].signals == ("SAME_FINGERPRINT",)


def test_terminal_applications_still_count(db):
    app = _apply(db, company="Acme GmbH", role="Python Developer", ref="1-S")
    tracker = ApplicationTracker()
    tracker.override_status(db, app.id, S.REJECTED, actor="t", reason="Absage geprüft")
    findings = assess_vacancy(_vacancy(1, "ACME", "Python Developer (m/w/d)", None), _apps(db))
    assert verdict(findings) == "ALREADY_APPLIED" and findings[0].status == "REJECTED"


def test_url_helpers_never_fetch_and_parse_conservatively():
    assert normalize_posting_url(None) is None and normalize_posting_url("not a url") is None
    assert normalize_posting_url("https://A.example/x/?q=1#f") == "https://a.example/x?q=1"
    assert bundesagentur_reference(BA + "20385-2347902-S?x=1") == "20385-2347902-s"
    assert bundesagentur_reference("https://www.xing.com/m/abc") is None


def test_cli_check_job_is_read_only_and_signals_by_exit_code(tmp_path, capsys):
    import hashlib

    import scripts.stage15_application_tracker as cli
    from tests.test_stage15_response_tracking import _migrated_db

    path = _migrated_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    _apply(
        session,
        company="ALD Vacuum Technologies GmbH",
        role="Backend Developer (m/w/div.)",
        ref="20385-2347902-S",
        job_id=1,
    )
    now = datetime(2026, 10, 20, tzinfo=UTC)
    for job_id, company, ref in (
        (1, "ALD Vacuum Technologies  GmbH", "20385-2347902-S"),
        (2, "N.O.C Engineering GmbH", "13552-95JQ-S"),
        (3, "Weber GmbH", "10001-1-S"),
    ):
        session.add(
            JobRecord(
                id=job_id,
                fingerprint=f"{job_id:064d}",
                source="bundesagentur",
                title="Backend Developer (m/w/div.)" if job_id < 3 else "Backend Developer",
                company=company,
                url=BA + ref,
                skills_json="[]",
                score=0,
                recommendation="SKIP",
                status="NEW",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    session.commit()
    counts = (
        session.scalar(select(func.count()).select_from(ApplicationRecord)),
        session.scalar(select(func.count()).select_from(ApplicationEventRecord)),
    )
    session.close()
    engine.dispose()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert cli.run(["--database", str(path), "check-job", "1"]) == 3
    assert "VERDICT: ALREADY_APPLIED" in capsys.readouterr().out
    assert cli.run(["--database", str(path), "check-job", "2"]) == 0
    assert "VERDICT: POSSIBLE_DUPLICATE" in capsys.readouterr().out
    assert cli.run(["--database", str(path), "--json", "check-job", "3"]) == 0
    assert '"verdict": "NO_DUPLICATE_FOUND"' in capsys.readouterr().out
    assert cli.run(["--database", str(path), "check-job", "404"]) == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert counts == (1, 1)


# --- record-submission (operator records a manual submission) ----------------------


def _record_db(tmp_path):
    from tests.test_stage15_response_tracking import _migrated_db

    path = _migrated_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    _apply(
        session,
        company="ALD Vacuum Technologies GmbH",
        role="Backend Developer (m/w/div.)",
        ref="20385-2347902-S",
        job_id=174,
    )
    session.close()
    engine.dispose()
    return path


def _record(path, *extra):
    import scripts.stage15_application_tracker as cli

    return cli.run(
        [
            "--database",
            str(path),
            "record-submission",
            "--actor",
            "operator",
            "--evidence",
            "Sent once via Gmail web UI; attachments CV + Anschreiben",
            *extra,
        ]
    )


HD24_ARGS = (
    "--company",
    "Heizungsdiscount 24 GmbH",
    "--role",
    "Softwareentwickler – Business Applications & Integrationen",
    "--source",
    "bundesagentur",
    "--reference",
    "18088-k60163.1143-S",
    "--channel",
    "EMAIL",
    "--submitted-at",
    "2026-10-20T18:58:59Z",
    "--employer-domain",
    "heizungsdiscount24.de",
)


def _counts(path):
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        result = (
            connection.scalar(select(func.count()).select_from(ApplicationRecord)),
            connection.scalar(select(func.count()).select_from(ApplicationEventRecord)),
        )
    engine.dispose()
    return result


def test_record_submission_is_a_dry_run_by_default(tmp_path, capsys):
    path = _record_db(tmp_path)
    assert _record(path, *HD24_ARGS) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert _counts(path) == (1, 1)


def test_record_submission_applies_once_and_reruns_are_idempotent(tmp_path, capsys):
    from scripts.stage15_application_tracker import submission_correlation_key

    path = _record_db(tmp_path)
    assert _record(path, *HD24_ARGS, "--apply") == 0
    assert "APPLIED: application 2 [SUBMITTED]" in capsys.readouterr().out
    assert _record(path, *HD24_ARGS, "--apply") == 0
    assert "ALREADY_RECORDED: application 2" in capsys.readouterr().out
    assert _counts(path) == (2, 2)
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    app = ApplicationTracker().get_application(session, 2)
    history = ApplicationTracker().get_application_history(session, 2)
    session.close()
    engine.dispose()
    assert app.submitted_at == datetime(2026, 10, 20, 18, 58, 59, tzinfo=UTC)
    assert app.application_channel == C.EMAIL and app.employer_domains == ("heizungsdiscount24.de",)
    assert history[0].origin == O.OPERATOR
    assert history[0].correlation_key == submission_correlation_key(
        "bundesagentur", "18088-k60163.1143-S"
    )


def test_record_submission_refuses_a_strong_duplicate_from_another_source(tmp_path, capsys):
    path = _record_db(tmp_path)
    args = (
        "--company",
        "ALD Vacuum Technologies GmbH",
        "--role",
        "Backend Developer (m/w/d)",
        "--source",
        "xing",
        "--reference",
        "xing-318",
        "--channel",
        "JOB_BOARD",
        "--submitted-at",
        "2026-10-20T12:00:00Z",
    )
    assert _record(path, *args, "--apply") == 3
    assert "REFUSED" in capsys.readouterr().err
    assert _counts(path) == (1, 1)
    assert _record(path, *args, "--confirm-distinct-from", "1", "--apply") == 0
    assert _counts(path) == (2, 2)


def test_record_submission_requires_confirmation_for_an_agency_mirror(tmp_path, capsys):
    path = _record_db(tmp_path)
    args = (
        "--company",
        "N.O.C Engineering GmbH",
        "--role",
        "Backend Developer (m/w/div.)",
        "--source",
        "bundesagentur",
        "--reference",
        "13552-95JQ-S",
        "--channel",
        "EMPLOYER_PORTAL",
        "--submitted-at",
        "2026-10-20T12:00:00Z",
    )
    assert _record(path, *args, "--apply") == 3
    assert "PROBABLE: application 1" in capsys.readouterr().out
    assert _counts(path) == (1, 1)


def test_record_submission_allows_a_different_role_at_the_same_employer(tmp_path, capsys):
    path = _record_db(tmp_path)
    args = (
        "--company",
        "ALD Vacuum Technologies GmbH",
        "--role",
        "Software Engineer (m/w/div.) im Bereich Prototyping & Data Analytics",
        "--source",
        "bundesagentur",
        "--reference",
        "20385-2352935-S",
        "--channel",
        "EMPLOYER_PORTAL",
        "--submitted-at",
        "2026-10-20T13:04:24Z",
        "--apply",
    )
    assert _record(path, *args) == 0
    assert "INFO: application 1" in capsys.readouterr().out
    assert _counts(path) == (2, 2)


@pytest.mark.parametrize("when", ["2026-10-20T12:00:00", "not-a-date", "2999-01-01T00:00:00Z"])
def test_record_submission_rejects_naive_invalid_or_future_timestamps(tmp_path, when):
    path = _record_db(tmp_path)
    args = list(HD24_ARGS)
    args[args.index("--submitted-at") + 1] = when
    assert _record(path, *args, "--apply") == 2
    assert _counts(path) == (1, 1)


# --- Codex P1-3: injective submission identity -------------------------------------


def _args_for(source, reference, *, company="Firma GmbH", role="Python Developer"):
    return (
        "--company",
        company,
        "--role",
        role,
        "--source",
        source,
        "--reference",
        reference,
        "--channel",
        "JOB_BOARD",
        "--submitted-at",
        "2026-10-20T12:00:00Z",
    )


def test_submission_key_is_injective_and_stable():
    from scripts.stage15_application_tracker import submission_correlation_key as key

    assert key("a:b", "c") != key("a", "b:c")
    assert key("a", "b") == key("a", "b")
    assert key("a", "b").startswith("submission-v2:") and len(key("a", "b")) == 14 + 64
    assert not key("bundesagentur", "x").startswith(("email-submission:", "portal-submission:"))


def test_separator_in_source_or_reference_never_merges_two_submissions(tmp_path, capsys):
    path = _record_db(tmp_path)
    assert _record(path, *_args_for("a:b", "c", company="Alpha GmbH"), "--apply") == 0
    assert (
        _record(path, *_args_for("a", "b:c", company="Beta GmbH", role="Data Engineer"), "--apply")
        == 0
    )
    out = capsys.readouterr().out
    assert "APPLIED: application 2" in out and "APPLIED: application 3" in out
    assert _counts(path) == (3, 3)


@pytest.mark.parametrize("reference", ["", "   ", "\t", " 18088-k60163.1143-S"])
def test_empty_or_padded_reference_is_refused_before_any_write(tmp_path, capsys, reference):
    path = _record_db(tmp_path)
    args = list(HD24_ARGS)
    args[args.index("--reference") + 1] = reference
    assert _record(path, *args, "--apply") == 2
    assert "--reference must not" in capsys.readouterr().err
    assert _counts(path) == (1, 1)


def test_legacy_keyed_record_is_still_found_and_never_duplicated(tmp_path, capsys):
    """The first real applications carry `email-submission:<src>:<ref>` keys;
    the structural (source, reference) lookup still finds them."""
    path = _record_db(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    ApplicationTracker().create_application(
        session,
        company_name="Heizungsdiscount 24 GmbH",
        role_title="Softwareentwickler – Business Applications & Integrationen",
        source="bundesagentur",
        source_reference="18088-k60163.1143-S",
        application_channel=C.EMAIL,
        initial_status=S.SUBMITTED,
        origin=O.OPERATOR,
        actor="legacy",
        occurred_at=AT,
        correlation_key="email-submission:bundesagentur:18088-k60163.1143-S",
    )
    session.close()
    engine.dispose()
    assert _record(path, *HD24_ARGS, "--apply") == 0
    assert "ALREADY_RECORDED: application 2" in capsys.readouterr().out
    assert _counts(path) == (2, 2)


# --- Codex P1-4: --job-id must be the job this submission is for -------------------


def _add_job(path, job_id, *, company, title, url, source="bundesagentur"):
    now = datetime.now(UTC)
    engine = create_engine(f"sqlite:///{path}")
    session = sessionmaker(engine)()
    session.add(
        JobRecord(
            id=job_id,
            fingerprint=f"{job_id:064d}",
            source=source,
            title=title,
            company=company,
            url=url,
            skills_json="[]",
            score=0,
            recommendation="SKIP",
            status="NEW",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    session.commit()
    session.close()
    engine.dispose()


def _hd24_job(path):
    _add_job(
        path,
        365,
        company="Heizungsdiscount 24  GmbH",
        title="Softwareentwickler – Business Applications & Integrationen (m/w/d)",
        url=BA + "18088-k60163.1143-S",
    )


def test_matching_job_company_role_and_reference_is_accepted(tmp_path, capsys):
    path = _record_db(tmp_path)
    _hd24_job(path)
    assert _record(path, *HD24_ARGS, "--job-id", "365", "--apply") == 0
    assert "APPLIED: application 2" in capsys.readouterr().out
    assert _counts(path) == (2, 2)


@pytest.mark.parametrize(
    ("flag", "value", "differs"),
    [
        ("--company", "Company B GmbH", "COMPANY"),
        ("--role", "Senior Data Engineer", "ROLE"),
        ("--reference", "18088-other-S", "REFERENCE"),
        ("--source", "stepstone", "SOURCE"),
    ],
)
@pytest.mark.parametrize("apply", [False, True])
def test_job_of_another_vacancy_is_refused_before_any_write(
    tmp_path, capsys, flag, value, differs, apply
):
    path = _record_db(tmp_path)
    _hd24_job(path)
    args = list(HD24_ARGS)
    args[args.index(flag) + 1] = value
    extra = ("--job-id", "365", *(("--apply",) if apply else ()))
    assert _record(path, *args, *extra) == 4
    err = capsys.readouterr().err
    assert "REFUSED: job 365" in err and differs in err
    assert _counts(path) == (1, 1)


def test_ba_destination_url_of_another_vacancy_is_refused(tmp_path, capsys):
    path = _record_db(tmp_path)
    _hd24_job(path)
    extra = ("--job-id", "365", "--destination-url", BA + "99999-other-S", "--apply")
    assert _record(path, *HD24_ARGS, *extra) == 4
    assert "DESTINATION_URL" in capsys.readouterr().err
    assert _counts(path) == (1, 1)


def test_manual_stepstone_submission_without_job_id_is_still_supported(tmp_path, capsys):
    path = _record_db(tmp_path)
    args = _args_for(
        "stepstone", "12152956", company="Acadeo GmbH", role="(Trainee) Python Entwickler"
    )
    assert _record(path, *args, "--apply") == 0
    assert "APPLIED: application 2" in capsys.readouterr().out
    assert _counts(path) == (2, 2)


def test_job_association_normalizes_only_form_not_identity():
    from app.services.application_duplicates import job_association_mismatches

    job = _vacancy(174, "ALD Vacuum Technologies  GmbH", "Backend Developer (m/w/div.)", None)
    job = VacancyView(174, "bundesagentur", job.company, job.title, BA + "20385-2347902-S")
    ok = dict(source="bundesagentur", reference="20385-2347902-S")
    assert not job_association_mismatches(
        job, company="ald vacuum technologies gmbh", role="Backend Developer (m/w/d)", **ok
    )
    assert job_association_mismatches(job, company="GmbH", role=job.title, **ok) == ["COMPANY"]
    assert job_association_mismatches(job, company=job.company, role="Developer", **ok) == ["ROLE"]


# --- Codex P2-1: only tracking parameters are dropped from posting URLs ------------


def test_vacancy_identifying_query_parameters_are_kept():
    assert normalize_posting_url("https://jobs.example/apply?id=123") != normalize_posting_url(
        "https://jobs.example/apply?id=456"
    )
    assert normalize_posting_url("https://jobs.example/apply?id=123") == (
        "https://jobs.example/apply?id=123"
    )


def test_tracking_only_differences_normalize_to_the_same_url():
    plain = normalize_posting_url("https://jobs.example/apply?id=123&lang=de")
    assert plain == normalize_posting_url(
        "HTTPS://Jobs.Example/apply/?lang=de&utm_source=xing&UTM_Campaign=a&id=123"
        "&trackingId=9&trk=x&refId=7#top"
    )
    assert plain == normalize_posting_url("https://jobs.example/apply?lang=de&id=123&gclid=1")


def test_different_vacancy_ids_are_not_same_url_duplicates(db):
    _apply(
        db,
        company="Alpha GmbH",
        role="Python Developer",
        ref="a-1",
        url="https://jobs.example/apply?id=123",
    )
    other = _vacancy(9, "Other", "Other", "https://jobs.example/apply?id=456")
    assert assess_vacancy(other, _apps(db)) == []
    same = _vacancy(9, "Other", "Other", "https://jobs.example/apply?utm_medium=mail&id=123")
    assert [f.signals for f in assess_vacancy(same, _apps(db))] == [("SAME_URL",)]


# --- Codex P2-2: a future --submitted-at is refused (no tolerance) ----------------


def test_submitted_at_accepts_now_and_past_and_refuses_any_future_instant():
    from datetime import timedelta

    from scripts.stage15_application_tracker import TargetRefusedError, _submitted_at

    now = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    assert _submitted_at("2026-10-21T09:00:00Z", now=now) == now
    assert _submitted_at("2026-10-21T11:00:00+02:00", now=now) == now
    assert _submitted_at("2026-10-20T18:58:59Z", now=now) < now
    for raw in (
        (now + timedelta(minutes=2)).isoformat(),
        (now + timedelta(seconds=1)).isoformat(),
        "2026-10-21T09:00:00",
        "yesterday",
    ):
        with pytest.raises(TargetRefusedError):
            _submitted_at(raw, now=now)


def test_cli_refuses_a_submission_two_minutes_in_the_future(tmp_path, capsys):
    from datetime import timedelta

    path = _record_db(tmp_path)
    args = list(HD24_ARGS)
    soon = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    args[args.index("--submitted-at") + 1] = soon
    assert _record(path, *args, "--apply") == 2
    assert "future" in capsys.readouterr().err
    assert _counts(path) == (1, 1)
