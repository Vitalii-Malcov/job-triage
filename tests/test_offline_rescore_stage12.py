"""Tests for scripts/offline_rescore_stage12.py -- the offline, in-place
re-score tool built for validating a historical pilot database (e.g.
`stage12_pilot_r2`) against the canonical CandidateProfile skill projection
(CSP-001) without running any collector or creating any new JobRecord.

Test matrix (see the Codex safety-remediation review this file responds
to): A preview zero-write, B missing-profile abort, C preference-sensitive
posting never creates a profile, D apply preserves non-score fields +
reference tokens, E apply touches only the score allowlist, F injected
mid-run drift causes complete rollback, G no new rows ever possible, H
count/fingerprint invariants hold, I preview result == applied result, J
threshold fixed at 60, K no duplicate jobs across human-review groups, L
deterministic SKIP sampling, M human labels always empty, N CSV formula
escaping, O missing profile never falls back to legacy/default skills, P
before snapshot uses the persisted historical score/recommendation/
data_confidence, never a legacy-profile recomputation.
"""

import csv
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

import scripts.offline_rescore_stage12 as offline_rescore_stage12
from app.agents.job_score_evaluator import EvaluationTrace, evaluate_job_score
from app.db.base import Base
from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    count_candidate_profiles,
    get_or_create_candidate_profile,
)
from app.db.models import (
    AutomationRunRecord,
    CandidateProfileRecord,
    JobRecord,
    JobReferenceTokenRecord,
    UserProfile,
)
from app.db.repositories import get_or_create_default_profile
from app.models.candidate_profile import (
    CandidateJobPreferences,
    CandidateProfilePatchRequest,
    CandidateSkill,
)
from app.models.job import Job
from app.services.collector_runner import score_and_persist
from scripts.offline_rescore_stage12 import (
    DETERMINISTIC_SKIP_SAMPLE_SIZE,
    HIGH_SCORE_SKIP_THRESHOLD,
    HUMAN_REVIEW_COLUMNS,
    POPULATION_LOCK_TABLES,
    STAGE12_PILOT_DATABASE,
    JobSnapshot,
    MissingCandidateProfileError,
    PilotIdentityMismatchError,
    RescoreConcurrentModificationError,
    RescoreResult,
    _apply_target_authorized,
    _build_human_review_rows,
    _csv_safe,
    _is_sentinel,
    _write_before_after_csv,
    _write_human_review_csv,
    apply_rescore,
    lock_pilot_population,
    main,
    population_lock_statements,
    preview_all_jobs,
    verify_pilot_identity,
)

RICH_TECH_DESCRIPTION = (
    "We build REST APIs with Python, FastAPI and SQLAlchemy against "
    "PostgreSQL. Git-based workflow, automated tests with Pytest, "
    "containerized with Docker. " * 6
)

RICH_SKILLS = ["Python", "FastAPI", "SQLAlchemy", "REST API", "Docker", "Pytest"]


def _engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return engine


def _set_candidate_skills(
    db: Session,
    skills: list[str],
    *,
    employment_types: list[str] | None = None,
    target_roles: list[str] | None = None,
) -> None:
    current = get_or_create_candidate_profile(db)
    # `target_roles` must stay OUT of the patch's `model_fields_set` unless
    # the caller actually asked for it -- the repository applies any
    # explicitly-provided field, including an explicit None.
    extra = {} if target_roles is None else {"target_roles": target_roles}
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=current.profile_version,
            skills=[CandidateSkill(name=name) for name in skills],
            job_preferences=CandidateJobPreferences(employment_types=employment_types or []),
            **extra,
        ),
    )


def _job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "AI Engineer",
        "company": "Example GmbH",
        "url": "https://example.com/jobs/rescore-1",
        "description": RICH_TECH_DESCRIPTION,
        "posting_type": "ARBEIT",
        "must_have_skills": ["Python", "FastAPI", "SQLAlchemy", "REST API"],
        "nice_to_have_skills": ["Docker", "Pytest"],
    }
    data.update(overrides)
    return Job(**data)


def _seed_job(db: Session, candidate_skills: frozenset[str], **overrides) -> JobRecord:
    """Seeds one persisted JobRecord via the real production path (as if a
    collector had ingested it) -- independent of the candidate skills the
    rescore script will later use.
    """
    job = _job(**overrides)
    record, _score, _created = score_and_persist(db, candidate_skills, job)
    return record


def _snap(job_id: int, recommendation: str, score: int) -> JobSnapshot:
    now = datetime.now(UTC)
    return JobSnapshot(
        id=job_id,
        fingerprint=f"fp-{job_id}",
        source="bundesagentur",
        title=f"Job {job_id}",
        company="Example GmbH",
        location="",
        url="https://example.com/jobs/1",
        posting_type="ARBEIT",
        description=RICH_TECH_DESCRIPTION,
        skills=["python"],
        must_have_skills=["python"],
        nice_to_have_skills=[],
        skill_source=None,
        score=score,
        recommendation=recommendation,
        data_confidence=1.0,
        matched_skills=["python"],
        missing_skills=[],
        matched_must_have=["python"],
        missing_must_have=[],
        first_seen_at=now,
        last_seen_at=now,
    )


def _fake_result(job_id: int, recommendation: str, score: int) -> RescoreResult:
    snap = _snap(job_id, recommendation, score)
    return RescoreResult(before=snap, after=snap, applied=False)


def _authorize_pilot_identity(
    monkeypatch, *, jobs: int, automation_runs: int = 0, profiles: int = 1
) -> None:
    """Points the hard-pinned Stage 12 pilot-identity expectations (used by
    BOTH the CLI preflight and `apply_rescore`'s own in-transaction
    recheck, via the shared `verify_pilot_identity` helper) at whatever
    THIS test's own fixture actually seeded, so ordinary `apply_rescore`
    behavior tests don't need to fabricate a full 222-job/2-automation-run
    pilot dataset just to get past the identity gate. Production
    (`scripts.offline_rescore_stage12.main`) never calls this -- it always
    runs against the real `STAGE12_PILOT_*` constants.
    """
    monkeypatch.setattr(offline_rescore_stage12, "STAGE12_PILOT_JOB_COUNT", jobs)
    monkeypatch.setattr(
        offline_rescore_stage12, "STAGE12_PILOT_AUTOMATION_RUNS_COUNT", automation_runs
    )
    monkeypatch.setattr(offline_rescore_stage12, "STAGE12_PILOT_CANDIDATE_PROFILE_COUNT", profiles)


def _read_score_tuple(db: Session, job_id: int) -> tuple:
    record = db.get(JobRecord, job_id)
    return (record.score, record.recommendation, record.data_confidence)


# --- A: preview is zero-write with an existing CandidateProfile ----------


def test_A_preview_zero_writes_with_existing_profile():
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        seeded = _seed_job(db, frozenset(["Python"]))
        job_id, before_score, before_rec, before_last_seen = (
            seeded.id,
            seeded.score,
            seeded.recommendation,
            seeded.last_seen_at,
        )

    with Session(engine) as db:
        context, results = preview_all_jobs(db)
        assert not db.new
        assert not db.dirty
        assert not db.deleted

    assert len(results) == 1
    assert context is not None

    with Session(engine) as db:
        reloaded = db.get(JobRecord, job_id)
        assert reloaded.score == before_score
        assert reloaded.recommendation == before_rec
        assert reloaded.last_seen_at == before_last_seen


# --- B: preview aborts without a CandidateProfile, zero writes -----------


def test_B_preview_aborts_without_candidate_profile():
    engine = _engine()
    with Session(engine) as db:
        job = _job()
        record, _score, _created = score_and_persist(db, frozenset(["Python"]), job)
        job_id = record.id

    with Session(engine) as db:
        with pytest.raises(MissingCandidateProfileError):
            preview_all_jobs(db)
        assert not db.new
        assert not db.dirty
        assert not db.deleted

    with Session(engine) as db:
        assert db.get(JobRecord, job_id) is not None
        assert count_candidate_profiles(db) == 0


# --- C: preference-sensitive posting never creates a profile row ---------


def test_get_or_create_candidate_profile_not_reachable_from_script():
    import scripts.offline_rescore_stage12 as module

    assert not hasattr(module, "get_or_create_candidate_profile")


