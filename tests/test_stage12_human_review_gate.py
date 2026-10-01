"""Tests for the Stage 12 Human Review Gate in scripts/offline_rescore_stage12.py.

--apply must be refused -- before any database write and before the
population lock / apply transaction -- unless a reviewed copy of the exact
preview's human-review CSV is supplied, structurally valid, provenance-
matched, complete, and fully APPROVE'd. Synthetic in-memory SQLite only:
`build_engine` is always monkeypatched, so no test can reach a real database.
"""

import contextlib
import csv
import hashlib
import io
import json
import math
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

import scripts.offline_rescore_stage12 as offline_rescore_stage12
from app.db.base import Base
from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
)
from app.db.models import AutomationRunRecord, JobRecord
from app.models.candidate_profile import (
    CandidateJobPreferences,
    CandidateProfilePatchRequest,
    CandidateSkill,
)
from app.models.job import Job
from app.services.collector_runner import score_and_persist
from scripts.offline_rescore_stage12 import (
    HUMAN_EDITABLE_COLUMNS,
    HUMAN_REVIEW_COLUMNS,
    IMMUTABLE_REVIEW_COLUMNS,
    REVIEW_PROVENANCE_VERSION,
    STAGE12_PILOT_DATABASE,
    ApprovalEvidenceAliasError,
    ApprovedReview,
    HumanDecision,
    HumanRelevance,
    HumanReviewAuditError,
    HumanReviewDuplicateIdentityError,
    HumanReviewError,
    HumanReviewExpectedPopulationError,
    HumanReviewFileMissingError,
    HumanReviewIncompleteError,
    HumanReviewInvalidValueError,
    HumanReviewMalformedError,
    HumanReviewMissingRowError,
    HumanReviewProvenanceMismatchError,
    HumanReviewRejectedError,
    HumanReviewUnexpectedRowError,
    HumanReviewUnresolvedError,
    PilotIdentityMismatchError,
    RescoreConcurrentModificationError,
    _build_human_review_rows,
    approval_audit_paths,
    compute_review_provenance,
    main,
    preview_all_jobs,
    verify_human_review,
)

RICH_TECH_DESCRIPTION = (
    "We build REST APIs with Python, FastAPI and SQLAlchemy against "
    "PostgreSQL. Git-based workflow, automated tests with Pytest, "
    "containerized with Docker. " * 6
)
RICH_SKILLS = ["Python", "FastAPI", "SQLAlchemy", "REST API", "Docker", "Pytest"]
WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE")


def _set_candidate_skills(db: Session, skills: list[str]) -> None:
    current = get_or_create_candidate_profile(db)
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=current.profile_version,
            skills=[CandidateSkill(name=name) for name in skills],
            job_preferences=CandidateJobPreferences(employment_types=[]),
        ),
    )


def _seed_job(db: Session, **overrides) -> None:
    data = {
        "source": "bundesagentur",
        "title": "AI Engineer",
        "company": "Example GmbH",
        "url": "https://example.com/jobs/gate-1",
        "description": RICH_TECH_DESCRIPTION,
        "posting_type": "ARBEIT",
        "must_have_skills": ["Python", "FastAPI", "SQLAlchemy", "REST API"],
        "nice_to_have_skills": ["Docker", "Pytest"],
    }
    data.update(overrides)
    # Ingested against a deliberately POOR skill set, so the canonical
    # (rich) profile genuinely changes the persisted score on apply.
    score_and_persist(db, frozenset(["Python"]), Job(**data))


def _authorize_pilot_identity(monkeypatch, *, jobs: int, automation_runs: int = 0) -> None:
    monkeypatch.setattr(offline_rescore_stage12, "STAGE12_PILOT_JOB_COUNT", jobs)
    monkeypatch.setattr(
        offline_rescore_stage12, "STAGE12_PILOT_AUTOMATION_RUNS_COUNT", automation_runs
    )
    monkeypatch.setattr(offline_rescore_stage12, "STAGE12_PILOT_CANDIDATE_PROFILE_COUNT", 1)


@pytest.fixture
def pilot(monkeypatch):
    """A 3-job synthetic stand-in for the pilot, wired into `main` through a
    monkeypatched `build_engine`; records every SQL statement executed."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, url="https://example.com/jobs/gate-1")
        _seed_job(db, title="Backend Developer", url="https://example.com/jobs/gate-2")
        _seed_job(db, title="Personalcontroller", url="https://example.com/jobs/gate-3")
    _authorize_pilot_identity(monkeypatch, jobs=3)

    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, params, context, executemany: statements.append(statement),
    )
    build_calls: list[object] = []

    def _fake_build_engine(args):
        build_calls.append(args)
        return engine

    monkeypatch.setattr(offline_rescore_stage12, "build_engine", _fake_build_engine)
    engine.statements = statements
    engine.build_calls = build_calls
    return engine


def _scores(engine) -> list[tuple]:
    with Session(engine) as db:
        return [
            (r.id, r.score, r.recommendation, r.data_confidence)
            for r in db.scalars(select(JobRecord).order_by(JobRecord.id))
        ]


def _writes(engine) -> list[str]:
    return [s for s in engine.statements if s.lstrip().upper().startswith(WRITE_PREFIXES)]


def _base_args(export_dir: Path) -> list[str]:
    return ["--postgres-db", STAGE12_PILOT_DATABASE, "--export-dir", str(export_dir)]


def _apply_args(export_dir: Path, review: Path | None) -> list[str]:
    args = _base_args(export_dir) + ["--apply", "--confirm-database", STAGE12_PILOT_DATABASE]
    if review is not None:
        args += ["--human-review", str(review)]
    return args


def _preview(tmp_path: Path) -> Path:
    export_dir = tmp_path / "preview"
    assert main(_base_args(export_dir)) == 0
    return export_dir / f"stage12_human_review_{STAGE12_PILOT_DATABASE}.csv"


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(path: Path, rows: list[dict[str, str]], fieldnames=HUMAN_REVIEW_COLUMNS) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _reviewed(tmp_path: Path, preview_csv: Path, mutate=None, **values) -> Path:
    rows = _read(preview_csv)
    for row in rows:
        row["human_relevant"] = values.get("relevant", "YES")
        row["human_decision"] = values.get("decision", "APPROVE")
        row["human_notes"] = values.get("notes", "")
    if mutate is not None:
        mutate(rows)
    return _write(tmp_path / "reviewed.csv", rows)


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    """Runs `main` capturing stdout/stderr; any exception escaping `main`
    (i.e. a traceback for the operator) fails the calling test."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def _audit_files(export_dir: Path) -> list[Path]:
    return sorted(export_dir.glob("stage12_approved_review_*"))


