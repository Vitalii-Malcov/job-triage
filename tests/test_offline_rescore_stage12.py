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

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

import scripts.offline_rescore_stage12 as offline_rescore_stage12
from app.agents.job_score_evaluator import evaluate_job_score
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
    apply_rescore,
    main,
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
    db: Session, skills: list[str], *, employment_types: list[str] | None = None
) -> None:
    current = get_or_create_candidate_profile(db)
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=current.profile_version,
            skills=[CandidateSkill(name=name) for name in skills],
            job_preferences=CandidateJobPreferences(employment_types=employment_types or []),
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


def test_actual_database_name_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(
        offline_rescore_stage12, "_actual_database_name", lambda db: "some_other_database"
    )
    engine = _engine()
    with Session(engine) as db:
        with pytest.raises(PilotIdentityMismatchError):
            verify_pilot_identity(db)