def test_C_preference_sensitive_posting_does_not_create_profile_row():
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, ["Python"], employment_types=["APPRENTICESHIP"])
        _seed_job(
            db,
            frozenset(["Python"]),
            posting_type="AUSBILDUNG",
            title="Python Auszubildender",
        )
        assert count_candidate_profiles(db) == 1

    with Session(engine) as db:
        _context, results = preview_all_jobs(db)
        assert count_candidate_profiles(db) == 1

    assert len(results) == 1


# --- D: apply preserves non-score fields and reference tokens ------------


def test_D_apply_preserves_non_score_fields_and_reference_tokens(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        seeded = _seed_job(db, frozenset(["Python"]))
        job_id = seeded.id
        original = {
            "fingerprint": seeded.fingerprint,
            "first_seen_at": seeded.first_seen_at,
            "last_seen_at": seeded.last_seen_at,
            "description": seeded.description,
            "skills_json": seeded.skills_json,
            "must_have_skills_json": seeded.must_have_skills_json,
            "nice_to_have_skills_json": seeded.nice_to_have_skills_json,
            "posting_type": seeded.posting_type,
            "skill_source": seeded.skill_source,
            "source": seeded.source,
            "title": seeded.title,
            "company": seeded.company,
            "location": seeded.location,
            "url": seeded.url,
            "status": seeded.status,
        }
        ref_tokens_before = sorted(
            t.token
            for t in db.scalars(
                select(JobReferenceTokenRecord).where(JobReferenceTokenRecord.job_id == job_id)
            )
        )
        assert ref_tokens_before  # sanity: seeding actually produced tokens

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1)
    apply_rescore(engine, context, results)

    with Session(engine) as db:
        reloaded = db.get(JobRecord, job_id)
        for field, value in original.items():
            assert getattr(reloaded, field) == value, f"{field} changed unexpectedly"
        ref_tokens_after = sorted(
            t.token
            for t in db.scalars(
                select(JobReferenceTokenRecord).where(JobReferenceTokenRecord.job_id == job_id)
            )
        )
        assert ref_tokens_after == ref_tokens_before


# --- E: apply changes only the explicit score allowlist -------------------


def test_E_apply_changes_only_score_allowlist_fields(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        # Seeded against a deliberately POOR skill match so the canonical
        # (rich) profile produces a genuinely different score/recommendation.
        seeded = _seed_job(db, frozenset(["Cobol"]))
        job_id = seeded.id
        before_score, before_rec, before_conf = (
            seeded.score,
            seeded.recommendation,
            seeded.data_confidence,
        )

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1)
    applied = apply_rescore(engine, context, results)
    assert len(applied) == 1

    with Session(engine) as db:
        reloaded = db.get(JobRecord, job_id)
        # The allowlisted fields DID change (canonical skills score higher).
        assert (reloaded.score, reloaded.recommendation, reloaded.data_confidence) != (
            before_score,
            before_rec,
            before_conf,
        )
        assert reloaded.score == applied[0].after.score
        assert reloaded.recommendation == applied[0].after.recommendation


# --- F: injected mid-run drift causes complete rollback -------------------