def _assert_blocked(engine, tmp_path, review: Path, error: type[HumanReviewError]) -> str:
    """Runs --apply and asserts the gate refused it with `error` as a
    controlled ABORT (exit 2, no traceback), that no write statement ever
    reached the database, scores are unchanged, and no approval audit
    artifact was preserved. Returns stderr."""
    before = _scores(engine)
    engine.statements.clear()
    export_dir = tmp_path / "apply"
    rc, out, err = _run_main(_apply_args(export_dir, review))
    assert rc == 2
    assert err.startswith("ABORT: human review gate refused --apply")
    assert f"({error.__name__})" in err
    assert "Traceback" not in err + out
    assert _writes(engine) == []
    assert _scores(engine) == before
    assert _audit_files(export_dir) == []
    return err


# --- PASS cases ------------------------------------------------------------


def test_valid_complete_review_reaches_apply_and_persists(pilot, tmp_path):
    preview_csv = _preview(tmp_path)
    before = _scores(pilot)
    review = _reviewed(tmp_path, preview_csv)

    assert main(_apply_args(tmp_path / "apply", review)) == 0

    after = _scores(pilot)
    assert after != before
    expected = {
        int(row["job_id"]): (int(row["score"]), row["recommendation"]) for row in _read(preview_csv)
    }
    assert {job_id: (score, rec) for job_id, score, rec, _dc in after} == expected
    # The reviewed copy is never overwritten by the apply-run export.
    assert _read(review)[0]["human_decision"] == "APPROVE"


def test_relevant_no_with_approve_keeps_the_existing_recommendation(pilot, tmp_path):
    preview_csv = _preview(tmp_path)
    review = _reviewed(tmp_path, preview_csv, relevant="NO")

    assert main(_apply_args(tmp_path / "apply", review)) == 0

    expected = {
        int(row["job_id"]): (int(row["score"]), row["recommendation"]) for row in _read(preview_csv)
    }
    assert {job_id: (score, rec) for job_id, score, rec, _dc in _scores(pilot)} == expected


def test_mixed_case_and_padded_values_normalize_and_notes_may_be_blank(pilot, tmp_path):
    preview_csv = _preview(tmp_path)
    review = _reviewed(tmp_path, preview_csv, relevant=" yEs ", decision="approve ", notes="")
    with Session(pilot) as db:
        context, results = preview_all_jobs(db)
    provenance = compute_review_provenance(context, results, sample_seed=12345)
    expected, _ = _build_human_review_rows(results, sample_seed=12345, review_provenance=provenance)

    reviewed = verify_human_review(review, expected)

    assert len(reviewed) == 3
    assert all(r.human_relevant is HumanRelevance.YES for r in reviewed)
    assert all(r.human_decision is HumanDecision.APPROVE for r in reviewed)
    assert all(r.human_notes == "" for r in reviewed)
    assert main(_apply_args(tmp_path / "apply", review)) == 0


def test_relevant_no_with_approve_is_accepted(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path), relevant="no", notes="not my field")
    assert main(_apply_args(tmp_path / "apply", review)) == 0


def test_preview_runs_without_review_and_exports_blank_human_fields(pilot, tmp_path):
    rows = _read(_preview(tmp_path))

    assert len(rows) == 3
    assert list(rows[0]) == HUMAN_REVIEW_COLUMNS
    for row in rows:
        assert row["human_relevant"] == row["human_decision"] == row["human_notes"] == ""
        assert len(row["review_provenance"]) == 64
    assert len({row["review_provenance"] for row in rows}) == 1
    assert _writes(pilot) == []


def test_preview_output_is_deterministic(pilot, tmp_path):
    assert main(_base_args(tmp_path / "a")) == 0
    assert main(_base_args(tmp_path / "b")) == 0
    for name in (
        f"stage12_human_review_{STAGE12_PILOT_DATABASE}.csv",
        f"stage12_rescore_{STAGE12_PILOT_DATABASE}_before_after.csv",
    ):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


# --- CLI-level blocks (before any engine is built) ------------------------


def test_apply_without_review_file_is_rejected_before_engine(pilot, tmp_path, capsys):
    assert main(_apply_args(tmp_path / "apply", None)) == 2
    assert "--human-review" in capsys.readouterr().err
    assert pilot.build_calls == []


def test_nonexistent_review_file_is_rejected_before_engine(pilot, tmp_path):
    rc, out, err = _run_main(_apply_args(tmp_path / "apply", tmp_path / "missing.csv"))
    assert rc == 2
    assert err.startswith("ABORT:")
    assert "Traceback" not in err + out
    assert pilot.build_calls == []


def test_human_review_without_apply_is_rejected(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    pilot.build_calls.clear()
    assert main(_base_args(tmp_path / "x") + ["--human-review", str(review)]) == 2
    assert pilot.build_calls == []


def test_review_file_equal_to_this_runs_export_path_is_rejected(pilot, tmp_path):
    preview_csv = _preview(tmp_path)
    _reviewed(tmp_path, preview_csv)
    (tmp_path / "reviewed.csv").replace(preview_csv)
    before = _scores(pilot)
    pilot.build_calls.clear()

    assert main(_apply_args(preview_csv.parent, preview_csv)) == 2

    assert pilot.build_calls == []
    assert _scores(pilot) == before


# --- Malformed file --------------------------------------------------------


def test_empty_file_blocks(pilot, tmp_path):
    _preview(tmp_path)
    review = tmp_path / "empty.csv"
    review.write_bytes(b"")
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


def test_header_only_file_blocks(pilot, tmp_path):
    _preview(tmp_path)
    review = _write(tmp_path / "header_only.csv", [])
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


def test_malformed_csv_blocks(pilot, tmp_path):
    preview_csv = _preview(tmp_path)
    review = tmp_path / "malformed.csv"
    review.write_text(
        preview_csv.read_text(encoding="utf-8") + '"unterminated,quote\n', encoding="utf-8"
    )
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


def test_row_with_wrong_field_count_blocks(pilot, tmp_path):
    preview_csv = _preview(tmp_path)
    review = tmp_path / "short_row.csv"
    review.write_text(preview_csv.read_text(encoding="utf-8") + "1,abc\n", encoding="utf-8")
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


def test_non_utf8_file_blocks(pilot, tmp_path):
    _preview(tmp_path)
    review = tmp_path / "binary.csv"
    review.write_bytes(b"\xff\xfe\x00garbage")
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


@pytest.mark.parametrize("column", ["human_decision", "human_relevant", "job_id", "fingerprint"])
def test_missing_required_column_blocks(pilot, tmp_path, column):
    rows = _read(_reviewed(tmp_path, _preview(tmp_path)))
    review = _write(
        tmp_path / "missing_col.csv", rows, [c for c in HUMAN_REVIEW_COLUMNS if c != column]
    )
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


def test_unexpected_column_blocks(pilot, tmp_path):
    rows = _read(_reviewed(tmp_path, _preview(tmp_path)))
    review = _write(tmp_path / "extra_col.csv", rows, [*HUMAN_REVIEW_COLUMNS, "approved_by"])
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


def test_historical_review_format_without_provenance_is_not_accepted(pilot, tmp_path):
    """A pre-gate export (e.g. the earlier 33-row QA file) has no
    review_provenance column -- even fully APPROVE'd it never authorizes."""
    rows = _read(_reviewed(tmp_path, _preview(tmp_path)))
    legacy_columns = [c for c in HUMAN_REVIEW_COLUMNS if c != "review_provenance"]
    review = _write(tmp_path / "historical.csv", rows, legacy_columns)
    _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)


