"""Offline, in-place re-score of already-persisted `JobRecord` rows against
the canonical CandidateProfile skill projection (CSP-001), for validating a
historical pilot database (e.g. `stage12_pilot_r2`) after a scoring-path
change -- without running any collector, creating any `JobRecord`, writing
any `automation_runs` row, or sending any notification.

**Two strictly separated phases.**

PREVIEW (default, no `--apply`): pure calculation, ZERO database writes.
Uses `app.agents.job_score_evaluator.evaluate_job_score` directly -- the
same pure, DB-free decision primitive the production ingestion path
(`app.services.collector_runner._score_for_posting_type`) itself calls, so
there is exactly one implementation of the scoring/gate logic, never a
second copy living in this script. Preview never calls
`get_or_create_candidate_profile` (or anything that could create one) --
only `app.db.candidate_profile_repository.get_candidate_profile` (pure
read) and `get_candidate_skills_for_scoring` (also pure read, see its own
docstring). A missing `CandidateProfile` aborts loudly
(`MissingCandidateProfileError`) rather than proceeding with an
undefined/empty comparison basis. On PostgreSQL, preview additionally
issues `SET TRANSACTION READ ONLY` as the very first statement of its
transaction, so any write anywhere in the call chain -- known or
undiscovered -- fails at the database level too; this is a second layer,
not the primary safety mechanism (the primary mechanism is that nothing in
the preview call graph ever calls `db.commit()`, `db.flush()`, or any
`get_or_create_*` helper).

APPLY (`--apply`): a SINGLE atomic transaction. Computes the complete
rescore first (via preview, using an identical `ScoringContext`), then opens
one transaction, takes a PostgreSQL `SHARE ROW EXCLUSIVE` table lock on
`jobs`/`automation_runs` as that transaction's very first statement so the
approved pilot POPULATION cannot be inserted into or deleted from by any
other writer until this transaction ends (see `POPULATION_LOCK_TABLES`),
re-locks the exact target rows (`SELECT ... FOR UPDATE`),
re-verifies every row's identity AND every scoring-input field against the
snapshot preview captured, re-verifies the `CandidateProfile`-derived
context hasn't changed, recomputes each score with the SAME pure evaluator
and asserts it matches what preview already computed, and ONLY THEN writes
-- exclusively the `SCORE_FIELD_ALLOWLIST` columns (`score`,
`recommendation`, `data_confidence`; see that constant's own comment for
how it was derived from `JobRecord`). `last_seen_at`, `skills_json`,
`skill_source`, `must_have_skills_json`, `nice_to_have_skills_json`,
`description`, `posting_type`, and `job_reference_tokens` are never touched.
`app.services.collector_runner.score_and_persist` is NOT used for apply --
it is an ingestion persistence path with broader mutation/commit semantics
(per-job commits, `last_seen_at` bump, full-record rewrite, reference-token
sync) than this offline operation needs or is allowed to have. Any failure,
row-count mismatch, or detected drift anywhere in the loop raises before any
row is written; on exception, the `with db.begin():` block rolls back the
ENTIRE transaction -- there is no partial apply.

Usage (PREVIEW):
    python -m scripts.offline_rescore_stage12 \\
        --postgres-db stage12_pilot_r2 \\
        --export-dir /path/outside/repo

Usage (APPLY -- requires the full pilot-identity preflight to pass):
    python -m scripts.offline_rescore_stage12 \\
        --postgres-db stage12_pilot_r2 \\
        --export-dir /path/outside/repo \\
        --apply --confirm-database stage12_pilot_r2

The PostgreSQL password is read ONLY from the environment variable named by
--postgres-password-env (default POSTGRES_PASSWORD) -- never accepted as a
CLI argument and never printed.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import random
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.agents.job_score_evaluator import EvaluationTrace, evaluate_job_score
from app.agents.posting_classifier import POSTING_TYPE_REQUIRES_PREFERENCE, classify_posting
from app.agents.role_relevance_classifier import (
    classify_title_relevance,
    derive_candidate_target_domain,
)
from app.agents.seniority_classifier import (
    classify_title_seniority,
    derive_candidate_target_seniority,
)
from app.db.candidate_profile_repository import (
    count_candidate_profiles,
    get_candidate_profile,
    get_candidate_skills_for_scoring,
    to_candidate_profile_response,
)
from app.db.models import AutomationRunRecord, JobRecord, UserProfile
from app.models.job import Job, JobScore

# Stage 12-specific, fixed human-review policy -- not a CLI-configurable
# value. There is no other codified "high-score SKIP" threshold anywhere in
# this repository; this number is policy, supplied once, here.
HIGH_SCORE_SKIP_THRESHOLD = 60
DETERMINISTIC_SKIP_SAMPLE_SIZE = 20

# The ONLY database --apply may ever write to. A literal, non-configurable
# constant -- never derived from --postgres-db/--confirm-database, which
# are themselves required to equal this exact string (see
# `_apply_target_authorized`) before --apply is even allowed to build an
# engine. This is Stage-12-pilot-specific tooling, not a general-purpose
# rescore utility.
STAGE12_PILOT_DATABASE = "stage12_pilot_r2"

# The known-good shape of the Stage 12 pilot database, checked by
# `verify_pilot_identity`. Real --apply runs (scripts/offline_rescore_
# stage12.main) always check against these values exactly as defined here.
# Module-level (rather than inline literals) SOLELY so this repo's own unit
# tests can monkeypatch them to match a small in-memory SQLite fixture
# instead of fabricating a full 222-row pilot dataset per test -- the
# production code path never overrides them.
STAGE12_PILOT_JOB_COUNT = 222
STAGE12_PILOT_AUTOMATION_RUNS_COUNT = 2
STAGE12_PILOT_CANDIDATE_PROFILE_COUNT = 1

# The exact, and ONLY, JobRecord columns an offline rescore may write.
# Derived directly from app.models.job.JobScore's fields intersected with
# what app/db/models.py::JobRecord actually persists as a scoring OUTPUT
# (not an input): `matched_skills`/`missing_skills`/`matched_must_have`/
# `missing_must_have`/`matched_nice_to_have`/`is_duplicate` are JobScore
# fields that are NEVER persisted on JobRecord at all (ephemeral,
# request-scoped only) -- there is nothing to allowlist for them because
# there is no column to write. Everything else on JobRecord
# (skills_json/must_have_skills_json/nice_to_have_skills_json/description/
# posting_type/skill_source/last_seen_at/...) is scoring INPUT or vacancy
# metadata, never touched by apply.
SCORE_FIELD_ALLOWLIST = ("score", "recommendation", "data_confidence")

# M1 remediation (Astra Stage 12 audit, round 2) -- the tables whose ROW
# POPULATION the Stage 12 pilot-identity invariant is defined over. The
# invariant `verify_pilot_identity` checks is a set of COUNTS (222 jobs,
# 222 distinct fingerprints, 1 CandidateProfile, 2 automation runs), and a
# count predicate cannot be protected by row locks: `SELECT ... FOR UPDATE`
# takes ROW SHARE on `jobs` and locks the rows it actually returned, which
# says nothing about a row that does not exist yet. Astra's reproduction:
# the final `verify_pilot_identity` returns OK, another transaction INSERTs
# an automation run (or a brand-new job with a new fingerprint) and commits,
# and apply then commits scores computed for a population that no longer
# matches the one a human approved.
#
# The mechanism below is a PostgreSQL table-level lock, taken as the very
# first statement of apply's transaction and held by PostgreSQL until that
# transaction commits or rolls back:
#
#     LOCK TABLE jobs, automation_runs IN SHARE ROW EXCLUSIVE MODE
#
# SHARE ROW EXCLUSIVE conflicts with ROW EXCLUSIVE -- the mode EVERY
# INSERT/UPDATE/DELETE acquires -- and with itself, SHARE, EXCLUSIVE and
# ACCESS EXCLUSIVE. It does NOT conflict with ACCESS SHARE (plain SELECT)
# or ROW SHARE (SELECT ... FOR UPDATE). Consequences, which are exactly the
# required invariant:
#   * no other transaction can insert, delete or update ANY row of `jobs`
#     or `automation_runs` between the moment apply takes this lock and the
#     moment apply's transaction ends -- including rows that did not exist
#     when the lock was taken, because the lock is on the TABLE, not on
#     rows;
#   * concurrent READERS are unaffected (the API, dashboards, and a
#     concurrent preview keep working);
#   * two concurrent offline applies serialize against each other, because
#     the mode is self-conflicting;
#   * crucially, NO cooperation is required from any other writer. Unlike
#     an advisory lock or a manifest row, correctness does not depend on
#     every collector/scheduler/API writer remembering to participate --
#     which is why this was chosen over those options, and why no
#     production writer needed to be modified.
#
# Apply itself later UPDATEs `jobs` (ROW EXCLUSIVE) and re-selects rows FOR
# UPDATE (ROW SHARE); a transaction never conflicts with its own locks, and
# because the strongest lock is taken FIRST there is no lock-upgrade
# deadlock window. Ownership/scope: this lock belongs to the Stage 12
# offline rescore workflow only. It is deliberately NOT introduced anywhere
# in the normal application, whose writers are not part of the pilot
# workflow and must not be globally serialized.
#
# LIMITATION, stated explicitly: this is a PostgreSQL mechanism. SQLite
# (every unit test here) has no `LOCK TABLE`, so `lock_pilot_population` is
# a documented no-op there and the SQLite tests can only verify the
# PROTOCOL (that the lock is requested, with the right SQL, before any
# identity check, and is never released before commit) -- not PostgreSQL's
# blocking behavior itself. See the M1 tests in
# tests/test_offline_rescore_stage12.py.
POPULATION_LOCK_TABLES = ("jobs", "automation_runs")
POPULATION_LOCK_MODE = "SHARE ROW EXCLUSIVE"
# Bounded wait: if some other transaction is already holding a conflicting
# lock, apply fails loudly (and rolls back, writing nothing) instead of
# hanging indefinitely against a production database.
POPULATION_LOCK_TIMEOUT = "15s"

CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")

SENTINEL_TITLES = (
    "AI Engineer",
    "KI-Entwickler",
    "Python Entwickler",
    "Backend Developer",
    "Junior Cyber Security Developer",
)

HUMAN_REVIEW_COLUMNS = [
    "job_id",
    "fingerprint",
    "title",
    "company",
    "location",
    "posting_type",
    "score",
    "recommendation",
    "matched_skills",
    "missing_must_have",
    # Stage 12 review-export remediation (Astra M4 finding): a reviewer
    # could not reconstruct WHY a score/recommendation happened from the
    # original export -- all `gate_reason` cells were empty, and the
    # resolved must/nice lists, cardinality, and title classifications
    # were absent entirely. Round 2 added the PRE-EXCLUSION base result
    # (`base_score`/`base_recommendation`) and the explicit
    # `matched_must_have` column: for an excluded job the final JobScore
    # is a zeroed `excluded_job_score()`, so the final columns above
    # cannot show what the scorer saw -- these do. All of them are purely
    # EXPLANATORY, read out of the `EvaluationTrace` the evaluator
    # recorded WHILE deciding; none can change a score or recommendation.
    "base_score",
    "base_recommendation",
    "resolved_must_have",
    "resolved_nice_to_have",
    # Explicitly exported (Astra M4 point 2): `matched_skills` above is
    # NOT a substitute -- it also contains optional and unclassified
    # source skills, and it is empty for every excluded job.
    "matched_must_have",
    "matched_optional",
    "unique_evidence_count",
    "evidence_cardinality",
    "role_relevance",
    "seniority_classification",
    "posting_classification",
    "data_confidence",
    "gate_reason",
    "description_or_url",
    "human_relevant",
    "human_decision",
    "human_notes",
]


class MissingCandidateProfileError(RuntimeError):
    """No canonical CandidateProfile exists. Offline rescore aborts rather
    than proceeding with an undefined comparison basis -- and, critically,
    never creates one and never falls back to the legacy UserProfile or any
    invented default skill set as a substitute.
    """


class CandidateProfileIntegrityError(RuntimeError):
    """More than one candidate_profiles row was found. Should be
    structurally impossible (the schema's own CHECK constraint forbids
    it) -- this is a defensive, should-never-fire abort, not a normal
    error path.
    """


class RescoreIntegrityError(RuntimeError):
    """The set of rows locked at apply time does not match the set of rows
    preview computed a result for (a row disappeared, or somehow a new one
    was created) -- aborts before any write; the enclosing transaction
    rolls back everything.
    """


class RescoreConcurrentModificationError(RuntimeError):
    """A row's identity/input snapshot at apply time no longer matches what
    preview observed, or the CandidateProfile-derived context changed
    between preview and apply -- state drifted and apply must not overwrite
    unknown state. Aborts before any write; the enclosing transaction rolls
    back everything.
    """


class PilotIdentityMismatchError(RuntimeError):
    """The database's actual identity (name/job count/fingerprint count/
    CandidateProfile count/automation run count) does not match what
    --apply requires -- refuses to write against a database that hasn't
    been positively confirmed to be the intended target.
    """


@dataclass(frozen=True)
class ScoringContext:
    candidate_skills: frozenset[str]
    target_seniority: str
    target_domain: str
    employment_types: frozenset[str]


@dataclass(frozen=True)
class ReviewEvidence:
    """Stage 12 review-export remediation (Astra M4 finding): the
    structured facts a human reviewer needs to reconstruct WHY a job
    received its score/recommendation, WITHOUT re-deciding anything.

    Round 2: every evidence field here is now read out of the
    `app.agents.job_score_evaluator.EvaluationTrace` the evaluator
    recorded WHILE deciding -- NOT reconstructed afterwards from the
    returned `JobScore`. Astra's M4 point 1: for a posting/seniority/role
    exclusion the evaluator returns `excluded_job_score()`, whose match
    lists are all empty, so a post-hoc reconstruction reported
    `resolved_must_have=[] matched_must_have=[] unique_evidence_count=1
    LOW_CARDINALITY` for a job that had actually scored 90/APPLY on two
    explicit must-have matches. `base_*` below is the pre-exclusion
    result; `final_*` on the surrounding `JobScore`/`JobSnapshot` is the
    post-gate outcome; `gate_trace` says which gate moved one to the
    other. See `_compute_review_evidence`.
    """

    base_score: int | None
    base_recommendation: str
    resolved_must_have: list[str]
    resolved_nice_to_have: list[str]
    matched_must_have: list[str]
    missing_must_have: list[str]
    matched_optional: list[str]
    unique_evidence_count: int
    evidence_cardinality: str
    role_relevance: str
    seniority_classification: str
    posting_classification: str
    gate_trace: list[str]


# The zero-evidence sentinel used for "before" snapshots (see the H1/P
# "legacy historical explanation behavior remains separate from new
# scoring" invariant -- the historical baseline is never recomputed
# against current-code gates, so it never gets a current-code evidence
# trace either) and for any synthetic `JobSnapshot` a test constructs
# directly without going through `preview_all_jobs`.
EMPTY_REVIEW_EVIDENCE = ReviewEvidence(
    base_score=None,
    base_recommendation="",
    resolved_must_have=[],
    resolved_nice_to_have=[],
    matched_must_have=[],
    missing_must_have=[],
    matched_optional=[],
    unique_evidence_count=0,
    evidence_cardinality="",
    role_relevance="",
    seniority_classification="",
    posting_classification="",
    gate_trace=[],
)


@dataclass
class JobSnapshot:
    id: int
    fingerprint: str
    source: str
    title: str
    company: str
    location: str
    url: str
    posting_type: str | None
    description: str
    skills: list[str]
    must_have_skills: list[str]
    nice_to_have_skills: list[str]
    skill_source: str | None
    score: int
    recommendation: str
    data_confidence: float
    matched_skills: list[str]
    missing_skills: list[str]
    matched_must_have: list[str]
    missing_must_have: list[str]
    first_seen_at: datetime
    last_seen_at: datetime
    # RAW values actually persisted on JobRecord at the moment this
    # snapshot was captured -- NEVER the reconstructed legacy-profile
    # "before" score, and NEVER the canonical preview "after" score. Those
    # two live in `score`/`recommendation`/`data_confidence` above (whose
    # meaning depends on which snapshot this is -- before/after/apply-
    # reread); these three always mean exactly "what does the database
    # currently say", so drift in them is detectable regardless of which
    # narrative the surrounding snapshot represents.
    persisted_score: int = 0
    persisted_recommendation: str = ""
    persisted_data_confidence: float = 0.0
    matched_skills_source: str = "recomputed"
    # Explanatory-only (Stage 12 review export) -- never compared by
    # `input_snapshot()` below, never influences a decision. Defaults to
    # the zero-evidence sentinel for "before" snapshots and any snapshot
    # built outside `preview_all_jobs`/`apply_rescore`.
    evidence: ReviewEvidence = EMPTY_REVIEW_EVIDENCE

    def input_snapshot(self) -> tuple:
        """Every scoring-INPUT field this job's re-evaluation depends on,
        plus identity/history/RAW-persisted-output fields that must never
        move -- compared verbatim between preview and apply to detect any
        drift (concurrent edit, concurrent re-score, concurrent ingestion
        bumping `last_seen_at`, etc).
        """
        return (
            self.fingerprint,
            self.first_seen_at,
            self.last_seen_at,
            self.source,
            self.title,
            self.company,
            self.location,
            self.url,
            self.posting_type,
            self.description,
            tuple(self.skills),
            tuple(self.must_have_skills),
            tuple(self.nice_to_have_skills),
            self.skill_source,
            self.persisted_score,
            self.persisted_recommendation,
            self.persisted_data_confidence,
        )


@dataclass
class RescoreResult:
    before: JobSnapshot
    after: JobSnapshot
    applied: bool


def _csv_safe(value: str) -> str:
    """Neutralizes a spreadsheet formula-injection prefix (Codex finding:
    vacancy title/company/description content is external/untrusted, and
    every text export here ends up opened in a spreadsheet by a human
    reviewer). Prefixing with a single quote is the standard mitigation --
    Excel/Sheets/LibreOffice all render the value as literal text instead
    of evaluating it as a formula. Only applied to CSV text fields, never
    to a JSON/raw evidence export.
    """
    if value and value[0] in CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


def _job_from_record(record: JobRecord) -> Job:
    """Reconstructs the `Job` the pure evaluator needs from a record's own
    CURRENTLY-persisted fields -- never invents or edits job content.
    """
    return Job(
        source=record.source,
        title=record.title,
        company=record.company,
        location=record.location,
        url=record.url,
        description=record.description,
        skills=json.loads(record.skills_json),
        must_have_skills=json.loads(record.must_have_skills_json),
        nice_to_have_skills=json.loads(record.nice_to_have_skills_json),
        skill_source=record.skill_source,
        posting_type=record.posting_type,
    )


def load_scoring_context(db: Session, *, for_update: bool = False) -> ScoringContext:
    """Read-only. Requires an existing canonical CandidateProfile --
    NEVER creates one, NEVER falls back to UserProfile or any invented
    default skill set. Raises `MissingCandidateProfileError` /
    `CandidateProfileIntegrityError` rather than degrading silently.

    `for_update=True` (Stage 12 H2 remediation): locks the singleton
    CandidateProfileRecord row for the remainder of the caller's
    transaction -- see `app.db.candidate_profile_repository.
    get_candidate_profile`'s own docstring. Only `apply_rescore` passes
    this; `preview_all_jobs` never does (its transaction is `SET
    TRANSACTION READ ONLY`, which rejects `FOR UPDATE`).
    """
    profile_count = count_candidate_profiles(db)
    if profile_count > 1:
        raise CandidateProfileIntegrityError(
            f"expected at most 1 candidate_profiles row, found {profile_count}"
        )
    profile_record = get_candidate_profile(db, for_update=for_update)
    if profile_record is None:
        raise MissingCandidateProfileError(
            "no CandidateProfile exists -- offline rescore refuses to proceed "
            "without one and will never create one"
        )
    profile = to_candidate_profile_response(profile_record)
    candidate_skills = get_candidate_skills_for_scoring(db)
    return ScoringContext(
        candidate_skills=frozenset(candidate_skills),
        target_seniority=derive_candidate_target_seniority(profile.target_roles),
        target_domain=derive_candidate_target_domain(profile.target_roles),
        employment_types=frozenset(profile.job_preferences.employment_types),
    )


def _allowed_employment_types_for(
    context: ScoringContext, posting_type: str | None
) -> frozenset[str]:
    if posting_type not in POSTING_TYPE_REQUIRES_PREFERENCE:
        return frozenset()
    return context.employment_types


def _evaluate(
    context: ScoringContext,
    job: Job,
    posting_type: str | None,
    *,
    trace: EvaluationTrace | None = None,
) -> JobScore:
    return evaluate_job_score(
        job,
        posting_type,
        candidate_skills=context.candidate_skills,
        allowed_employment_types=_allowed_employment_types_for(context, posting_type),
        get_candidate_target_seniority=lambda: context.target_seniority,
        get_candidate_target_domain=lambda: context.target_domain,
        trace=trace,
    )


def _compute_review_evidence(
    context: ScoringContext,
    job: Job,
    posting_type: str | None,
    trace: EvaluationTrace,
) -> ReviewEvidence:
    """Explanatory-only (Stage 12 review-export remediation, Astra M4
    finding): projects the `EvaluationTrace` the evaluator already
    recorded into the export shape -- it never re-decides, re-scores, or
    reconstructs anything from the (possibly zeroed) final `JobScore`.

    Round 2 (Astra M4 point 1): the evidence lists come from
    `trace.resolved_must_have`/`matched_must_have`/... , which the
    evaluator captured from the BASE `JobScorer` result BEFORE any
    posting/seniority/role gate could replace it with
    `excluded_job_score()`. The three classification fields prefer what
    the gates actually observed (`trace.*_classification`) and fall back
    to an independently pure classifier call only for a gate that never
    ran -- so a reviewer sees the full picture either way, and the
    displayed value is never in conflict with the gate that used it.
    """
    posting_classification = trace.posting_classification
    if not posting_classification:
        posting = classify_posting(
            title=job.title,
            posting_type=posting_type,
            allowed_employment_types=_allowed_employment_types_for(context, posting_type),
        )
        posting_classification = (
            "TARGET_EMPLOYMENT"
            if posting.is_target_employment
            else f"EXCLUDED:{posting.excluded_reason}"
        )
    return ReviewEvidence(
        base_score=trace.base_score,
        base_recommendation=trace.base_recommendation or "",
        resolved_must_have=list(trace.resolved_must_have),
        resolved_nice_to_have=list(trace.resolved_nice_to_have),
        matched_must_have=list(trace.matched_must_have),
        missing_must_have=list(trace.missing_must_have),
        matched_optional=list(trace.matched_optional),
        unique_evidence_count=trace.unique_evidence_count,
        evidence_cardinality=trace.evidence_cardinality,
        role_relevance=trace.role_relevance or classify_title_relevance(job.title).level,
        seniority_classification=(
            trace.seniority_classification or classify_title_seniority(job.title).level
        ),
        posting_classification=posting_classification,
        gate_trace=list(trace.events),
    )


def _legacy_candidate_skills(db: Session) -> frozenset[str] | None:
    """Best-effort, READ-ONLY recovery of the skill set the ORIGINAL
    (pre-CSP-001) Stage 12 pilot run actually scored against, for
    reconstructing "before" snapshot EVIDENCE only (matched/missing skill
    lists) -- never for the before snapshot's score/recommendation/
    data_confidence, which are always the actual persisted historical
    values (`record.score`/`record.recommendation`/
    `record.data_confidence`), not a recomputation. Never
    `get_or_create_default_profile` (would INSERT a row). Returns None if
    no legacy profile is persisted -- callers must treat that as "not
    recoverable", never synthesize a fallback. This value NEVER influences
    the applied/new result.
    """
    profile = db.scalar(select(UserProfile).where(UserProfile.name == "default"))
    if profile is None:
        return None
    return frozenset(json.loads(profile.skills_json))


def _snapshot(
    record: JobRecord,
    score: JobScore,
    *,
    matched_skills_source: str,
    evidence: ReviewEvidence = EMPTY_REVIEW_EVIDENCE,
) -> JobSnapshot:
    return JobSnapshot(
        id=record.id,
        fingerprint=record.fingerprint,
        source=record.source,
        title=record.title,
        company=record.company,
        location=record.location,
        url=record.url,
        posting_type=record.posting_type,
        description=record.description,
        skills=json.loads(record.skills_json),
        must_have_skills=json.loads(record.must_have_skills_json),
        nice_to_have_skills=json.loads(record.nice_to_have_skills_json),
        skill_source=record.skill_source,
        score=score.score,
        recommendation=score.recommendation,
        data_confidence=score.data_confidence,
        matched_skills=score.matched_skills,
        missing_skills=score.missing_skills,
        matched_must_have=score.matched_must_have,
        missing_must_have=score.missing_must_have,
        first_seen_at=record.first_seen_at,
        last_seen_at=record.last_seen_at,
        # RAW persisted values, straight off the record -- deliberately
        # independent of whichever `score` (legacy-recomputed/canonical-
        # preview/apply-reread) this snapshot otherwise represents.
        persisted_score=record.score,
        persisted_recommendation=record.recommendation,
        persisted_data_confidence=record.data_confidence,
        matched_skills_source=matched_skills_source,
        evidence=evidence,
    )


def preview_all_jobs(db: Session) -> tuple[ScoringContext, list[RescoreResult]]:
    """PURE CALCULATION ONLY. Zero database writes: never calls
    `db.commit()`, never calls `db.flush()`, never calls
    `get_or_create_candidate_profile` (directly or transitively -- see
    `load_scoring_context`'s own docstring). On PostgreSQL, additionally
    issues `SET TRANSACTION READ ONLY` as the very first statement so any
    write anywhere in the call chain fails at the database level as a
    second, independent layer of protection.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION READ ONLY"))

    context = load_scoring_context(db)
    legacy_skills = _legacy_candidate_skills(db)

    records = list(db.scalars(select(JobRecord).order_by(JobRecord.id.asc())))
    results: list[RescoreResult] = []
    for record in records:
        job = _job_from_record(record)

        # The BEFORE score/recommendation/data_confidence are ALWAYS the
        # authoritative historical values Stage 12 actually persisted
        # (`record.score`/`record.recommendation`/`record.data_confidence`)
        # -- never a current-code recomputation, even when a legacy
        # UserProfile is available. Recomputing "before" against
        # current-code gate/threshold logic (Stage 11A/11B/11E/11C did not
        # exist when the historical scores were produced) would silently
        # replace the actual historical baseline with a hybrid "old skills,
        # new gates" result, corrupting the before/after comparison this
        # script exists to produce. Legacy skills, when recoverable, are
        # used ONLY to reconstruct explanatory evidence (matched/missing
        # skill lists) for that historical score -- never its outcome.
        if legacy_skills is not None:
            legacy_evidence = evaluate_job_score(
                job,
                record.posting_type,
                candidate_skills=legacy_skills,
                allowed_employment_types=_allowed_employment_types_for(
                    context, record.posting_type
                ),
                get_candidate_target_seniority=lambda: context.target_seniority,
                get_candidate_target_domain=lambda: context.target_domain,
            )
            before_score = JobScore(
                score=record.score,
                recommendation=record.recommendation,
                data_confidence=record.data_confidence,
                matched_skills=legacy_evidence.matched_skills,
                missing_skills=legacy_evidence.missing_skills,
                matched_must_have=legacy_evidence.matched_must_have,
                missing_must_have=legacy_evidence.missing_must_have,
                matched_nice_to_have=legacy_evidence.matched_nice_to_have,
            )
            before_source = "recomputed_legacy_profile_evidence"
        else:
            before_score = JobScore(
                score=record.score,
                recommendation=record.recommendation,
                data_confidence=record.data_confidence,
            )
            before_source = "persisted_score_only"
        before = _snapshot(record, before_score, matched_skills_source=before_source)

        gate_trace = EvaluationTrace()
        after_score = _evaluate(context, job, record.posting_type, trace=gate_trace)
        after_evidence = _compute_review_evidence(context, job, record.posting_type, gate_trace)
        after = _snapshot(
            record,
            after_score,
            matched_skills_source="preview_no_write",
            evidence=after_evidence,
        )

        results.append(RescoreResult(before=before, after=after, applied=False))

    assert not db.new, "preview staged new rows -- this must never happen"
    assert not db.dirty, "preview staged dirty rows -- this must never happen"
    assert not db.deleted, "preview staged deleted rows -- this must never happen"
    return context, results


def apply_rescore(
    engine, preview_context: ScoringContext, preview_results: list[RescoreResult]
) -> list[RescoreResult]:
    """SINGLE ATOMIC TRANSACTION. Takes the `jobs`/`automation_runs`
    population lock as its FIRST statement (M1 -- see
    `POPULATION_LOCK_TABLES`), re-locks the exact target rows and the
    singleton CandidateProfile row, re-verifies every row's identity/input
    snapshot AND the CandidateProfile-derived context against what preview
    captured, recomputes each score with the SAME pure evaluator and
    asserts it matches preview's own result, and ONLY THEN writes --
    exclusively `SCORE_FIELD_ALLOWLIST` columns. Any mismatch/exception
    aborts before any write; `with db.begin():` rolls back the ENTIRE
    transaction on exception, so there is no partial apply.
    """
    expected_by_id = {r.before.id: r for r in preview_results}
    job_ids = sorted(expected_by_id)
    applied_results: list[RescoreResult] = []

    with Session(engine) as db, db.begin():
        # M1 remediation (Astra Stage 12 audit, round 2): FIRST statement of
        # the transaction -- takes the table-level population lock on
        # `jobs`/`automation_runs` that every INSERT/UPDATE/DELETE against
        # those tables must block on until this transaction commits or rolls
        # back. Without it, the pilot-identity COUNTS below are only ever a
        # point-in-time observation: Astra reproduced a competing
        # automation-run insert landing strictly AFTER the final
        # `verify_pilot_identity` returned and before commit, which no
        # additional count, and no row-level lock on already-existing rows,
        # can prevent. See POPULATION_LOCK_TABLES for the lock semantics,
        # its ownership/scope, and the PostgreSQL-only limitation.
        lock_pilot_population(db)

        # H2 remediation (Astra Stage 12 audit): `for_update=True` locks the
        # singleton CandidateProfileRecord row for the rest of this
        # transaction -- a concurrent `apply_candidate_profile_patch` CAS
        # against that same row now blocks until this transaction commits
        # or rolls back, instead of silently landing in the gap between
        # this read and the eventual commit (Astra's reproduced race:
        # "apply captures context, another session changes the profile,
        # apply still commits the stale score"). See the final recheck
        # below for the second, dialect-independent half of this fix.
        apply_context = load_scoring_context(db, for_update=True)
        if apply_context != preview_context:
            raise RescoreConcurrentModificationError(
                "CandidateProfile-derived scoring context changed since preview -- "
                "aborting, transaction will roll back, ZERO jobs changed"
            )

        rows = list(
            db.scalars(
                select(JobRecord)
                .where(JobRecord.id.in_(job_ids))
                .order_by(JobRecord.id.asc())
                .with_for_update()
            )
        )
        if len(rows) != len(job_ids):
            raise RescoreIntegrityError(
                f"expected {len(job_ids)} rows, locked {len(rows)} -- row set changed "
                "since preview, aborting, transaction will roll back, ZERO jobs changed"
            )

        # In-transaction TOCTOU recheck (Codex finding): the preflight
        # call in main() runs BEFORE preview, so a database-level identity
        # change between preflight and this transaction (another job
        # ingested/deleted, an automation run started/finished, the
        # CandidateProfile row itself deleted) would otherwise go
        # undetected. Same helper as the preflight call -- never a second,
        # divergent identity check. Runs after row locks are acquired and
        # the row-set-size is verified, but strictly before any score
        # field below is assigned.
        verify_pilot_identity(db)

        for record in rows:
            expected = expected_by_id[record.id]
            current_snapshot = _snapshot(
                record,
                JobScore(
                    score=record.score,
                    recommendation=record.recommendation,
                    data_confidence=record.data_confidence,
                ),
                matched_skills_source="apply_reread",
            )
            if current_snapshot.input_snapshot() != expected.before.input_snapshot():
                raise RescoreConcurrentModificationError(
                    f"job id={record.id} drifted since preview -- aborting, transaction "
                    "will roll back, ZERO jobs changed"
                )

            job = _job_from_record(record)
            after_score = _evaluate(apply_context, job, record.posting_type)
            if (
                after_score.score,
                after_score.recommendation,
                after_score.data_confidence,
            ) != (
                expected.after.score,
                expected.after.recommendation,
                expected.after.data_confidence,
            ):
                raise RescoreConcurrentModificationError(
                    f"job id={record.id}: apply-time recomputation does not match the "
                    "result preview computed -- aborting, transaction will roll back, "
                    "ZERO jobs changed"
                )

            # The ONLY writes this function ever performs -- exactly
            # SCORE_FIELD_ALLOWLIST, nothing else.
            record.score = after_score.score
            record.recommendation = after_score.recommendation
            record.data_confidence = after_score.data_confidence

            applied_results.append(
                RescoreResult(
                    before=expected.before,
                    after=_snapshot(
                        record,
                        after_score,
                        matched_skills_source="applied",
                        # The review evidence is a pure, deterministic
                        # function of (job, context, trace) -- already
                        # computed once during preview and, immediately
                        # above, already verified to reproduce the exact
                        # same score/recommendation/data_confidence.
                        # Recomputing it here would be redundant work with
                        # zero additional safety value; carry preview's
                        # already-verified evidence forward instead.
                        evidence=expected.after.evidence,
                    ),
                    applied=True,
                )
            )

        # H2/M1 remediation (Astra Stage 12 audit): a final recheck,
        # immediately before the implicit commit below, of the SAME two
        # checks already run earlier in this transaction. This is the
        # SECOND, dialect-independent layer, not the primary defense:
        #   * pilot identity (population counts) is primarily protected by
        #     the SHARE ROW EXCLUSIVE table lock taken as this
        #     transaction's first statement -- on PostgreSQL no competing
        #     insert/delete can land in this window at all, whether before
        #     or after this final count;
        #   * the CandidateProfile-derived context is primarily protected
        #     by the `FOR UPDATE` row lock on the singleton profile row.
        # Both rechecks still run because they cost one query each, they
        # catch anything a future refactor might let slip past the locks,
        # and they are the only protection available on a dialect where
        # the locks are no-ops (SQLite, in these tests). Ordered
        # context-then-identity so the population count is the very last
        # observation before commit. Any mismatch raises, and
        # `with db.begin():` rolls back the ENTIRE transaction -- no stale
        # score is ever persisted.
        final_context = load_scoring_context(db, for_update=True)
        if final_context != apply_context:
            raise RescoreConcurrentModificationError(
                "CandidateProfile-derived scoring context changed during apply -- "
                "aborting, transaction will roll back, ZERO jobs changed"
            )
        verify_pilot_identity(db)
        # Single commit on successful context-manager exit. Any exception
        # above rolls back everything via `with db.begin():`.

    return applied_results


def population_lock_statements() -> tuple[str, ...]:
    """The exact SQL `lock_pilot_population` issues on PostgreSQL, as a
    separate pure function so a test can assert the statements without a
    live PostgreSQL server (and so the statements exist in exactly one
    place). See `POPULATION_LOCK_TABLES`' own comment for why this
    specific lock mode.
    """
    return (
        f"SET LOCAL lock_timeout = '{POPULATION_LOCK_TIMEOUT}'",
        f"LOCK TABLE {', '.join(POPULATION_LOCK_TABLES)} IN {POPULATION_LOCK_MODE} MODE",
    )


def lock_pilot_population(db: Session) -> bool:
    """M1 remediation: takes the through-commit population lock described
    at `POPULATION_LOCK_TABLES`. MUST be the first statement of apply's
    transaction, before `load_scoring_context`, before any row lock, and
    before any `verify_pilot_identity` call -- so that every subsequent
    identity observation is made under a population that no other writer
    can change until this transaction ends.

    Returns True if the real lock was taken, False on a dialect that has
    no `LOCK TABLE` (SQLite, used by every unit test here) -- callers must
    treat False as "the population invariant is NOT protected by the
    database on this dialect". Never called by preview (whose transaction
    is `SET TRANSACTION READ ONLY` and which writes nothing).
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return False
    for statement in population_lock_statements():
        db.execute(text(statement))
    return True


def _actual_database_name(db: Session) -> str | None:
    """The server's own answer to "what database am I connected to",
    queried directly rather than trusted from a CLI argument -- `None` on
    a dialect that has no such concept (SQLite, used by every unit test
    here) so callers can treat "not checkable on this dialect" and "not
    checked yet" identically. Split out from `verify_pilot_identity` so
    tests can monkeypatch exactly this one seam to simulate a PostgreSQL
    identity mismatch without a real PostgreSQL server.
    """
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return db.execute(text("SELECT current_database()")).scalar()
    return None


def _apply_target_authorized(postgres_db: str, confirm_database: str) -> bool:
    """True only if BOTH `--postgres-db` and `--confirm-database` equal
    the hard-pinned `STAGE12_PILOT_DATABASE` literal exactly -- never just
    equal to each other. `--postgres-db foo --confirm-database foo` must
    fail even though the two CLI arguments agree with one another, because
    neither one is the pilot database.
    """
    return postgres_db == STAGE12_PILOT_DATABASE and confirm_database == STAGE12_PILOT_DATABASE


def verify_pilot_identity(db: Session) -> None:
    """MANDATORY for --apply only, called three times via this one shared
    helper (never a second, divergent implementation of "is this really
    the Stage 12 pilot"): once as a preflight before preview, once inside
    the apply transaction after the population lock and row locks are
    acquired and before any score field is assigned, and once as the last
    observation before commit. On PostgreSQL every in-transaction call is
    made while the `jobs`/`automation_runs` population lock is held, so
    the counts it observes cannot change before commit. Refuses to write
    unless the database's actual
    identity matches the Stage 12 pilot exactly, hard-pinned to
    `STAGE12_PILOT_DATABASE` (never a caller-supplied value). Preview may
    report a mismatch but always remains zero-write regardless (this
    function is never called from preview_all_jobs).
    """
    actual_database = _actual_database_name(db)
    if actual_database is not None and actual_database != STAGE12_PILOT_DATABASE:
        raise PilotIdentityMismatchError(
            f"connected to database {actual_database!r}, but apply requires "
            f"{STAGE12_PILOT_DATABASE!r}"
        )

    jobs_count = db.scalar(select(func.count()).select_from(JobRecord))
    if jobs_count != STAGE12_PILOT_JOB_COUNT:
        raise PilotIdentityMismatchError(
            f"expected {STAGE12_PILOT_JOB_COUNT} jobs, found {jobs_count}"
        )

    unique_fingerprints = db.scalar(select(func.count(func.distinct(JobRecord.fingerprint))))
    if unique_fingerprints != STAGE12_PILOT_JOB_COUNT:
        raise PilotIdentityMismatchError(
            f"expected {STAGE12_PILOT_JOB_COUNT} unique fingerprints, found {unique_fingerprints}"
        )

    profile_count = count_candidate_profiles(db)
    if profile_count != STAGE12_PILOT_CANDIDATE_PROFILE_COUNT:
        raise PilotIdentityMismatchError(
            f"expected exactly {STAGE12_PILOT_CANDIDATE_PROFILE_COUNT} CandidateProfile, "
            f"found {profile_count}"
        )

    automation_runs_count = db.scalar(select(func.count()).select_from(AutomationRunRecord))
    if automation_runs_count != STAGE12_PILOT_AUTOMATION_RUNS_COUNT:
        raise PilotIdentityMismatchError(
            f"expected exactly {STAGE12_PILOT_AUTOMATION_RUNS_COUNT} automation_runs, "
            f"found {automation_runs_count}"
        )


def _is_sentinel(title: str) -> bool:
    return any(sentinel.casefold() in title.casefold() for sentinel in SENTINEL_TITLES)


def _write_before_after_csv(path: Path, results: list[RescoreResult]) -> None:
    fieldnames = [
        "job_id",
        "fingerprint",
        "title",
        "company",
        "posting_type",
        "before_score",
        "before_recommendation",
        "before_matched_skills",
        "before_missing_must_have",
        "before_matched_skills_source",
        "after_score",
        "after_recommendation",
        "after_matched_skills",
        "after_missing_must_have",
        "first_seen_at",
        "last_seen_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(
                {
                    "job_id": r.after.id,
                    "fingerprint": r.after.fingerprint,
                    "title": _csv_safe(r.after.title),
                    "company": _csv_safe(r.after.company),
                    "posting_type": _csv_safe(r.after.posting_type or ""),
                    "before_score": r.before.score,
                    "before_recommendation": r.before.recommendation,
                    "before_matched_skills": _csv_safe(";".join(r.before.matched_skills)),
                    "before_missing_must_have": _csv_safe(";".join(r.before.missing_must_have)),
                    "before_matched_skills_source": r.before.matched_skills_source,
                    "after_score": r.after.score,
                    "after_recommendation": r.after.recommendation,
                    "after_matched_skills": _csv_safe(";".join(r.after.matched_skills)),
                    "after_missing_must_have": _csv_safe(";".join(r.after.missing_must_have)),
                    "first_seen_at": r.after.first_seen_at.isoformat(),
                    "last_seen_at": r.after.last_seen_at.isoformat(),
                }
            )


def _build_human_review_rows(results: list[RescoreResult], *, sample_seed: int):
    apply_rows = [r for r in results if r.after.recommendation == "APPLY"]
    maybe_rows = [r for r in results if r.after.recommendation == "MAYBE"]
    skip_rows = [r for r in results if r.after.recommendation == "SKIP"]
    high_score_skip_rows = [r for r in skip_rows if r.after.score >= HIGH_SCORE_SKIP_THRESHOLD]

    high_score_job_ids = {r.after.id for r in high_score_skip_rows}
    remaining_skip = [r for r in skip_rows if r.after.id not in high_score_job_ids]
    rng = random.Random(sample_seed)
    sample_size = min(DETERMINISTIC_SKIP_SAMPLE_SIZE, len(remaining_skip))
    deterministic_skip_rows = rng.sample(remaining_skip, sample_size) if sample_size else []

    selected = apply_rows + maybe_rows + high_score_skip_rows + deterministic_skip_rows
    selected_job_ids = [r.after.id for r in selected]
    assert len(selected_job_ids) == len(set(selected_job_ids)), (
        "duplicate job selected across groups"
    )
    selected.sort(key=lambda r: r.after.id)

    rows = []
    for r in selected:
        evidence = r.after.evidence
        rows.append(
            {
                "job_id": r.after.id,
                "fingerprint": r.after.fingerprint,
                "title": _csv_safe(r.after.title),
                "company": _csv_safe(r.after.company),
                "location": _csv_safe(r.after.location),
                "posting_type": _csv_safe(r.after.posting_type or ""),
                "score": r.after.score,
                "recommendation": r.after.recommendation,
                "matched_skills": _csv_safe(";".join(r.after.matched_skills)),
                "missing_must_have": _csv_safe(";".join(r.after.missing_must_have)),
                "base_score": "" if evidence.base_score is None else evidence.base_score,
                "base_recommendation": _csv_safe(evidence.base_recommendation),
                "resolved_must_have": _csv_safe(";".join(evidence.resolved_must_have)),
                "resolved_nice_to_have": _csv_safe(";".join(evidence.resolved_nice_to_have)),
                "matched_must_have": _csv_safe(";".join(evidence.matched_must_have)),
                "matched_optional": _csv_safe(";".join(evidence.matched_optional)),
                "unique_evidence_count": evidence.unique_evidence_count,
                "evidence_cardinality": _csv_safe(evidence.evidence_cardinality),
                "role_relevance": _csv_safe(evidence.role_relevance),
                "seniority_classification": _csv_safe(evidence.seniority_classification),
                "posting_classification": _csv_safe(evidence.posting_classification),
                "data_confidence": r.after.data_confidence,
                "gate_reason": _csv_safe(";".join(evidence.gate_trace)),
                "description_or_url": _csv_safe(r.after.url),
                "human_relevant": "",
                "human_decision": "",
                "human_notes": "",
            }
        )
    counts = {
        "apply": len(apply_rows),
        "maybe": len(maybe_rows),
        "high_score_skip": len(high_score_skip_rows),
        "deterministic_skip": len(deterministic_skip_rows),
    }
    return rows, counts


def _write_human_review_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HUMAN_REVIEW_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _transition(before: str, after: str) -> str:
    return f"{before} -> {after}" if before != after else "unchanged"


def build_engine(args: argparse.Namespace):
    password = os.environ.get(args.postgres_password_env)
    if not password:
        password = getpass.getpass(
            f"PostgreSQL password for {args.postgres_user}@{args.postgres_host} "
            f"(env {args.postgres_password_env} not set): "
        )
    url = URL.create(
        drivername="postgresql+psycopg",
        username=args.postgres_user,
        password=password,
        host=args.postgres_host,
        port=args.postgres_port,
        database=args.postgres_db,
    )
    return create_engine(url, connect_args={"connect_timeout": 10})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-host", default="localhost")
    parser.add_argument("--postgres-port", type=int, default=5432)
    parser.add_argument("--postgres-db", required=True)
    parser.add_argument("--postgres-user", default="jobtriage")
    parser.add_argument("--postgres-password-env", default="POSTGRES_PASSWORD")
    parser.add_argument("--export-dir", required=True, type=Path)
    parser.add_argument("--sample-seed", type=int, default=12345)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the rescore for real. Without this flag, nothing is written.",
    )
    parser.add_argument(
        "--confirm-database",
        default=None,
        help=(
            f"Required with --apply: must exactly equal {STAGE12_PILOT_DATABASE!r} (and so "
            "must --postgres-db). A separate, explicit confirmation so --apply can never "
            "run off of --postgres-db alone -- and hard-pinned to the one Stage 12 pilot "
            "database, not merely to each other, so `--postgres-db foo --confirm-database "
            "foo` is rejected even though the two agree."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not _apply_target_authorized(args.postgres_db, args.confirm_database):
        print(
            "ABORT: --apply requires both --postgres-db and --confirm-database to exactly "
            f"equal {STAGE12_PILOT_DATABASE!r} "
            f"(got --postgres-db={args.postgres_db!r}, "
            f"--confirm-database={args.confirm_database!r})",
            file=sys.stderr,
        )
        return 2

    args.export_dir.mkdir(parents=True, exist_ok=True)
    engine = build_engine(args)

    if args.apply:
        with Session(engine) as preflight_db:
            verify_pilot_identity(preflight_db)

    with Session(engine) as db:
        context, results = preview_all_jobs(db)

    if args.apply:
        results = apply_rescore(engine, context, results)

    before_after_path = args.export_dir / f"stage12_rescore_{args.postgres_db}_before_after.csv"
    _write_before_after_csv(before_after_path, results)

    review_rows, review_counts = _build_human_review_rows(results, sample_seed=args.sample_seed)
    review_path = args.export_dir / f"stage12_human_review_{args.postgres_db}.csv"
    _write_human_review_csv(review_path, review_rows)

    before_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}
    transitions: dict[str, int] = {}
    changed: list[RescoreResult] = []
    for r in results:
        before_counts[r.before.recommendation] = before_counts.get(r.before.recommendation, 0) + 1
        after_counts[r.after.recommendation] = after_counts.get(r.after.recommendation, 0) + 1
        t = _transition(r.before.recommendation, r.after.recommendation)
        transitions[t] = transitions.get(t, 0) + 1
        if r.before.recommendation != r.after.recommendation or r.before.score != r.after.score:
            changed.append(r)

    print(f"MODE: {'APPLY (persisted)' if args.apply else 'PREVIEW (no writes)'}")
    print(f"jobs processed: {len(results)}")
    print(f"before distribution: {before_counts}")
    print(f"after distribution:  {after_counts}")
    print(f"transitions: {transitions}")
    print(f"changed (score or recommendation): {len(changed)}")
    print()
    print("SENTINELS:")
    for r in results:
        if _is_sentinel(r.after.title):
            print(
                f"  [{r.after.id}] {r.after.title!r} @ {r.after.company!r}: "
                f"score {r.before.score} -> {r.after.score}, "
                f"recommendation {r.before.recommendation} -> {r.after.recommendation}, "
                f"missing_must_have={r.after.missing_must_have}"
            )
    print()
    print(f"human review sample: {review_counts} (total rows: {len(review_rows)})")
    print(f"HIGH_SCORE_SKIP_THRESHOLD: {HIGH_SCORE_SKIP_THRESHOLD}")
    print(f"before/after export: {before_after_path}")
    print(f"human review export: {review_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