def test_F_injected_mid_run_drift_causes_complete_rollback(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        ids = []
        for i in range(3):
            # Seeded against a deliberately POOR skill match (only "Python"
            # of the job's full must-have/nice-to-have list) so the
            # canonical (rich) profile's rescore is a genuinely different,
            # non-vacuous result for these EARLIER jobs too -- not just for
            # the later job that gets its title drifted below.
            rec = _seed_job(
                db,
                frozenset(["Python"]),
                title=f"Backend Developer {i}",
                url=f"https://example.com/jobs/f-{i}",
            )
            ids.append(rec.id)
        original = {}
        for jid in ids:
            r = db.get(JobRecord, jid)
            original[jid] = (r.score, r.recommendation, r.data_confidence)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    # Non-vacuous rollback proof (Codex finding): before triggering drift,
    # confirm at least one EARLIER job's canonical rescore actually differs
    # from its persisted value -- i.e. apply would genuinely have written
    # something for it were it not for the later row's drift. Otherwise a
    # rollback test could "pass" even if rollback were silently broken,
    # simply because there was nothing to roll back.
    results_by_id = {r.before.id: r for r in results}
    earlier_ids = ids[:-1]
    assert any(
        (results_by_id[jid].after.score, results_by_id[jid].after.recommendation)
        != (
            results_by_id[jid].after.persisted_score,
            results_by_id[jid].after.persisted_recommendation,
        )
        for jid in earlier_ids
    ), "expected at least one earlier job's canonical rescore to differ from its persisted value"

    _authorize_pilot_identity(monkeypatch, jobs=3)

    # Simulate drift on the LAST job after preview captured its snapshot --
    # a title edit (e.g. a concurrent manual correction) changes the
    # scoring-input snapshot without touching score/recommendation/
    # data_confidence directly. Apply must detect this and roll back ALL
    # THREE jobs, not just this one.
    with Session(engine) as db:
        last = db.get(JobRecord, ids[-1])
        last.title = "DRIFTED TITLE (concurrent edit)"
        db.commit()

    with pytest.raises(RescoreConcurrentModificationError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        for jid in ids[:-1]:
            r = db.get(JobRecord, jid)
            assert (r.score, r.recommendation, r.data_confidence) == original[jid]
        drifted = db.get(JobRecord, ids[-1])
        assert drifted.title == "DRIFTED TITLE (concurrent edit)"
        assert (drifted.score, drifted.recommendation, drifted.data_confidence) == original[
            ids[-1]
        ]  # untouched by apply beyond the injected drift itself


# --- G: created-row path is structurally impossible ------------------------


def test_G_apply_never_creates_new_rows(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        for i in range(4):
            _seed_job(
                db,
                frozenset(["Python"]),
                title=f"Job {i}",
                url=f"https://example.com/jobs/g-{i}",
            )

    with Session(engine) as db:
        context, results = preview_all_jobs(db)
        count_before = db.scalar(select(func.count()).select_from(JobRecord))

    _authorize_pilot_identity(monkeypatch, jobs=4)
    apply_rescore(engine, context, results)

    with Session(engine) as db:
        count_after = db.scalar(select(func.count()).select_from(JobRecord))
    assert count_before == count_after == 4


# --- H: fingerprint/count invariants remain unchanged ----------------------


def test_H_fingerprint_and_count_invariants_unchanged(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        for i in range(5):
            _seed_job(
                db,
                frozenset(["Python"]),
                title=f"Job {i}",
                url=f"https://example.com/jobs/h-{i}",
            )

    with Session(engine) as db:
        context, results = preview_all_jobs(db)
        fp_before = sorted(db.scalars(select(JobRecord.fingerprint)))

    _authorize_pilot_identity(monkeypatch, jobs=5)
    apply_rescore(engine, context, results)

    with Session(engine) as db:
        fp_after = sorted(db.scalars(select(JobRecord.fingerprint)))
        unique_after = db.scalar(select(func.count(func.distinct(JobRecord.fingerprint))))
    assert fp_after == fp_before
    assert unique_after == 5


# --- I: preview result == applied result -----------------------------------


def test_I_preview_result_equals_applied_result(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]))

    with Session(engine) as db:
        context, preview_results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1)
    applied_results = apply_rescore(engine, context, preview_results)

    assert len(applied_results) == 1
    assert applied_results[0].after.score == preview_results[0].after.score
    assert applied_results[0].after.recommendation == preview_results[0].after.recommendation
    assert applied_results[0].after.data_confidence == preview_results[0].after.data_confidence


# --- J: threshold fixed at 60 ------------------------------------------------


def test_J_threshold_is_fixed_at_60():
    assert HIGH_SCORE_SKIP_THRESHOLD == 60


# --- K/L/M: human review sampling -------------------------------------------


def test_K_high_score_and_deterministic_skip_groups_have_no_duplicates_and_M_labels_empty():
    results = []
    for i in range(3):
        results.append(_fake_result(i, "APPLY", 85))
    for i in range(3, 6):
        results.append(_fake_result(i, "MAYBE", 60))
    for i in range(6, 8):
        results.append(_fake_result(i, "SKIP", 65))  # high-score SKIP (>= 60)
    for i in range(8, 38):
        results.append(_fake_result(i, "SKIP", 10))  # 30 low-score SKIPs

    rows, counts = _build_human_review_rows(results, sample_seed=1)

    assert counts["apply"] == 3
    assert counts["maybe"] == 3
    assert counts["high_score_skip"] == 2
    assert counts["deterministic_skip"] == DETERMINISTIC_SKIP_SAMPLE_SIZE
    job_ids = [row["job_id"] for row in rows]
    assert len(job_ids) == len(set(job_ids))
    assert len(rows) == 3 + 3 + 2 + DETERMINISTIC_SKIP_SAMPLE_SIZE
    for row in rows:
        assert row["human_relevant"] == ""
        assert row["human_decision"] == ""
        assert row["human_notes"] == ""


def test_L_deterministic_skip_sample_is_reproducible_for_same_seed():
    results = [_fake_result(i, "SKIP", 10) for i in range(50)]

    rows_a, _ = _build_human_review_rows(results, sample_seed=7)
    rows_b, _ = _build_human_review_rows(results, sample_seed=7)

    assert [r["job_id"] for r in rows_a] == [r["job_id"] for r in rows_b]
    assert len(rows_a) == DETERMINISTIC_SKIP_SAMPLE_SIZE


# --- N: CSV formula-injection escaping --------------------------------------


def test_N_csv_formula_prefixes_are_neutralized():
    assert _csv_safe("=cmd|'/c calc'!A1").startswith("'=")
    assert _csv_safe("+1+1").startswith("'+")
    assert _csv_safe("-1+1").startswith("'-")
    assert _csv_safe("@SUM(A1)").startswith("'@")
    assert _csv_safe("Normal Title") == "Normal Title"
    assert _csv_safe("") == ""


def test_N_human_review_rows_escape_untrusted_title_and_company():
    poisoned = _fake_result(1, "APPLY", 90)
    poisoned.after.title = '=HYPERLINK("http://evil")'
    poisoned.after.company = "+SUM(1,1)"

    rows, _ = _build_human_review_rows([poisoned], sample_seed=1)

    assert rows[0]["title"].startswith("'=")
    assert rows[0]["company"].startswith("'+")


@pytest.mark.parametrize("poisoned_value", ["=CMD", "+SUM", "@foo", "-1+1"])
def test_N_csv_posting_type_is_escaped_in_both_csv_paths(tmp_path, poisoned_value):
    poisoned = _fake_result(1, "APPLY", 90)
    poisoned.after.posting_type = poisoned_value

    rows, _ = _build_human_review_rows([poisoned], sample_seed=1)
    assert rows[0]["posting_type"] == "'" + poisoned_value

    out_path = tmp_path / "before_after.csv"
    _write_before_after_csv(out_path, [poisoned])
    content = out_path.read_text(encoding="utf-8")
    assert ("'" + poisoned_value) in content


# --- O: missing profile never falls back to legacy/default skills ----------


def test_O_missing_profile_never_falls_back_to_legacy_or_default_skills():
    engine = _engine()
    with Session(engine) as db:
        get_or_create_default_profile(db)  # legacy UserProfile("default"), pre-CSP-001
        job = _job()
        score_and_persist(db, frozenset(["Python"]), job)

    with Session(engine) as db:
        with pytest.raises(MissingCandidateProfileError):
            preview_all_jobs(db)


# --- P: before snapshot is the PERSISTED historical result, never a --------
# --- legacy-profile recomputation -------------------------------------------


def test_P_preview_before_snapshot_uses_persisted_historical_values_not_legacy_recompute(
    tmp_path,
):
    """Codex finding: `preview_all_jobs` used to set `before.score`/
    `before.recommendation`/`before.data_confidence` to the result of
    RECOMPUTING against a recovered legacy UserProfile with CURRENT-CODE
    gate/threshold logic (Stage 11A/11B/11E/11C did not exist when the
    historical Stage 12 scores were produced) -- silently replacing the
    actual historical baseline with a hybrid "old skills, new gates"
    result. The before snapshot's score/recommendation/data_confidence
    must always be exactly what Stage 12 actually persisted
    (`JobRecord.score`/`.recommendation`/`.data_confidence`); the legacy
    profile, when recoverable, may only reconstruct EXPLANATORY evidence
    (matched/missing skill lists), never the outcome itself.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        # Persisted (historical) result: scored against a POOR skill match.
        seeded = _seed_job(db, frozenset(["Cobol"]))
        job_id = seeded.id
        persisted_score = seeded.score
        persisted_recommendation = seeded.recommendation
        persisted_data_confidence = seeded.data_confidence

        # A recovered legacy UserProfile with a RICH skill set -- if
        # (wrongly) used as the before score/recommendation, this would
        # produce a genuinely different (APPLY) result.
        db.add(UserProfile(name="default", skills_json=json.dumps(RICH_SKILLS)))
        db.commit()

    with Session(engine) as db:
        context, results = preview_all_jobs(db)
        record = db.get(JobRecord, job_id)
        legacy_job = offline_rescore_stage12._job_from_record(record)
        posting_type = record.posting_type

    assert len(results) == 1
    result = results[0]

    # Non-vacuous proof: the legacy-profile recompute really would have
    # differed from the persisted historical recommendation -- otherwise
    # this test could pass even if the bug were still present.
    legacy_would_have_scored = evaluate_job_score(
        legacy_job,
        posting_type,
        candidate_skills=frozenset(RICH_SKILLS),
        allowed_employment_types=frozenset(),
        get_candidate_target_seniority=lambda: context.target_seniority,
        get_candidate_target_domain=lambda: context.target_domain,
    )
    assert legacy_would_have_scored.recommendation != persisted_recommendation, (
        "expected legacy-profile recompute to genuinely differ from the persisted "
        "historical recommendation -- otherwise this test proves nothing"
    )

    # The historical BEFORE result is the PERSISTED value, not the recompute.
    assert result.before.score == persisted_score
    assert result.before.recommendation == persisted_recommendation
    assert result.before.data_confidence == persisted_data_confidence

    # Reconstructed legacy EVIDENCE is still present and clearly marked as
    # such -- distinct from the (never used) recomputed outcome.
    assert result.before.matched_skills_source == "recomputed_legacy_profile_evidence"
    assert result.before.matched_must_have == legacy_would_have_scored.matched_must_have
    assert result.before.missing_must_have == legacy_would_have_scored.missing_must_have
    assert result.before.matched_must_have, "expected non-trivial reconstructed evidence"

    # The before/after CSV export must reflect the PERSISTED before, not
    # the legacy recompute.
    csv_path = tmp_path / "before_after.csv"
    _write_before_after_csv(csv_path, results)
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["before_score"] == str(persisted_score)
    assert rows[0]["before_recommendation"] == persisted_recommendation
    assert rows[0]["before_recommendation"] != legacy_would_have_scored.recommendation

    # The before distribution / transition matrix (main()'s own
    # before_counts/transition logic operates directly on r.before.* --
    # replicated here) must be keyed off the PERSISTED recommendation.
    before_counts: dict[str, int] = {}
    for r in results:
        before_counts[r.before.recommendation] = before_counts.get(r.before.recommendation, 0) + 1
    assert before_counts == {persisted_recommendation: 1}


# --- sentinel title matching -------------------------------------------------


def test_sentinel_detection_matches_known_titles_case_insensitively():
    assert _is_sentinel("AI Engineer")
    assert _is_sentinel("senior ai engineer (m/w/d)")
    assert _is_sentinel("KI-Entwickler / Python")
    assert _is_sentinel("Junior Cyber Security Developer")
    assert not _is_sentinel("Frontend Developer")


# --- Concurrency snapshot: RAW persisted score-result/last_seen_at drift ----
# Stage 12 final-hardening review (Codex): the concurrency snapshot used to
# compare only scoring-INPUT fields -- a concurrent write that changed only
# the persisted score/recommendation/data_confidence (e.g. a manual
# POST /jobs/score, or a second offline-rescore run) or only last_seen_at
# (e.g. a live collector re-ingesting the same fingerprint) went completely
# undetected. `JobSnapshot.persisted_score`/`persisted_recommendation`/
# `persisted_data_confidence` (raw, straight off the record, independent of
# whichever before/after narrative the snapshot represents) plus
# `last_seen_at` are now part of `input_snapshot()`, so all four are caught
# the same way a title/skills drift already was.


@pytest.mark.parametrize("field", ["score", "recommendation", "data_confidence"])
def test_persisted_score_result_field_drift_after_preview_blocks_apply(monkeypatch, field):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        drifted = _seed_job(
            db,
            frozenset(["Cobol"]),
            title="Drift Target",
            url="https://example.com/jobs/drift-score-1",
        )
        control = _seed_job(
            db,
            frozenset(["Cobol"]),
            title="Control Job",
            url="https://example.com/jobs/drift-score-2",
        )
        drift_id, control_id = drifted.id, control.id
        control_original = (control.score, control.recommendation, control.data_confidence)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=2)

    with Session(engine) as db:
        record = db.get(JobRecord, drift_id)
        current = getattr(record, field)
        if field == "score":
            external_value = 3 if current != 3 else 97
        elif field == "recommendation":
            external_value = "SKIP" if current != "SKIP" else "MAYBE"
        else:
            external_value = 0.11 if current != 0.11 else 0.89
        setattr(record, field, external_value)
        db.commit()

    with pytest.raises(RescoreConcurrentModificationError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        # external change remains
        assert getattr(db.get(JobRecord, drift_id), field) == external_value
        # offline apply changed ZERO other rows
        reloaded_control = db.get(JobRecord, control_id)
        assert (
            reloaded_control.score,
            reloaded_control.recommendation,
            reloaded_control.data_confidence,
        ) == control_original


def test_last_seen_at_drift_after_preview_blocks_apply(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        drifted = _seed_job(
            db,
            frozenset(["Cobol"]),
            title="Drift Target",
            url="https://example.com/jobs/drift-last-seen-1",
        )
        control = _seed_job(
            db,
            frozenset(["Cobol"]),
            title="Control Job",
            url="https://example.com/jobs/drift-last-seen-2",
        )
        drift_id, control_id = drifted.id, control.id
        original_last_seen = drifted.last_seen_at
        control_original = (control.score, control.recommendation, control.data_confidence)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=2)

    bumped_last_seen = original_last_seen + timedelta(hours=1)
    with Session(engine) as db:
        record = db.get(JobRecord, drift_id)
        record.last_seen_at = bumped_last_seen
        db.commit()

    with pytest.raises(RescoreConcurrentModificationError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        # external change remains
        assert db.get(JobRecord, drift_id).last_seen_at == bumped_last_seen
        # offline apply changed ZERO other rows
        reloaded_control = db.get(JobRecord, control_id)
        assert (
            reloaded_control.score,
            reloaded_control.recommendation,
            reloaded_control.data_confidence,
        ) == control_original


# --- Pilot identity TOCTOU: the in-transaction recheck catches drift that ---
# --- happened strictly between preview and apply's row-lock acquisition ----


def test_pilot_job_count_drift_after_preview_blocks_apply(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        ids = []
        for i in range(3):
            rec = _seed_job(
                db,
                frozenset(["Python"]),
                title=f"Pilot Job {i}",
                url=f"https://example.com/jobs/pilot-count-{i}",
            )
            ids.append(rec.id)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)
        original = {jid: _read_score_tuple(db, jid) for jid in ids}

    _authorize_pilot_identity(monkeypatch, jobs=3)

    # An extra job appears (e.g. a collector run slipping in) after preview
    # captured its snapshot but before apply's in-transaction recheck --
    # total job count no longer matches the pilot-identity expectation,
    # even though none of the THREE rows apply actually targets have
    # drifted individually.
    with Session(engine) as db:
        _seed_job(
            db,
            frozenset(["Python"]),
            title="Unexpected Extra Job",
            url="https://example.com/jobs/pilot-count-extra",
        )

    with pytest.raises(PilotIdentityMismatchError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        for jid in ids:
            assert _read_score_tuple(db, jid) == original[jid]


def test_pilot_automation_runs_drift_after_preview_blocks_apply(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        rec = _seed_job(db, frozenset(["Python"]), url="https://example.com/jobs/pilot-runs-1")
        job_id = rec.id
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

    with Session(engine) as db:
        context, results = preview_all_jobs(db)
        original = _read_score_tuple(db, job_id)

    _authorize_pilot_identity(monkeypatch, jobs=1, automation_runs=1)

    # A second automation run is recorded after preview -- the pilot's
    # automation-run count no longer matches what apply expects.
    with Session(engine) as db:
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

    with pytest.raises(PilotIdentityMismatchError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        assert _read_score_tuple(db, job_id) == original


def test_pilot_candidate_profile_drift_after_preview_blocks_apply(monkeypatch):
    """The schema's own singleton CHECK constraint
    (`ck_candidate_profiles_singleton`, `id = 1`) makes a SECOND
    `CandidateProfileRecord` row structurally impossible, so the only
    genuinely reachable "CandidateProfile identity" drift is deletion
    (profile count 1 -> 0). That is caught even earlier than
    `verify_pilot_identity`'s own profile-count check: `apply_rescore` calls
    `load_scoring_context` (which raises `MissingCandidateProfileError` for
    a missing profile) before it ever reaches the in-transaction
    pilot-identity recheck -- two independent layers converging on the same
    "abort, zero writes" outcome.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        rec = _seed_job(db, frozenset(["Python"]), url="https://example.com/jobs/pilot-profile-1")
        job_id = rec.id

    with Session(engine) as db:
        context, results = preview_all_jobs(db)
        original = _read_score_tuple(db, job_id)

    _authorize_pilot_identity(monkeypatch, jobs=1)

    with Session(engine) as db:
        db.execute(delete(CandidateProfileRecord))
        db.commit()

    with pytest.raises(MissingCandidateProfileError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        assert _read_score_tuple(db, job_id) == original


# --- Apply hard-pinned to the exact Stage 12 pilot database -----------------


def test_apply_target_authorized_requires_the_literal_pilot_constant():
    assert _apply_target_authorized(STAGE12_PILOT_DATABASE, STAGE12_PILOT_DATABASE)
    assert not _apply_target_authorized("foo", "foo")
    assert not _apply_target_authorized(STAGE12_PILOT_DATABASE, "wrong")
    assert not _apply_target_authorized("wrong", STAGE12_PILOT_DATABASE)


def test_main_rejects_apply_with_postgres_db_foo_and_matching_confirm_foo(tmp_path, capsys):
    rc = main(
        [
            "--postgres-db",
            "foo",
            "--postgres-user",
            "irrelevant",
            "--export-dir",
            str(tmp_path),
            "--apply",
            "--confirm-database",
            "foo",
        ]
    )
    assert rc == 2
    assert "ABORT" in capsys.readouterr().err


def test_main_rejects_apply_with_correct_postgres_db_but_wrong_confirm(tmp_path, capsys):
    rc = main(
        [
            "--postgres-db",
            STAGE12_PILOT_DATABASE,
            "--postgres-user",
            "irrelevant",
            "--export-dir",
            str(tmp_path),
            "--apply",
            "--confirm-database",
            "not-the-pilot",
        ]
    )
    assert rc == 2
    assert "ABORT" in capsys.readouterr().err


# --- Review export (Astra M4 finding): each reviewed vacancy's decision ---
# --- must be reconstructable from the exported evidence, without ----------
# --- changing any score/recommendation -------------------------------------


def test_review_export_includes_resolved_evidence_and_populated_gate_trace():
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]))

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    assert results[0].after.recommendation in ("APPLY", "MAYBE")
    rows, _counts = _build_human_review_rows(results, sample_seed=1)
    assert len(rows) == 1
    row = rows[0]

    # The decision is reconstructable: resolved must/nice, matches, and a
    # non-empty gate trace explaining how the recommendation was reached
    # -- M4's exact complaint was that `gate_reason` was always "".
    assert row["resolved_must_have"] != ""
    assert row["gate_reason"] != ""
    assert "posting:TARGET" in row["gate_reason"]
    assert "base_score:" in row["gate_reason"]
    assert "final:" in row["gate_reason"]
    assert row["role_relevance"] in ("RELEVANT", "IRRELEVANT", "UNKNOWN")
    assert row["seniority_classification"] in ("SENIOR", "UNKNOWN")
    assert row["posting_classification"] == "TARGET_EMPLOYMENT"
    assert row["evidence_cardinality"] in ("SUFFICIENT", "LOW_CARDINALITY")
    assert int(row["unique_evidence_count"]) >= 2
    assert row["data_confidence"] == results[0].after.data_confidence