# --- Human values ----------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "error"),
    [
        ({"relevant": ""}, HumanReviewIncompleteError),
        ({"relevant": "   "}, HumanReviewIncompleteError),
        ({"decision": ""}, HumanReviewIncompleteError),
        ({"relevant": "MAYBE"}, HumanReviewInvalidValueError),
        ({"relevant": "Y"}, HumanReviewInvalidValueError),
        ({"decision": "APPROVED"}, HumanReviewInvalidValueError),
        ({"decision": "APPLY"}, HumanReviewInvalidValueError),
        ({"relevant": "UNSURE"}, HumanReviewUnresolvedError),
        ({"relevant": "unsure"}, HumanReviewUnresolvedError),
        ({"decision": "REVIEW"}, HumanReviewUnresolvedError),
        ({"decision": "REJECT"}, HumanReviewRejectedError),
        ({"decision": "reject", "relevant": "NO"}, HumanReviewRejectedError),
    ],
)
def test_non_approving_or_invalid_values_block(pilot, tmp_path, values, error):
    review = _reviewed(tmp_path, _preview(tmp_path), **values)
    _assert_blocked(pilot, tmp_path, review, error)


def test_single_unresolved_row_among_approvals_blocks(pilot, tmp_path):
    def _one_review(rows):
        rows[1]["human_decision"] = "REVIEW"

    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_one_review)
    _assert_blocked(pilot, tmp_path, review, HumanReviewUnresolvedError)


def test_single_rejected_row_among_approvals_blocks(pilot, tmp_path):
    def _one_reject(rows):
        rows[2]["human_decision"] = "REJECT"

    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_one_reject)
    _assert_blocked(pilot, tmp_path, review, HumanReviewRejectedError)


# --- Identity / population -------------------------------------------------


def _duplicate_first_row(rows):
    rows.append(dict(rows[0]))


def _duplicate_fingerprint(rows):
    rows[1]["fingerprint"] = rows[0]["fingerprint"]


def _drop_last_row(rows):
    rows.pop()


def _extra_row(rows):
    extra = dict(rows[0])
    extra["job_id"] = "999"
    extra["fingerprint"] = "f" * 64
    rows.append(extra)


def _change_job_id(rows):
    rows[0]["job_id"] = "999"


def _change_fingerprint(rows):
    fingerprint = rows[0]["fingerprint"]
    rows[0]["fingerprint"] = ("0" if fingerprint[0] != "0" else "1") + fingerprint[1:]


def _change_recommendation(rows):
    rows[0]["recommendation"] = "SKIP" if rows[0]["recommendation"] != "SKIP" else "APPLY"


def _change_score(rows):
    rows[0]["score"] = str(int(rows[0]["score"]) + 1)


def _change_provenance(rows):
    for row in rows:
        row["review_provenance"] = "0" * 64


def _blank_provenance(rows):
    for row in rows:
        row["review_provenance"] = ""


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (_duplicate_first_row, HumanReviewDuplicateIdentityError),
        (_duplicate_fingerprint, HumanReviewDuplicateIdentityError),
        (_drop_last_row, HumanReviewMissingRowError),
        (_extra_row, HumanReviewUnexpectedRowError),
        (_change_job_id, HumanReviewUnexpectedRowError),
        (_change_fingerprint, HumanReviewProvenanceMismatchError),
        (_change_recommendation, HumanReviewProvenanceMismatchError),
        (_change_score, HumanReviewProvenanceMismatchError),
        (_change_provenance, HumanReviewProvenanceMismatchError),
        (_blank_provenance, HumanReviewProvenanceMismatchError),
    ],
)
def test_identity_tampering_blocks(pilot, tmp_path, mutate, error):
    review = _reviewed(tmp_path, _preview(tmp_path), mutate=mutate)
    _assert_blocked(pilot, tmp_path, review, error)


def test_row_order_does_not_matter(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path), mutate=lambda rows: rows.reverse())
    assert main(_apply_args(tmp_path / "apply", review)) == 0