def test_review_export_gate_trace_explains_early_posting_exclusion():
    # A job excluded at the very first gate (Stage 10 posting-type
    # exclusion) must still carry a gate trace explaining WHY -- not an
    # empty string just because later gates never ran.
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(
            db,
            frozenset(["Python"]),
            title="Programmierung mit Python",
            posting_type="SELBSTAENDIGKEIT",
        )

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    assert results[0].after.recommendation == "SKIP"
    assert results[0].after.score == 0
    evidence = results[0].after.evidence
    assert evidence.gate_trace
    assert evidence.gate_trace[0].startswith("posting_excluded:")
    assert evidence.posting_classification.startswith("EXCLUDED:")


def test_review_export_before_snapshot_never_gets_current_code_evidence():
    # Ties to the H1/P invariant: the historical BEFORE baseline is never
    # recomputed against current-code gates -- so it must never carry a
    # current-code gate trace either, only the canonical AFTER result
    # does.
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]))

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    assert results[0].before.evidence.gate_trace == []
    assert results[0].before.evidence.resolved_must_have == []
    assert results[0].after.evidence.gate_trace != []


def test_review_export_evidence_survives_apply_unchanged(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]))

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    preview_evidence = results[0].after.evidence
    _authorize_pilot_identity(monkeypatch, jobs=1)
    applied_results = apply_rescore(engine, context, results)

    assert applied_results[0].after.evidence == preview_evidence

    rows, _counts = _build_human_review_rows(applied_results, sample_seed=1)
    assert rows[0]["gate_reason"] == ";".join(preview_evidence.gate_trace)


def test_actual_database_name_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(
        offline_rescore_stage12, "_actual_database_name", lambda db: "some_other_database"
    )
    engine = _engine()
    with Session(engine) as db:
        with pytest.raises(PilotIdentityMismatchError):
            verify_pilot_identity(db)


# --- H2 (Astra Stage 12 audit): CandidateProfile changed strictly BETWEEN --
# --- apply's own context read and its commit must not produce a stale -----
# --- commit -- not merely "changed before preview", which the pre-existing
# --- tests above already covered. --------------------------------------


def test_H2_candidate_profile_change_mid_apply_transaction_blocks_stale_commit(monkeypatch):
    """Reproduces Astra's exact H2 probe: preview captures a rich-skill
    CandidateProfile context; a SEPARATE session commits a profile change
    (skills replaced with something that would score very differently)
    strictly AFTER `apply_rescore` has already read its own context (and,
    with the H2 fix, locked the row) but BEFORE `apply_rescore` commits.
    Without the fix, apply would silently finish and persist a score
    computed against the now-stale in-memory context. With the fix, the
    interleaved write is detected (either by blocking behind the FOR
    UPDATE lock on a real PostgreSQL, or by the dialect-independent final
    recheck exercised here against SQLite) and the WHOLE transaction rolls
    back -- zero jobs changed.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        seeded = _seed_job(db, frozenset(["Python"]))
        job_id = seeded.id
        original = (seeded.score, seeded.recommendation, seeded.data_confidence)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1)

    interleaved = {"done": False}
    real_evaluate = offline_rescore_stage12._evaluate

    def _evaluate_then_interleave_profile_change(*args, **kwargs):
        if not interleaved["done"]:
            interleaved["done"] = True
            # A SEPARATE session/transaction, entirely independent of the
            # one apply_rescore is using -- exactly what Astra's probe
            # did: a concurrent write landing strictly inside apply's own
            # read-compute-commit window.
            with Session(engine) as concurrent_db:
                _set_candidate_skills(concurrent_db, ["Cobol"])
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(
        offline_rescore_stage12, "_evaluate", _evaluate_then_interleave_profile_change
    )

    with pytest.raises(RescoreConcurrentModificationError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        reloaded = db.get(JobRecord, job_id)
        assert (reloaded.score, reloaded.recommendation, reloaded.data_confidence) == original
        # The concurrent session's write itself is NOT rolled back --
        # only apply_rescore's own (aborted) transaction is. This proves
        # the interleave genuinely landed, not merely one that never ran.
    with Session(engine) as db:
        fresh_context, _ = preview_all_jobs(db)
    assert fresh_context.candidate_skills == frozenset({"Cobol"})


# --- M1 (Astra Stage 12 audit): pilot identity changed strictly BETWEEN ---
# --- apply's own in-transaction recheck and its commit ---------------------


def test_M1_automation_run_inserted_mid_apply_transaction_blocks_stale_commit(monkeypatch):
    """Reproduces Astra's M1 probe: a separate session inserts an
    AutomationRunRecord strictly AFTER `apply_rescore`'s own
    `verify_pilot_identity(db)` in-transaction recheck has already run,
    but BEFORE the transaction commits. Existing coverage
    (`test_pilot_automation_runs_drift_after_preview_blocks_apply`) only
    proves drift *before* that recheck is caught; this proves drift
    *after* it is caught too.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        seeded = _seed_job(db, frozenset(["Python"]), url="https://example.com/jobs/m1-1")
        job_id = seeded.id
        original = (seeded.score, seeded.recommendation, seeded.data_confidence)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1, automation_runs=0)

    interleaved = {"done": False}
    real_evaluate = offline_rescore_stage12._evaluate

    def _evaluate_then_interleave_automation_run(*args, **kwargs):
        if not interleaved["done"]:
            interleaved["done"] = True
            with Session(engine) as concurrent_db:
                concurrent_db.add(
                    AutomationRunRecord(
                        account_key="concurrent@example.com",
                        status="COMPLETED",
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                        results_json="{}",
                    )
                )
                concurrent_db.commit()
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(
        offline_rescore_stage12, "_evaluate", _evaluate_then_interleave_automation_run
    )

    with pytest.raises(PilotIdentityMismatchError):
        apply_rescore(engine, context, results)

    with Session(engine) as db:
        reloaded = db.get(JobRecord, job_id)
        assert (reloaded.score, reloaded.recommendation, reloaded.data_confidence) == original
        assert db.scalar(select(func.count()).select_from(AutomationRunRecord)) == 1, (
            "the concurrent session's own insert is not itself rolled back"
        )


# =========================================================================
# Astra Stage 12 remediation, ROUND 2 (H1 residual / M1 OPEN / M4 PARTIAL)
# =========================================================================


def _statement_log(engine) -> list[str]:
    """Captures every SQL statement the engine actually sends to the
    driver, in order -- the only way to assert what apply/preview really
    do at the database level (FOR UPDATE, LOCK TABLE, DML) rather than
    what their Python-level call graph suggests.
    """
    log: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        log.append(" ".join(statement.split()))

    return log


# --- H1 (round 2): unclassified/source skills must never become ----------
# --- mandatory evidence for a FRESHLY INGESTED record --------------------


def test_H1_freshly_ingested_source_skills_never_become_must_have_evidence():
    """Astra's round-2 H1 reproduction, end to end through the real
    persistence path and the real offline preview -- not a pure-scorer
    probe.

    A brand-new Backend Developer whose description merely MENTIONS
    Python and PostgreSQL extracts to `must_have_skills=[]`,
    `nice_to_have_skills=[]`, `skill_source="description_extracted"`,
    while the ingestion union keeps both technologies in `skills`. That
    shape is NOT evidence of pre-must/nice-split historical data -- the
    current extractor produces it every day -- and the removed
    `must = job.skills` fallback used to turn it into a fully-matched
    two-signal must-have set worth 90/APPLY.
    """
    engine = _engine()
    descriptive_only = (
        "Our platform uses Python and PostgreSQL to support customers around "
        "the world. We care about clean code and good documentation. " * 8
    )
    with Session(engine) as db:
        _set_candidate_skills(db, ["Python", "PostgreSQL"])
        seeded = _seed_job(
            db,
            frozenset(["Python", "PostgreSQL"]),
            title="Backend Developer",
            description=descriptive_only,
            skills=["Python", "PostgreSQL"],
            must_have_skills=[],
            nice_to_have_skills=[],
            skill_source="description_extracted",
        )
        job_id = seeded.id
        # Sanity: the fixture really is the shape under test.
        assert json.loads(seeded.must_have_skills_json) == []
        assert json.loads(seeded.nice_to_have_skills_json) == []
        assert seeded.skill_source == "description_extracted"
        assert set(json.loads(seeded.skills_json)) == {"Python", "PostgreSQL"}
        # Ingestion itself must not have produced a false APPLY.
        assert seeded.recommendation != "APPLY"

    with Session(engine) as db:
        _context, results = preview_all_jobs(db)

    assert len(results) == 1
    after = results[0].after
    assert after.id == job_id
    assert after.recommendation != "APPLY"
    assert after.matched_must_have == []
    assert after.missing_must_have == []
    # The source skills are still VISIBLE as descriptive evidence -- they
    # are simply not mandatory evidence any more.
    assert "python" in after.matched_skills
    assert after.evidence.matched_must_have == []
    assert after.evidence.resolved_must_have == []