def test_wrong_population_blocks(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    with Session(pilot) as db:
        _seed_job(db, title="Data Engineer", url="https://example.com/jobs/gate-4")
    _authorize_pilot_identity(monkeypatch, jobs=4)
    _assert_blocked(pilot, tmp_path, review, HumanReviewProvenanceMismatchError)


def test_stale_review_from_a_different_preview_blocks(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    with Session(pilot) as db:
        _set_candidate_skills(db, [*RICH_SKILLS, "Kubernetes"])
    _assert_blocked(pilot, tmp_path, review, HumanReviewProvenanceMismatchError)


def test_review_for_a_different_sample_seed_blocks(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    before = _scores(pilot)
    rc, _out, err = _run_main(_apply_args(tmp_path / "apply", review) + ["--sample-seed", "1"])
    assert rc == 2
    assert "(HumanReviewProvenanceMismatchError)" in err
    assert _scores(pilot) == before


def test_review_cannot_be_replayed_after_a_successful_apply(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    assert main(_apply_args(tmp_path / "first_apply", review)) == 0
    _assert_blocked(pilot, tmp_path, review, HumanReviewProvenanceMismatchError)


def test_provenance_is_reproducible_and_sensitive_to_inputs(pilot):
    with Session(pilot) as db:
        context, results = preview_all_jobs(db)
    with Session(pilot) as db:
        context_again, results_again = preview_all_jobs(db)

    digest = compute_review_provenance(context, results, sample_seed=7)
    assert digest == compute_review_provenance(context_again, results_again, sample_seed=7)
    assert digest == compute_review_provenance(context, list(reversed(results)), sample_seed=7)
    assert digest != compute_review_provenance(context, results, sample_seed=8)
    assert digest != compute_review_provenance(context, results[:-1], sample_seed=7)


# --- Ordering / existing safeguards preserved ------------------------------


def _record_call_order(monkeypatch) -> list[str]:
    calls: list[str] = []
    for name in (
        "enforce_human_review_gate",
        "preserve_approval_evidence",
        "lock_pilot_population",
        "apply_rescore",
    ):
        original = getattr(offline_rescore_stage12, name)

        def _spy(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(offline_rescore_stage12, name, _spy)
    return calls


def test_population_lock_and_apply_run_only_after_gate_passes(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    calls = _record_call_order(monkeypatch)

    assert main(_apply_args(tmp_path / "apply", review)) == 0

    assert calls == [
        "enforce_human_review_gate",
        "preserve_approval_evidence",
        "apply_rescore",
        "lock_pilot_population",
    ]


def test_failed_gate_never_reaches_population_lock_or_apply(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path), decision="REVIEW")
    calls = _record_call_order(monkeypatch)

    _assert_blocked(pilot, tmp_path, review, HumanReviewUnresolvedError)

    assert calls == ["enforce_human_review_gate"]


def test_candidate_profile_check_still_blocks_after_gate_passes(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    before = _scores(pilot)
    real_gate = offline_rescore_stage12.enforce_human_review_gate

    def _gate_then_profile_change(*args, **kwargs):
        rows = real_gate(*args, **kwargs)
        with Session(pilot) as db:
            _set_candidate_skills(db, [*RICH_SKILLS, "Kubernetes"])
        return rows

    monkeypatch.setattr(
        offline_rescore_stage12, "enforce_human_review_gate", _gate_then_profile_change
    )
    with pytest.raises(RescoreConcurrentModificationError):
        main(_apply_args(tmp_path / "apply", review))
    assert _scores(pilot) == before


def test_pilot_identity_check_still_blocks_after_gate_passes(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    before = _scores(pilot)
    real_gate = offline_rescore_stage12.enforce_human_review_gate

    def _gate_then_new_automation_run(*args, **kwargs):
        rows = real_gate(*args, **kwargs)
        with Session(pilot) as db:
            db.add(
                AutomationRunRecord(
                    account_key="pilot@example.com",
                    status="COMPLETED",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    results_json="{}",
                )
            )
            db.commit()
        return rows

    monkeypatch.setattr(
        offline_rescore_stage12, "enforce_human_review_gate", _gate_then_new_automation_run
    )
    with pytest.raises(PilotIdentityMismatchError):
        main(_apply_args(tmp_path / "apply", review))
    assert _scores(pilot) == before


# --- HRG-H1: every immutable (non-human) column is bound to the preview ----


def _db_name_outputs(export_dir: Path) -> dict[str, Path]:
    return {
        "before_after": export_dir / f"stage12_rescore_{STAGE12_PILOT_DATABASE}_before_after.csv",
        "human_review": export_dir / f"stage12_human_review_{STAGE12_PILOT_DATABASE}.csv",
    }


def test_immutable_columns_are_derived_from_the_schema():
    assert HUMAN_EDITABLE_COLUMNS == {"human_relevant", "human_decision", "human_notes"}
    assert set(IMMUTABLE_REVIEW_COLUMNS) == set(HUMAN_REVIEW_COLUMNS) - HUMAN_EDITABLE_COLUMNS
    assert len(IMMUTABLE_REVIEW_COLUMNS) == len(HUMAN_REVIEW_COLUMNS) - 3
    # Far wider than the original fingerprint/score/recommendation subset.
    for column in ("title", "company", "gate_reason", "description_or_url", "data_confidence"):
        assert column in IMMUTABLE_REVIEW_COLUMNS


def _tamper(column: str, variant: str):
    def _mutate(rows):
        value = rows[0][column]
        if variant == "trailing_space":
            rows[0][column] = value + " "
        elif column == "job_id":
            rows[0][column] = str(int(value) + 1000)
        else:
            rows[0][column] = value + "~ALTERED EVIDENCE"

    return _mutate


def _tamper_error(column: str, variant: str) -> type[HumanReviewError]:
    if column == "job_id":
        if variant == "trailing_space":
            return HumanReviewMalformedError
        return HumanReviewUnexpectedRowError
    return HumanReviewProvenanceMismatchError


@pytest.mark.parametrize("variant", ["altered", "trailing_space"])
@pytest.mark.parametrize("column", IMMUTABLE_REVIEW_COLUMNS)
def test_any_single_immutable_column_tamper_blocks_with_copied_provenance(
    pilot, monkeypatch, tmp_path, column, variant
):
    """Every non-human column -- discovered from the schema, so future
    evidence columns are covered automatically -- is mutated alone while
    every other cell, including the valid review_provenance, is copied
    verbatim. The gate must refuse before preservation/lock/apply/DML."""
    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_tamper(column, variant))
    if column != "review_provenance":
        assert len({row["review_provenance"] for row in _read(review)}) == 1
    calls = _record_call_order(monkeypatch)

    _assert_blocked(pilot, tmp_path, review, _tamper_error(column, variant))

    assert calls == ["enforce_human_review_gate"]


@pytest.mark.parametrize(
    ("column", "edit"),
    [
        ("title", lambda v: "Principal AI Architect"),
        ("title", lambda v: v.upper()),
        ("company", lambda v: "Completely Different AG"),
        ("gate_reason", lambda v: ""),
        ("gate_reason", lambda v: "all gates passed"),
        ("matched_must_have", lambda v: v + ";Kubernetes"),
        ("missing_must_have", lambda v: "Kubernetes"),
        ("description_or_url", lambda v: "https://attacker.example/other-job"),
        ("data_confidence", lambda v: v + "0"),
        ("data_confidence", lambda v: "1.0"),
    ],
)
def test_readable_evidence_edits_block(pilot, tmp_path, column, edit):
    preview_csv = _preview(tmp_path)

    def _mutate(rows):
        for row in rows:
            row[column] = edit(row[column])

    review = _reviewed(tmp_path, preview_csv, mutate=_mutate)
    assert any(
        r[column] != p[column] for r, p in zip(_read(review), _read(preview_csv), strict=True)
    )

    err = _assert_blocked(pilot, tmp_path, review, HumanReviewProvenanceMismatchError)

    assert f"immutable column {column} differs" in err


def test_all_evidence_columns_altered_on_one_row_blocks(pilot, tmp_path):
    def _mutate(rows):
        for column in IMMUTABLE_REVIEW_COLUMNS:
            if column not in ("job_id", "fingerprint", "review_provenance"):
                rows[0][column] = "ALTERED EVIDENCE"

    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_mutate)
    _assert_blocked(pilot, tmp_path, review, HumanReviewProvenanceMismatchError)


def test_changed_regenerated_evidence_with_identical_scores_blocks(pilot, monkeypatch, tmp_path):
    """Explanatory output changed (e.g. a gate-trace wording change) while
    scores/recommendations -- and so the provenance digest -- did not: the
    old review no longer describes what the exporter would show now."""
    review = _reviewed(tmp_path, _preview(tmp_path))
    real_build = offline_rescore_stage12._build_human_review_rows

    def _build_with_new_explanations(*args, **kwargs):
        rows, counts = real_build(*args, **kwargs)
        for row in rows:
            row["gate_reason"] = row["gate_reason"] + ";reworded"
        return rows, counts

    monkeypatch.setattr(
        offline_rescore_stage12, "_build_human_review_rows", _build_with_new_explanations
    )
    _assert_blocked(pilot, tmp_path, review, HumanReviewProvenanceMismatchError)


# --- HRG-L1: expected population is validated explicitly -----------------


def _expected_rows(engine) -> list[dict]:
    with Session(engine) as db:
        context, results = preview_all_jobs(db)
    provenance = compute_review_provenance(context, results, sample_seed=12345)
    rows, _ = _build_human_review_rows(results, sample_seed=12345, review_provenance=provenance)
    return rows


def _dup_expected_id(rows):
    return [*rows, dict(rows[0])]


def _conflicting_dup_expected_id(rows):
    return [*rows, dict(rows[0], title="Something else", fingerprint="e" * 64)]


def _dup_expected_fingerprint(rows):
    rows[1] = dict(rows[1], fingerprint=rows[0]["fingerprint"])
    return rows


def _empty_expected(rows):
    return []


def _blank_expected_provenance(rows):
    return [dict(row, review_provenance="") for row in rows]


def _mixed_expected_provenance(rows):
    rows[0] = dict(rows[0], review_provenance="0" * 64)
    return rows


def _bad_expected_job_id(rows):
    rows[0] = dict(rows[0], job_id="1")
    return rows


def _blank_expected_fingerprint(rows):
    rows[0] = dict(rows[0], fingerprint="")
    return rows


@pytest.mark.parametrize(
    "corrupt",
    [
        _dup_expected_id,
        _conflicting_dup_expected_id,
        _dup_expected_fingerprint,
        _empty_expected,
        _blank_expected_provenance,
        _mixed_expected_provenance,
        _bad_expected_job_id,
        _blank_expected_fingerprint,
    ],
)
def test_malformed_expected_population_is_rejected(pilot, tmp_path, corrupt):
    review = _reviewed(tmp_path, _preview(tmp_path))
    expected = corrupt(_expected_rows(pilot))
    with pytest.raises(HumanReviewExpectedPopulationError):
        verify_human_review(review, expected)


def test_duplicate_expected_row_cannot_collapse_into_an_accepted_review(pilot, tmp_path):
    """Astra probe: 4 expected entries (one duplicated) vs a 3-row review
    used to be accepted after dict collapse."""
    review = _reviewed(tmp_path, _preview(tmp_path))
    expected = _expected_rows(pilot)
    assert len(verify_human_review(review, expected)) == 3
    with pytest.raises(HumanReviewExpectedPopulationError):
        verify_human_review(review, _dup_expected_id(expected))


# --- HRG-L2: strict, canonical provenance serialization ------------------

_T0 = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)

_PROVENANCE_PROBE = """
from datetime import UTC, datetime
from scripts.offline_rescore_stage12 import (
    JobSnapshot, RescoreResult, ScoringContext, compute_review_provenance,
)
t = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
snap = JobSnapshot(
    id=1, fingerprint="fp", source="s", title="t", company="c", location="l", url="u",
    posting_type=None, description="d", skills=["b", "a"], must_have_skills=["x"],
    nice_to_have_skills=[], skill_source=None, score=10, recommendation="SKIP",
    data_confidence=0.5, matched_skills=[], missing_skills=[], matched_must_have=[],
    missing_must_have=[], first_seen_at=t, last_seen_at=t,
)
ctx = ScoringContext(
    candidate_skills=frozenset({"Python", "FastAPI", "SQL", "Docker", "Pytest"}),
    target_seniority="mid", target_domain="ai",
    employment_types=frozenset({"FULL_TIME", "PART_TIME", "CONTRACT"}),
)
digest = compute_review_provenance(
    ctx, [RescoreResult(before=snap, after=snap, applied=False)], sample_seed=7
)
"""


def _probe_objects():
    namespace: dict = {}
    exec(_PROVENANCE_PROBE, namespace)
    return namespace["ctx"], namespace["snap"]


def _digest(ctx, snap, **overrides) -> str:
    before = replace(snap, **overrides)
    results = [offline_rescore_stage12.RescoreResult(before=before, after=snap, applied=False)]
    return compute_review_provenance(ctx, results, sample_seed=7)


def test_provenance_version_is_v2():
    assert REVIEW_PROVENANCE_VERSION == "stage12-human-review-v2"


def test_provenance_digest_is_deterministic_across_hash_seeds():
    ctx, snap = _probe_objects()
    in_process = _digest(ctx, snap)
    assert in_process == _digest(ctx, snap)
    repo_root = Path(__file__).resolve().parents[1]
    for seed in ("1", "999"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
        completed = subprocess.run(
            [sys.executable, "-c", _PROVENANCE_PROBE + "print(digest)\n"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        assert completed.stdout.strip() == in_process


def test_timezone_equivalent_datetimes_share_a_digest():
    ctx, snap = _probe_objects()
    plus_two = _T0.astimezone(timezone(timedelta(hours=2)))
    naive_utc = _T0.replace(tzinfo=None)
    baseline = _digest(ctx, snap, first_seen_at=_T0)
    assert plus_two.utcoffset() == timedelta(hours=2)
    assert _digest(ctx, snap, first_seen_at=plus_two) == baseline
    # Naive = UTC (project-wide semantic: SQLite drops tzinfo on read).
    assert _digest(ctx, snap, first_seen_at=naive_utc) == baseline
    assert _digest(ctx, snap, first_seen_at=_T0 + timedelta(microseconds=1)) != baseline


def test_datetime_and_its_string_do_not_collide():
    ctx, snap = _probe_objects()
    as_datetime = _digest(ctx, snap, first_seen_at=_T0)
    for text in (_T0.isoformat(), _T0.isoformat(timespec="microseconds")):
        assert _digest(ctx, snap, first_seen_at=text) != as_datetime


@pytest.mark.parametrize(
    "value",
    [object(), Decimal("1"), Decimal("0.5"), date(2026, 1, 2), {"a"}, b"bytes", 1 + 2j],
    ids=["object", "decimal_int", "decimal_frac", "date", "set", "bytes", "complex"],
)
def test_unsupported_provenance_types_fail_closed(value):
    ctx, snap = _probe_objects()
    with pytest.raises(TypeError):
        _digest(ctx, snap, title=value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_fail_closed(value):
    ctx, snap = _probe_objects()
    with pytest.raises(ValueError):
        _digest(ctx, snap, persisted_data_confidence=value)


def test_historical_v1_digest_never_authorizes(pilot, tmp_path):
    """A review stamped with the digest the v1 algorithm produced for the
    very same preview is refused."""
    with Session(pilot) as db:
        context, results = preview_all_jobs(db)
    v1_payload = {
        "version": "stage12-human-review-v1",
        "sample_seed": 12345,
        "context": {
            "candidate_skills": sorted(context.candidate_skills),
            "target_seniority": context.target_seniority,
            "target_domain": context.target_domain,
            "employment_types": sorted(context.employment_types),
        },
        "jobs": [
            {
                "id": r.before.id,
                "input": r.before.input_snapshot(),
                "after": [r.after.score, r.after.recommendation, r.after.data_confidence],
            }
            for r in sorted(results, key=lambda r: r.before.id)
        ],
    }
    v1_digest = hashlib.sha256(
        json.dumps(
            v1_payload,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v),
        ).encode("utf-8")
    ).hexdigest()
    assert v1_digest != compute_review_provenance(context, results, sample_seed=12345)

    def _stamp_v1(rows):
        for row in rows:
            row["review_provenance"] = v1_digest

    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_stamp_v1)
    _assert_blocked(pilot, tmp_path, review, HumanReviewProvenanceMismatchError)


# --- HRG-M1: approval evidence can never be overwritten -------------------


def _place_alias(mode: str, tmp_path: Path, review_bytes: bytes, target: Path) -> Path | None:
    """Arranges for the --human-review argument to alias `target` (an output
    path) via `mode`; returns the argument, or None if the platform cannot
    create that alias kind."""
    if mode == "junction_export_dir":
        if sys.platform != "win32":
            return None
        import _winapi

        real_dir = tmp_path / "real_export"
        real_dir.mkdir()
        _winapi.CreateJunction(str(real_dir), str(target.parent))
        (real_dir / target.name).write_bytes(review_bytes)
        return real_dir / target.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "same_path":
        target.write_bytes(review_bytes)
        return target
    if mode == "hardlink":
        approved = tmp_path / "approved.csv"
        approved.write_bytes(review_bytes)
        os.link(approved, target)
        return approved
    if mode == "relative":
        target.write_bytes(review_bytes)
        return Path(os.path.relpath(target, Path.cwd()))
    if mode == "case":
        target.write_bytes(review_bytes)
        alias = target.with_name(target.name.upper())
        return alias if alias.is_file() else None
    if mode == "symlink_to_output":
        target.write_bytes(review_bytes)
        link = tmp_path / "link.csv"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            return None
        return link
    if mode == "output_is_symlink":
        approved = tmp_path / "approved.csv"
        approved.write_bytes(review_bytes)
        try:
            os.symlink(approved, target)
        except (OSError, NotImplementedError):
            return None
        return approved
    raise AssertionError(mode)


@pytest.mark.parametrize("output", ["before_after", "human_review"])
@pytest.mark.parametrize(
    "mode",
    [
        "same_path",
        "hardlink",
        "relative",
        "case",
        "junction_export_dir",
        "symlink_to_output",
        "output_is_symlink",
    ],
)
def test_review_aliasing_any_output_is_rejected_before_engine(
    pilot, monkeypatch, tmp_path, output, mode
):
    review_bytes = _reviewed(tmp_path, _preview(tmp_path)).read_bytes()
    export_dir = tmp_path / "apply"
    target = _db_name_outputs(export_dir)[output]
    monkeypatch.chdir(tmp_path)
    argument = _place_alias(mode, tmp_path, review_bytes, target)
    if argument is None:
        pytest.skip(f"{mode} alias not creatable on this platform/filesystem")
    before = _scores(pilot)
    pilot.build_calls.clear()

    rc, out, err = _run_main(_apply_args(export_dir, argument))

    assert rc == 2
    assert "must not be (or alias) an export path" in err
    assert "Traceback" not in err + out
    assert pilot.build_calls == []
    assert _scores(pilot) == before
    assert argument.read_bytes() == review_bytes
    assert _audit_files(export_dir) == []


def _audit_pair(export_dir: Path) -> tuple[Path, Path]:
    files = _audit_files(export_dir)
    assert [f.suffix for f in files] == [".csv", ".json"]
    return files[0], files[1]


def test_successful_apply_preserves_exact_approval_bytes_and_sha(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path), notes="checked against posting")
    provenance = _read(review)[0]["review_provenance"]
    # A spreadsheet-style BOM must survive byte-for-byte too.
    review.write_bytes(b"\xef\xbb\xbf" + review.read_bytes())
    original = review.read_bytes()
    export_dir = tmp_path / "apply"

    rc, out, _err = _run_main(_apply_args(export_dir, review))

    assert rc == 0
    assert review.read_bytes() == original
    audit_csv, manifest_path = _audit_pair(export_dir)
    assert audit_csv.read_bytes() == original
    sha = hashlib.sha256(original).hexdigest()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "approval_sha256": sha,
        "approval_byte_length": len(original),
        "review_provenance": provenance,
        "review_provenance_version": REVIEW_PROVENANCE_VERSION,
        "audit_csv": audit_csv.name,
    }
    assert sha in out
    assert sha[:16] in audit_csv.name
    assert provenance[:16] in audit_csv.name
    # Both exports were published next to -- not over -- the audit artifact.
    for path in _db_name_outputs(export_dir).values():
        assert path.is_file()
        assert not os.path.samefile(path, audit_csv)


def test_audit_exists_before_apply_and_survives_late_input_replacement(
    pilot, monkeypatch, tmp_path
):
    review = _reviewed(tmp_path, _preview(tmp_path))
    original = review.read_bytes()
    export_dir = tmp_path / "apply"
    real_apply = offline_rescore_stage12.apply_rescore
    seen_at_apply: list[bytes] = []

    def _apply_after_input_swap(*args, **kwargs):
        audit_csv, _manifest = _audit_pair(export_dir)
        seen_at_apply.append(audit_csv.read_bytes())
        review.write_bytes(b"job_id\n999\n")  # input replaced after validation
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(offline_rescore_stage12, "apply_rescore", _apply_after_input_swap)

    assert main(_apply_args(export_dir, review)) == 0

    assert seen_at_apply == [original]
    audit_csv, _manifest = _audit_pair(export_dir)
    assert audit_csv.read_bytes() == original


@pytest.mark.parametrize("output", ["before_after", "human_review"])
def test_late_output_alias_to_audit_fails_closed_and_audit_is_untouched(
    pilot, monkeypatch, tmp_path, output
):
    review = _reviewed(tmp_path, _preview(tmp_path))
    original = review.read_bytes()
    export_dir = tmp_path / "apply"
    target = _db_name_outputs(export_dir)[output]
    real_apply = offline_rescore_stage12.apply_rescore

    def _apply_then_alias(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        audit_csv, _manifest = _audit_pair(export_dir)
        target.unlink(missing_ok=True)
        os.link(audit_csv, target)
        return result

    monkeypatch.setattr(offline_rescore_stage12, "apply_rescore", _apply_then_alias)

    with pytest.raises(ApprovalEvidenceAliasError):
        main(_apply_args(export_dir, review))

    audit_csv, _manifest = _audit_pair(export_dir)
    assert audit_csv.read_bytes() == original


def test_publication_never_writes_through_an_existing_hardlink(tmp_path):
    victim = tmp_path / "victim.csv"
    victim.write_bytes(b"precious")
    out = tmp_path / "out.csv"
    os.link(victim, out)

    offline_rescore_stage12._write_human_review_csv(out, [])

    assert victim.read_bytes() == b"precious"
    assert out.read_text(encoding="utf-8").startswith("job_id,")
    assert list(tmp_path.glob("*.tmp")) == []


def _audit_csv_path(export_dir: Path, review: Path) -> Path:
    provenance = _read(review)[0]["review_provenance"]
    approval = ApprovedReview(rows=[], data=review.read_bytes())
    return approval_audit_paths(export_dir, STAGE12_PILOT_DATABASE, approval, provenance)[0]


def _assert_audit_refused(engine, export_dir: Path, review: Path) -> None:
    before = _scores(engine)
    engine.statements.clear()
    rc, out, err = _run_main(_apply_args(export_dir, review))
    assert rc == 2
    assert f"({HumanReviewAuditError.__name__})" in err
    assert "Traceback" not in err + out
    assert _writes(engine) == []
    assert _scores(engine) == before


def test_existing_different_audit_artifact_is_never_overwritten(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    export_dir = tmp_path / "apply"
    export_dir.mkdir()
    occupied = _audit_csv_path(export_dir, review)
    occupied.write_bytes(b"older evidence")

    _assert_audit_refused(pilot, export_dir, review)

    assert occupied.read_bytes() == b"older evidence"


def test_audit_path_occupied_by_directory_blocks(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    export_dir = tmp_path / "apply"
    export_dir.mkdir()
    _audit_csv_path(export_dir, review).mkdir()

    _assert_audit_refused(pilot, export_dir, review)


def test_audit_write_failure_blocks_apply(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))

    def _failing_fsync(fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(offline_rescore_stage12.os, "fsync", _failing_fsync)

    _assert_audit_refused(pilot, tmp_path / "apply", review)


def test_review_input_that_is_the_audit_path_blocks(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    export_dir = tmp_path / "apply"
    export_dir.mkdir()
    as_audit = _audit_csv_path(export_dir, review)
    as_audit.write_bytes(review.read_bytes())

    _assert_audit_refused(pilot, export_dir, as_audit)


def test_identical_audit_from_a_refused_apply_allows_retry(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    export_dir = tmp_path / "apply"
    real_apply = offline_rescore_stage12.apply_rescore

    def _refused_apply(*args, **kwargs):
        raise RescoreConcurrentModificationError("simulated drift")

    monkeypatch.setattr(offline_rescore_stage12, "apply_rescore", _refused_apply)
    with pytest.raises(RescoreConcurrentModificationError):
        main(_apply_args(export_dir, review))
    first = [p.read_bytes() for p in _audit_pair(export_dir)]

    monkeypatch.setattr(offline_rescore_stage12, "apply_rescore", real_apply)
    assert main(_apply_args(export_dir, review)) == 0
    assert [p.read_bytes() for p in _audit_pair(export_dir)] == first


# --- HRG-L3: controlled, redacted CLI failures -----------------------------

PRIVATE_MARKER = "SYNTHETIC_PRIVATE_MARKER_7f3a"


def test_malformed_job_id_is_reported_without_echo(pilot, tmp_path):
    def _mutate(rows):
        rows[0]["job_id"] = PRIVATE_MARKER

    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_mutate)
    err = _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)
    assert "data row" in err
    assert PRIVATE_MARKER not in err


def test_unexpected_header_is_reported_without_echo(pilot, tmp_path):
    rows = _read(_reviewed(tmp_path, _preview(tmp_path)))
    review = _write(tmp_path / "extra.csv", rows, [*HUMAN_REVIEW_COLUMNS, PRIVATE_MARKER])
    err = _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)
    assert "1 unexpected column" in err
    assert PRIVATE_MARKER not in err


@pytest.mark.parametrize("column", ["title", "human_notes", "description_or_url"])
def test_row_content_is_never_echoed(pilot, tmp_path, column):
    def _mutate(rows):
        rows[0][column] = PRIVATE_MARKER
        rows[0]["human_decision"] = "REJECT"

    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_mutate)
    if column == "human_notes":
        error = HumanReviewRejectedError
    else:
        error = HumanReviewProvenanceMismatchError
    assert PRIVATE_MARKER not in _assert_blocked(pilot, tmp_path, review, error)


def test_review_file_disappearing_after_cli_check_is_a_controlled_abort(
    pilot, monkeypatch, tmp_path
):
    review = _reviewed(tmp_path, _preview(tmp_path))
    fake_build = offline_rescore_stage12.build_engine

    def _build_then_delete(args):
        review.unlink()
        return fake_build(args)

    monkeypatch.setattr(offline_rescore_stage12, "build_engine", _build_then_delete)
    _assert_blocked(pilot, tmp_path, review, HumanReviewFileMissingError)


def test_unreadable_review_file_is_a_controlled_abort(pilot, monkeypatch, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    real_read_bytes = Path.read_bytes

    def _read_bytes(self):
        if self == review:
            raise PermissionError(f"denied {PRIVATE_MARKER}")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    err = _assert_blocked(pilot, tmp_path, review, HumanReviewFileMissingError)
    assert PRIVATE_MARKER not in err


# --- HRG-M1-R1: an existing audit artifact must itself be fsynced ----------


class _FsyncProbe:
    """Replaces os.fsync. `fail(n)` decides whether the n-th call (1-based,
    counted across the whole test) fails. Records the file identity
    (st_dev, st_ino) of every SUCCESSFUL sync."""

    def __init__(self, fail):
        self.fail = fail
        self.calls = 0
        self.synced: set[tuple[int, int]] = set()
        self._real = os.fsync

    def __call__(self, fd):
        self.calls += 1
        if self.fail(self.calls):
            raise OSError("simulated fsync failure")
        self._real(fd)
        st = os.fstat(fd)
        self.synced.add((st.st_dev, st.st_ino))


def _identity(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_dev, st.st_ino


def test_persistent_fsync_failure_blocks_every_retry(pilot, monkeypatch, tmp_path):
    """Astra HRG-M1-R1: with fsync failing on EVERY call, attempt 1 leaves the
    CSV, attempt 2 leaves the manifest, and attempt 3 used to accept both by
    byte equality and apply. Every attempt must fsync and refuse."""
    review = _reviewed(tmp_path, _preview(tmp_path))
    export_dir = tmp_path / "apply"
    probe = _FsyncProbe(fail=lambda n: True)
    monkeypatch.setattr(offline_rescore_stage12.os, "fsync", probe)
    calls = _record_call_order(monkeypatch)

    for attempt in (1, 2, 3):
        calls_before = probe.calls
        _assert_audit_refused(pilot, export_dir, review)
        assert probe.calls > calls_before, f"attempt {attempt} skipped fsync"

    assert probe.synced == set()
    assert "apply_rescore" not in calls
    assert "lock_pilot_population" not in calls
    # Attempt 1 left the unsynced CSV visible; every retry re-attempted its
    # fsync, failed, and so never even reached manifest creation.
    assert len(_audit_files(export_dir)) == 1


@pytest.mark.parametrize("failing_call", [1, 2], ids=["csv_fsync_fails", "manifest_fsync_fails"])
def test_fsync_recovery_requires_both_artifacts_synced_before_apply(
    pilot, monkeypatch, tmp_path, failing_call
):
    review = _reviewed(tmp_path, _preview(tmp_path))
    export_dir = tmp_path / "apply"
    probe = _FsyncProbe(fail=lambda n: n == failing_call)
    monkeypatch.setattr(offline_rescore_stage12.os, "fsync", probe)

    _assert_audit_refused(pilot, export_dir, review)
    # The artifact whose fsync failed is visible but was never synced.
    failed_artifact = _audit_files(export_dir)[failing_call - 1]
    assert _identity(failed_artifact) not in probe.synced

    real_apply = offline_rescore_stage12.apply_rescore
    synced_at_apply: list[bool] = []

    def _apply_checking_durability(*args, **kwargs):
        audit_csv, manifest = _audit_pair(export_dir)
        synced_at_apply.append({_identity(audit_csv), _identity(manifest)} <= probe.synced)
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(offline_rescore_stage12, "apply_rescore", _apply_checking_durability)
    calls_before = probe.calls

    assert main(_apply_args(export_dir, review)) == 0

    assert synced_at_apply == [True]
    # Retry re-synced the existing artifact(s) rather than trusting them.
    assert probe.calls - calls_before == 2
    audit_csv, _manifest = _audit_pair(export_dir)
    assert audit_csv.read_bytes() == review.read_bytes()


def test_existing_artifact_with_extra_link_is_refused_even_if_bytes_match(pilot, tmp_path):
    review = _reviewed(tmp_path, _preview(tmp_path))
    export_dir = tmp_path / "apply"
    export_dir.mkdir()
    audit = _audit_csv_path(export_dir, review)
    audit.write_bytes(review.read_bytes())
    os.link(audit, tmp_path / "second_link.csv")

    _assert_audit_refused(pilot, export_dir, review)

    assert audit.read_bytes() == review.read_bytes()


# --- HRG-L3-R1: bounded job_id parsing ---------------------------------------

HUGE_JOB_ID = "9" * 4301


def _set_job_id(index: int, value: str):
    def _mutate(rows):
        rows[index]["job_id"] = value

    return _mutate


@pytest.mark.parametrize(
    ("mutate", "raw"),
    [
        (_set_job_id(0, HUGE_JOB_ID), HUGE_JOB_ID),
        (
            lambda rows: [_set_job_id(i, HUGE_JOB_ID)(rows) for i in (0, 1)],
            HUGE_JOB_ID,
        ),
        (_set_job_id(0, str(offline_rescore_stage12.MAX_JOB_ID + 1)), "2147483648"),
        (_set_job_id(0, "1" * 11), "1" * 11),
        (_set_job_id(0, "0"), None),
        (_set_job_id(0, "01"), None),
        (_set_job_id(0, "+1"), None),
        (_set_job_id(0, "-1"), None),
        (_set_job_id(0, "１"), None),  # fullwidth digit one
    ],
    ids=[
        "4301_digits",
        "duplicated_4301_digits",
        "max_plus_one",
        "eleven_digits",
        "zero",
        "leading_zero",
        "plus_sign",
        "minus_sign",
        "non_ascii_digit",
    ],
)
def test_out_of_range_or_noncanonical_job_id_is_a_controlled_abort(
    pilot, monkeypatch, tmp_path, mutate, raw
):
    review = _reviewed(tmp_path, _preview(tmp_path), mutate=mutate)
    calls = _record_call_order(monkeypatch)

    err = _assert_blocked(pilot, tmp_path, review, HumanReviewMalformedError)

    assert err.count("ABORT") == 1
    assert "data row 1:" in err
    if raw is not None:
        assert raw not in err
    assert calls == ["enforce_human_review_gate"]


def test_largest_supported_job_id_parses_and_is_judged_by_population(pilot, tmp_path):
    max_id = offline_rescore_stage12.MAX_JOB_ID
    assert max_id == 2**31 - 1
    assert offline_rescore_stage12._parse_job_id(str(max_id), row_index=1) == max_id
    assert offline_rescore_stage12._parse_job_id("1", row_index=1) == 1

    review = _reviewed(tmp_path, _preview(tmp_path), mutate=_set_job_id(0, str(max_id)))
    err = _assert_blocked(pilot, tmp_path, review, HumanReviewUnexpectedRowError)
    assert str(max_id) in err