def test_H1_explicit_must_have_still_reaches_apply_through_the_offline_path():
    """The legitimate counterpart of the test above, through the same
    persistence + preview path: removing the fallback must not suppress a
    posting whose requirements really were extracted.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, ["Python", "PostgreSQL"])
        _seed_job(
            db,
            frozenset(["Python", "PostgreSQL"]),
            title="Backend Developer",
            description=("You will build backend services with Python and PostgreSQL. " * 10),
            skills=["Python", "PostgreSQL"],
            must_have_skills=["Python", "PostgreSQL"],
            nice_to_have_skills=[],
            skill_source="description_extracted",
        )

    with Session(engine) as db:
        _context, results = preview_all_jobs(db)

    after = results[0].after
    assert after.recommendation == "APPLY"
    assert set(after.matched_must_have) == {"postgresql", "python"}
    assert set(after.evidence.matched_must_have) == {"postgresql", "python"}


# --- M1 (round 2): the pilot POPULATION is locked through commit ---------


class _FakePostgresSession:
    """Just enough of a `Session` for `lock_pilot_population` -- it only
    reads `db.bind.dialect.name` and calls `db.execute(text(...))`. Lets
    the exact PostgreSQL statements be asserted with no server, no
    driver, and no connection.
    """

    def __init__(self) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.executed: list[str] = []

    def execute(self, statement):
        self.executed.append(str(statement))


def test_M1_population_lock_statements_are_the_documented_postgresql_table_lock():
    assert population_lock_statements() == (
        "SET LOCAL lock_timeout = '15s'",
        "LOCK TABLE jobs, automation_runs IN SHARE ROW EXCLUSIVE MODE",
    )
    # The lock covers exactly the two tables the pilot-identity COUNT
    # predicate is defined over.
    assert POPULATION_LOCK_TABLES == ("jobs", "automation_runs")


def test_M1_lock_pilot_population_issues_those_statements_on_postgresql():
    db = _FakePostgresSession()
    assert lock_pilot_population(db) is True
    assert db.executed == list(population_lock_statements())


def test_M1_lock_pilot_population_is_a_documented_noop_on_sqlite():
    """SQLite has no LOCK TABLE. The helper reports False so a caller (and
    a reader of these tests) can never mistake a passing SQLite test for
    evidence of PostgreSQL blocking behavior -- see the module comment on
    POPULATION_LOCK_TABLES.
    """
    engine = _engine()
    with Session(engine) as db:
        assert lock_pilot_population(db) is False


def test_M1_apply_takes_the_population_lock_before_any_identity_observation(monkeypatch):
    """Protocol/ordering regression: the lock must be the FIRST thing
    apply's transaction does. A lock taken after the first count would
    leave exactly the window Astra reproduced.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]), url="https://example.com/jobs/m1-order-1")

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1)

    order: list[str] = []
    real_lock = offline_rescore_stage12.lock_pilot_population
    real_verify = offline_rescore_stage12.verify_pilot_identity
    real_load_context = offline_rescore_stage12.load_scoring_context

    def _spy_lock(db):
        order.append("lock_population")
        return real_lock(db)

    def _spy_verify(db):
        order.append("verify_pilot_identity")
        return real_verify(db)

    def _spy_load_context(db, **kwargs):
        order.append(f"load_scoring_context(for_update={kwargs.get('for_update', False)})")
        return real_load_context(db, **kwargs)

    monkeypatch.setattr(offline_rescore_stage12, "lock_pilot_population", _spy_lock)
    monkeypatch.setattr(offline_rescore_stage12, "verify_pilot_identity", _spy_verify)
    monkeypatch.setattr(offline_rescore_stage12, "load_scoring_context", _spy_load_context)

    apply_rescore(engine, context, results)

    assert order[0] == "lock_population", order
    assert order.count("lock_population") == 1, order
    assert order.count("verify_pilot_identity") == 2, order
    # Nothing observes the population before the lock is held.
    assert order.index("lock_population") < order.index("verify_pilot_identity")
    # The population count is the LAST observation before commit.
    assert order[-1] == "verify_pilot_identity", order


def test_M1_competing_population_writer_arriving_after_the_final_check_cannot_commit(monkeypatch):
    """The regression Astra explicitly requires: the competing mutation
    starts ONLY AFTER the FINAL successful `verify_pilot_identity` has
    returned, i.e. inside the final-check-to-commit window that a second
    or third COUNT cannot close.

    SQLite has no `LOCK TABLE`, so this test cannot (and does not claim
    to) demonstrate PostgreSQL blocking. What it demonstrates is the
    PROTOCOL that makes PostgreSQL's blocking apply to this exact window:
    the `jobs`/`automation_runs` population lock is acquired before the
    first identity observation and is STILL HELD when the competing
    writer arrives after the final one -- because a PostgreSQL table lock
    is released only by the transaction ending, and this transaction has
    not ended until `apply_rescore` returns. The competing writer is
    therefore routed through a model of PostgreSQL's SHARE ROW EXCLUSIVE
    conflict rule (its INSERT needs ROW EXCLUSIVE, which conflicts) and
    must be blocked until apply's transaction is over.

    STILL REQUIRES POSTGRESQL INTEGRATION VALIDATION: that PostgreSQL
    actually enforces that conflict. That is documented server behavior;
    what is asserted here is only that this code follows the protocol
    which makes it applicable.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        seeded = _seed_job(db, frozenset(["Cobol"]), url="https://example.com/jobs/m1-final-1")
        job_id = seeded.id
        original = (seeded.score, seeded.recommendation, seeded.data_confidence)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    # Non-vacuous: apply really is going to write a different score.
    assert (results[0].after.score, results[0].after.recommendation) != original[:2]

    _authorize_pilot_identity(monkeypatch, jobs=1, automation_runs=0)

    # Model of the PostgreSQL table lock: held from acquisition until the
    # transaction ends (which, for `apply_rescore`, is when it returns).
    lock = {"held": False}
    timeline: list[str] = []
    verify_calls = {"n": 0}
    competing = {"attempted": False, "blocked": None, "inserted": False}

    real_lock = offline_rescore_stage12.lock_pilot_population
    real_verify = offline_rescore_stage12.verify_pilot_identity

    def _tracked_lock(db):
        lock["held"] = True
        timeline.append("population_lock_acquired")
        return real_lock(db)

    def _competing_automation_run_insert():
        """A writer obeying the protocol: an INSERT into
        `automation_runs` needs ROW EXCLUSIVE, which conflicts with the
        SHARE ROW EXCLUSIVE lock apply holds.
        """
        competing["attempted"] = True
        if lock["held"]:
            competing["blocked"] = True
            timeline.append("competing_writer_blocked_by_population_lock")
            return
        competing["blocked"] = False
        timeline.append("competing_writer_committed")
        with Session(engine) as concurrent_db:
            concurrent_db.add(
                AutomationRunRecord(
                    account_key="concurrent@example.com",
                    status="COMPLETED",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    results_json="{}",
                )
            )
            concurrent_db.commit()
        competing["inserted"] = True

    def _verify_then_maybe_interleave(db):
        verify_calls["n"] += 1
        result = real_verify(db)  # only reached if the check PASSED
        timeline.append(f"verify_pilot_identity_returned_ok:{verify_calls['n']}")
        if verify_calls["n"] == 2:
            # STRICTLY after the FINAL successful identity check.
            _competing_automation_run_insert()
        return result

    monkeypatch.setattr(offline_rescore_stage12, "lock_pilot_population", _tracked_lock)
    monkeypatch.setattr(
        offline_rescore_stage12, "verify_pilot_identity", _verify_then_maybe_interleave
    )

    applied = apply_rescore(engine, context, results)
    lock["held"] = False  # apply's transaction has now ended
    timeline.append("apply_transaction_committed")

    # 1. The competing mutation really was attempted, and only after the
    #    FINAL identity check had already returned successfully.
    assert competing["attempted"], "the competing writer never ran -- test proves nothing"
    assert timeline.index("verify_pilot_identity_returned_ok:2") < timeline.index(
        "competing_writer_blocked_by_population_lock"
    )
    # 2. The lock was taken before the FIRST identity observation and was
    #    still held at the moment the competing writer arrived.
    assert timeline[0] == "population_lock_acquired"
    assert competing["blocked"] is True
    assert not competing["inserted"]
    # 3. The approved population identity survived through commit.
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AutomationRunRecord)) == 0
        assert db.scalar(select(func.count()).select_from(JobRecord)) == 1
        reloaded = db.get(JobRecord, job_id)
        assert (reloaded.score, reloaded.recommendation) == (
            applied[0].after.score,
            applied[0].after.recommendation,
        )
    # 4. The lock is released only by the transaction ending.
    assert timeline[-1] == "apply_transaction_committed"


def test_M1_no_lock_table_statement_is_emitted_on_sqlite(monkeypatch):
    """Makes the dialect gap explicit in the suite itself, so a future
    reader cannot mistake the SQLite M1 tests above for PostgreSQL
    evidence.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]), url="https://example.com/jobs/m1-sqlite-1")

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1)
    log = _statement_log(engine)
    apply_rescore(engine, context, results)

    assert log, "no SQL captured -- the spy is not wired up"
    assert not any("LOCK TABLE" in s.upper() for s in log)


# --- H2 regression (must not regress): preview never locks, apply does ---


def _postgresql_compiled_statement_log(monkeypatch) -> list[str]:
    """Every statement the code hands to the Session (`execute`, `scalar`
    and `scalars` all funnel through `Session._execute_internal`),
    compiled with the PostgreSQL dialect.

    Necessary because SQLite's dialect silently DROPS `FOR UPDATE` when
    rendering SQL, so the raw cursor log can never show it. Compiling the
    real ORM statement objects for PostgreSQL shows what the production
    dialect would actually send -- the same technique Astra used to verify
    the H2 lock.
    """
    seen: list[str] = []
    real_execute = Session._execute_internal

    def _spy(self, statement, *args, **kwargs):
        try:
            seen.append(str(statement.compile(dialect=postgresql.dialect())))
        except Exception:  # pragma: no cover - defensive, non-compilable stmt
            seen.append(str(statement))
        return real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "_execute_internal", _spy)
    return seen


def test_H2_regression_preview_issues_no_for_update_and_no_write(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]), url="https://example.com/jobs/h2-preview-1")

    raw_log = _statement_log(engine)
    compiled_log = _postgresql_compiled_statement_log(monkeypatch)
    with Session(engine) as db:
        preview_all_jobs(db)

    assert raw_log, "no SQL captured -- the spy is not wired up"
    for statement in raw_log:
        upper = statement.upper()
        assert not upper.startswith("INSERT"), statement
        assert not upper.startswith("UPDATE"), statement
        assert not upper.startswith("DELETE"), statement
        assert "LOCK TABLE" not in upper, statement

    assert compiled_log, "no ORM statement captured -- the spy is not wired up"
    for statement in compiled_log:
        assert "FOR UPDATE" not in statement.upper(), statement


def test_H2_regression_apply_locks_the_candidate_profile_row_for_update(monkeypatch):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Cobol"]), url="https://example.com/jobs/h2-apply-1")

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    _authorize_pilot_identity(monkeypatch, jobs=1)
    compiled_log = _postgresql_compiled_statement_log(monkeypatch)
    apply_rescore(engine, context, results)

    for_update = [s for s in compiled_log if "FOR UPDATE" in s.upper()]
    assert any("candidate_profiles" in s for s in for_update), for_update
    assert any("FROM jobs" in s for s in for_update), for_update


# --- M4 (round 2): pre-exclusion evidence survives every exclusion gate --


def _preview_single(engine):
    with Session(engine) as db:
        _context, results = preview_all_jobs(db)
    assert len(results) == 1
    return results[0]


def test_M4_seniority_exclusion_preserves_pre_exclusion_evidence():
    """Astra's exact M4 reproduction: a Senior Backend Developer with
    must=[python, postgresql] and nice=[REST API, rest] scored 90/APPLY
    and was then correctly excluded by the Stage 11A seniority gate. The
    export used to report resolved_must_have=[], matched_must_have=[],
    unique_evidence_count=1, LOW_CARDINALITY -- all false.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(
            db,
            ["Python", "PostgreSQL", "REST API"],
            target_roles=["Junior Python Developer"],
        )
        _seed_job(
            db,
            frozenset(["Python"]),
            title="Senior Backend Developer",
            description=("Build REST APIs with Python against PostgreSQL. " * 12),
            skills=["Python", "PostgreSQL", "REST API"],
            must_have_skills=["Python", "PostgreSQL"],
            nice_to_have_skills=["REST API", "rest"],
            url="https://example.com/jobs/m4-seniority-1",
        )

    result = _preview_single(engine)
    evidence = result.after.evidence

    # The FINAL decision is the exclusion -- unchanged behavior.
    assert result.after.recommendation == "SKIP"
    assert result.after.score == 0

    # The BASE decision and its evidence are fully preserved.
    assert evidence.base_recommendation == "APPLY"
    assert evidence.base_score is not None and evidence.base_score >= 80
    assert evidence.resolved_must_have == ["postgresql", "python"]
    assert evidence.matched_must_have == ["postgresql", "python"]
    assert evidence.missing_must_have == []
    assert evidence.unique_evidence_count == 3
    assert evidence.evidence_cardinality == "SUFFICIENT"
    assert evidence.seniority_classification == "SENIOR"

    # And which gate changed the outcome is explicit.
    assert "seniority_excluded:senior" in evidence.gate_trace
    assert f"base_score:{evidence.base_score}:APPLY" in evidence.gate_trace
    assert "final:0:SKIP" in evidence.gate_trace


def test_M4_posting_exclusion_preserves_pre_exclusion_evidence():
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, ["Python", "PostgreSQL"])
        _seed_job(
            db,
            frozenset(["Python"]),
            title="Backend Developer",
            description=("Build backend services with Python and PostgreSQL. " * 12),
            skills=["Python", "PostgreSQL"],
            must_have_skills=["Python", "PostgreSQL"],
            nice_to_have_skills=[],
            posting_type="SELBSTAENDIGKEIT",
            url="https://example.com/jobs/m4-posting-1",
        )

    result = _preview_single(engine)
    evidence = result.after.evidence

    assert result.after.recommendation == "SKIP"
    assert result.after.score == 0
    assert evidence.posting_classification.startswith("EXCLUDED:")
    assert evidence.gate_trace[0].startswith("posting_excluded:")
    # Evidence the scorer WOULD have seen is still exported, even though
    # the posting gate returned before the normal scoring path.
    assert evidence.matched_must_have == ["postgresql", "python"]
    assert evidence.resolved_must_have == ["postgresql", "python"]
    assert evidence.base_recommendation != ""
    assert evidence.unique_evidence_count == 2
    assert "final:0:SKIP" in evidence.gate_trace
    # Gates that never ran say so explicitly rather than being absent.
    assert "seniority_not_applicable:posting_excluded" in evidence.gate_trace
    assert "role_not_applicable:posting_excluded" in evidence.gate_trace


def test_M4_role_exclusion_preserves_pre_exclusion_evidence():
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(
            db,
            ["Python", "PostgreSQL"],
            target_roles=["Python Developer"],
        )
        _seed_job(
            db,
            frozenset(["Python"]),
            title="Python Systemadministrator",
            description=("Administer Python tooling and PostgreSQL servers. " * 12),
            skills=["Python", "PostgreSQL"],
            must_have_skills=["Python", "PostgreSQL"],
            nice_to_have_skills=[],
            url="https://example.com/jobs/m4-role-1",
        )

    result = _preview_single(engine)
    evidence = result.after.evidence

    assert result.after.recommendation == "SKIP"
    assert result.after.score == 0
    assert evidence.role_relevance == "IRRELEVANT"
    assert any(e.startswith("role_excluded:") for e in evidence.gate_trace), evidence.gate_trace
    assert evidence.base_recommendation in ("APPLY", "MAYBE")
    assert evidence.matched_must_have == ["postgresql", "python"]
    assert evidence.resolved_must_have == ["postgresql", "python"]
    assert evidence.unique_evidence_count == 2
    assert "cardinality_not_applicable:role_excluded" in evidence.gate_trace


def test_M4_matched_must_have_is_an_explicit_human_review_csv_column(tmp_path):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, RICH_SKILLS)
        _seed_job(db, frozenset(["Python"]), url="https://example.com/jobs/m4-csv-1")

    with Session(engine) as db:
        _context, results = preview_all_jobs(db)

    assert "matched_must_have" in HUMAN_REVIEW_COLUMNS
    assert "base_score" in HUMAN_REVIEW_COLUMNS
    assert "base_recommendation" in HUMAN_REVIEW_COLUMNS

    rows, _counts = _build_human_review_rows(results, sample_seed=1)
    path = tmp_path / "human_review.csv"
    _write_human_review_csv(path, rows)
    with path.open(newline="", encoding="utf-8") as f:
        csv_rows = list(csv.DictReader(f))

    assert len(csv_rows) == 1
    evidence = results[0].after.evidence
    assert evidence.matched_must_have, "fixture must produce non-empty matched must-have"
    assert csv_rows[0]["matched_must_have"] == ";".join(evidence.matched_must_have)
    # matched_skills is NOT a substitute: it is a strictly wider set here.
    assert csv_rows[0]["matched_skills"] != csv_rows[0]["matched_must_have"]
    assert csv_rows[0]["base_recommendation"] == evidence.base_recommendation
    assert csv_rows[0]["base_score"] == str(evidence.base_score)


def test_M4_matched_must_have_column_is_populated_for_an_excluded_row(tmp_path):
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(
            db,
            ["Python", "PostgreSQL", "REST API"],
            target_roles=["Junior Python Developer"],
        )
        _seed_job(
            db,
            frozenset(["Python"]),
            title="Senior Backend Developer",
            description=("Build REST APIs with Python against PostgreSQL. " * 12),
            skills=["Python", "PostgreSQL", "REST API"],
            must_have_skills=["Python", "PostgreSQL"],
            nice_to_have_skills=["REST API", "rest"],
            url="https://example.com/jobs/m4-csv-excluded-1",
        )

    with Session(engine) as db:
        _context, results = preview_all_jobs(db)

    rows, _counts = _build_human_review_rows(results, sample_seed=1)
    assert len(rows) == 1
    path = tmp_path / "human_review_excluded.csv"
    _write_human_review_csv(path, rows)
    with path.open(newline="", encoding="utf-8") as f:
        row = next(iter(csv.DictReader(f)))

    assert row["recommendation"] == "SKIP"
    assert row["score"] == "0"
    # The FINAL columns are legitimately empty for an excluded row...
    assert row["matched_skills"] == ""
    # ...but the pre-exclusion evidence columns are not.
    assert row["matched_must_have"] == "postgresql;python"
    assert row["resolved_must_have"] == "postgresql;python"
    assert row["base_recommendation"] == "APPLY"
    assert "seniority_excluded:senior" in row["gate_reason"]


def test_M4_rest_aliases_export_as_one_canonical_resolved_nice_signal():
    """Astra M4 point 3: `resolved_nice_to_have` was a raw
    `sorted(set(...))` of the stored strings, so "REST API" and "rest"
    both appeared even though the classifier correctly counts them as ONE
    signal. It must use the same canonical normalization scoring uses.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(db, ["Python", "PostgreSQL", "REST API"])
        _seed_job(
            db,
            frozenset(["Python"]),
            title="Backend Developer",
            description=("Build REST APIs with Python against PostgreSQL. " * 12),
            skills=["Python", "PostgreSQL", "REST API"],
            must_have_skills=["Python", "PostgreSQL"],
            nice_to_have_skills=["REST API", "rest-api", "REST"],
            url="https://example.com/jobs/m4-alias-1",
        )

    result = _preview_single(engine)
    evidence = result.after.evidence

    assert evidence.resolved_nice_to_have == ["rest"]
    # Consistent with the cardinality count the scorer itself used.
    assert evidence.unique_evidence_count == 3


def test_M4_trace_and_no_trace_produce_identical_job_scores():
    """The audit object must be strictly observational: passing a trace
    must never change the returned JobScore.
    """
    cases = [
        ("Senior Backend Developer", "ARBEIT", ["Python", "PostgreSQL"], ["REST API", "rest"]),
        ("Python Systemadministrator", "ARBEIT", ["Python", "PostgreSQL"], []),
        ("Programmierung mit Python", "SELBSTAENDIGKEIT", ["Python"], []),
        ("Junior Python Developer", "ARBEIT", ["Python", "PostgreSQL"], ["Docker"]),
        ("Backend Developer", "ARBEIT", [], []),
    ]
    for title, posting_type, must, nice in cases:
        job = Job(
            source="bundesagentur",
            title=title,
            company="Example GmbH",
            url="https://example.com/jobs/trace-eq",
            description=RICH_TECH_DESCRIPTION,
            posting_type=posting_type,
            skills=[*must, *nice, "Python"],
            must_have_skills=must,
            nice_to_have_skills=nice,
        )
        kwargs = {
            "candidate_skills": frozenset(RICH_SKILLS),
            "allowed_employment_types": frozenset(),
            "get_candidate_target_seniority": lambda: "JUNIOR",
            "get_candidate_target_domain": lambda: "SOFTWARE_DEVELOPMENT",
        }
        untraced = evaluate_job_score(job, posting_type, **kwargs)
        trace = EvaluationTrace()
        traced = evaluate_job_score(job, posting_type, trace=trace, **kwargs)
        assert untraced == traced, title
        assert trace.final_recommendation == untraced.recommendation, title
        assert trace.final_score == untraced.score, title
        assert trace.events, title


def test_M4_historical_before_keeps_persisted_values_and_gets_no_current_gate_trace():
    """M4 item 7 / the H1-P invariant: the BEFORE snapshot's
    score/recommendation/data_confidence stay exactly what Stage 12
    persisted, and BEFORE never receives a current-code evaluation trace
    -- even for a job the CURRENT gates exclude.
    """
    engine = _engine()
    with Session(engine) as db:
        _set_candidate_skills(
            db,
            ["Python", "PostgreSQL", "REST API"],
            target_roles=["Junior Python Developer"],
        )
        seeded = _seed_job(
            db,
            frozenset(["Python", "PostgreSQL", "REST API"]),
            title="Senior Backend Developer",
            description=("Build REST APIs with Python against PostgreSQL. " * 12),
            skills=["Python", "PostgreSQL", "REST API"],
            must_have_skills=["Python", "PostgreSQL"],
            nice_to_have_skills=["REST API"],
            url="https://example.com/jobs/m4-historical-1",
        )
        # Force a persisted historical result that the CURRENT gates would
        # never produce, exactly as a pre-Stage-11 pilot row would look.
        seeded.score = 91
        seeded.recommendation = "APPLY"
        seeded.data_confidence = 0.73
        db.commit()
        db.add(UserProfile(name="default", skills_json=json.dumps(RICH_SKILLS)))
        db.commit()

    result = _preview_single(engine)

    assert (result.before.score, result.before.recommendation, result.before.data_confidence) == (
        91,
        "APPLY",
        0.73,
    )
    assert result.before.evidence.gate_trace == []
    assert result.before.evidence.base_score is None
    assert result.before.evidence.base_recommendation == ""
    # The AFTER result is the one the current gates decide.
    assert result.after.recommendation == "SKIP"
    assert result.after.evidence.gate_trace != []
