"""Pure, DB-free job-scoring decision primitive.

Extracted from `app.services.collector_runner._score_for_posting_type` so
the exact same gate/threshold logic (Stage 10 posting-type exclusion,
Stage 11A seniority gate, Stage 11B domain-relevance gate, Stage 11E
evidence-cardinality downgrade, Stage 11C sparse-evidence rescue) is
available to a caller that has no live database `Session` to hand it --
specifically `scripts/offline_rescore_stage12.py`'s read-only preview path,
which must reuse this decision logic without duplicating it and without
triggering any of the DB reads/writes `app.services.collector_runner`'s own
helpers perform.

**Zero DB access. Zero side effects. Deterministic.** Every candidate-
preference input this function needs is supplied by the caller, either as a
plain value (`candidate_skills`, `allowed_employment_types` -- both cheap,
already-resolved-by-the-caller values in every real call site) or as a
zero-argument callable (`get_candidate_target_seniority`,
`get_candidate_target_domain`). The callables exist ONLY so
`app.services.collector_runner._score_for_posting_type` can keep its
original lazy/short-circuited read pattern (never touching
`CandidateProfile` for a job that's already excluded or already SKIP on
skill grounds alone) -- this function calls each callable at the exact same
points, and the exact same number of times (zero, one, or -- in the one
pre-existing case where Stage 11E's own downgrade feeds back into Stage
11C's re-check -- up to twice), that the logic it replaces already did.
Callers with a plain in-memory value (the offline script) can simply pass
`lambda: value`.
"""

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from app.agents.evidence_cardinality_classifier import (
    classify_evidence_cardinality,
    unique_evidence_signals,
)
from app.agents.evidence_quality_classifier import classify_evidence_quality
from app.agents.job_scorer import JobScorer, normalize_skill
from app.agents.posting_classifier import classify_posting
from app.agents.role_relevance_classifier import classify_title_relevance
from app.agents.seniority_classifier import classify_title_seniority
from app.models.job import Job, JobScore

logger = logging.getLogger(__name__)


@dataclass
class EvaluationTrace:
    """OBSERVATIONAL-ONLY audit of one `evaluate_job_score` call (Stage 12
    review-export remediation, Astra M4 finding).

    Astra's M4 defect: a human-review export reconstructed its evidence
    from the FINAL `JobScore`, but every exclusion gate returns
    `excluded_job_score()` -- a zeroed result with empty match lists. A
    Senior Backend Developer with `must=[python, postgresql]` that scored
    90/APPLY and was then correctly excluded by the Stage 11A seniority
    gate exported as `resolved_must_have=[] matched_must_have=[]
    unique_evidence_count=1 LOW_CARDINALITY`, which is simply false about
    what the scorer actually saw. The fix is to capture the BASE evidence
    at the moment it exists -- inside the evaluation, before any early
    return erases it -- instead of trying to recover it afterwards.

    **This object never influences the decision.** Every write to it in
    `evaluate_job_score` is behind `if trace is not None`, nothing is ever
    read back out of it by the decision path, and the returned `JobScore`
    is byte-for-byte identical whether or not a trace is passed (see
    `tests/test_job_score_evaluator_trace.py`). The candidate-preference
    callables (`get_candidate_target_seniority`/`get_candidate_target_
    domain`) are NOT called any extra times to fill it in -- only pure,
    DB-free classifiers/scorers are, and only where the un-traced path
    would already have the value or where the value is needed to record an
    otherwise-erased base result.

    Skill lists are stored CANONICALLY NORMALIZED (via
    `app.agents.job_scorer.normalize_skill`, the same normalization
    scoring itself uses), so "REST API", "rest-api" and "REST" appear as
    the single signal "rest" a reviewer can reconcile against
    `unique_evidence_count` -- Astra's third M4 point.
    """

    # Ordered, machine-readable record of which gates ran and what they
    # decided -- including explicit "not applicable" entries, so a reader
    # never has to infer a gate outcome from the final score.
    events: list[str] = field(default_factory=list)

    # The pre-exclusion scoring result and its evidence. `base_score` is
    # None only when the job never reached the scorer at all.
    base_score: int | None = None
    base_recommendation: str | None = None
    base_data_confidence: float | None = None
    resolved_must_have: list[str] = field(default_factory=list)
    resolved_nice_to_have: list[str] = field(default_factory=list)
    matched_must_have: list[str] = field(default_factory=list)
    missing_must_have: list[str] = field(default_factory=list)
    matched_optional: list[str] = field(default_factory=list)
    unique_evidence_count: int = 0
    evidence_cardinality: str = ""

    # Gate classifications, recorded as each gate observed them.
    posting_classification: str = ""
    seniority_classification: str = ""
    role_relevance: str = ""

    # The outcome actually returned to the caller.
    final_score: int | None = None
    final_recommendation: str | None = None

    def _record_base(self, job: Job, result: JobScore) -> None:
        resolved_must = result.matched_must_have + result.missing_must_have
        self.base_score = result.score
        self.base_recommendation = result.recommendation
        self.base_data_confidence = result.data_confidence
        self.resolved_must_have = sorted({normalize_skill(s) for s in resolved_must if s.strip()})
        self.resolved_nice_to_have = sorted(
            {normalize_skill(s) for s in job.nice_to_have_skills if s.strip()}
        )
        self.matched_must_have = sorted(result.matched_must_have)
        self.missing_must_have = sorted(result.missing_must_have)
        self.matched_optional = sorted(result.matched_nice_to_have)
        self.unique_evidence_count = len(
            unique_evidence_signals(resolved_must, job.nice_to_have_skills)
        )
        self.evidence_cardinality = classify_evidence_cardinality(
            resolved_must, job.nice_to_have_skills
        )
        self.events.append(f"base_score:{result.score}:{result.recommendation}")

    def _record_final(self, result: JobScore) -> None:
        self.final_score = result.score
        self.final_recommendation = result.recommendation
        self.events.append(f"final:{result.score}:{result.recommendation}")


def excluded_job_score() -> JobScore:
    """The shared zeroed-out shape for a job forced out of the normal
    scoring pipeline -- Stage 10's posting_type exclusion and Stage 11A's
    seniority-mismatch exclusion both return exactly this (same
    score=0/SKIP/zero-confidence result), so neither invents a new magic
    score threshold. The two exclusion reasons are indistinguishable from
    the JobScore/JobRecord shape alone by design -- each call site is
    expected to log its own specific reason as the auditable record of WHY.
    """
    return JobScore(
        score=0,
        matched_skills=[],
        missing_skills=[],
        matched_must_have=[],
        missing_must_have=[],
        matched_nice_to_have=[],
        recommendation="SKIP",
        data_confidence=0.0,
    )


def evaluate_job_score(
    job: Job,
    effective_posting_type: str | None,
    *,
    candidate_skills: Collection[str],
    allowed_employment_types: Collection[str],
    get_candidate_target_seniority: Callable[[], str],
    get_candidate_target_domain: Callable[[], str],
    trace: EvaluationTrace | None = None,
) -> JobScore:
    """The full Stage 10/11A/11B/11E/11C classify-then-score decision for
    `job`, given the posting_type to classify against and the candidate's
    already-resolved preferences. Pure -- callable any number of times with
    the same arguments for the same result, exactly like the
    `_score_for_posting_type` body this was extracted from.

    `trace` (Stage 12 review-export remediation, Astra M4 finding): when a
    caller passes an `EvaluationTrace`, this function records -- WHILE
    deciding, before any early return can erase it -- the pre-exclusion
    base score/evidence, each gate's outcome (including explicit
    "not applicable" entries), and the final result. Purely additive and
    opt-in: every write is behind `if trace is not None`, nothing is read
    back out of it, and the returned `JobScore` is identical whether or
    not a trace is passed. See `EvaluationTrace`'s own docstring.
    """
    classification = classify_posting(
        title=job.title,
        posting_type=effective_posting_type,
        allowed_employment_types=frozenset(allowed_employment_types),
    )
    if trace is not None:
        trace.posting_classification = (
            "TARGET_EMPLOYMENT"
            if classification.is_target_employment
            else f"EXCLUDED:{classification.excluded_reason}"
        )
    if not classification.is_target_employment:
        if trace is not None:
            trace.events.append(f"posting_excluded:{classification.excluded_reason}")
            # Observational ONLY: the decision is already made (the
            # `return` below is unconditional), but a reviewer still needs
            # to see what evidence the posting carried -- Astra's M4
            # point 1, "evidence must remain present even though the final
            # recommendation is SKIP". `JobScorer.score` is pure and
            # DB-free, so computing it here has no effect beyond filling
            # in the audit record.
            trace._record_base(job, JobScorer(candidate_skills).score(job))
            trace.events.append("seniority_not_applicable:posting_excluded")
            trace.events.append("role_not_applicable:posting_excluded")
            trace.events.append("cardinality_not_applicable:posting_excluded")
            trace.events.append("sparse_rescue_not_applicable:posting_excluded")
        logger.info(
            "job_excluded_non_target_posting job_title=%s source=%s reason=%s",
            job.title,
            job.source,
            classification.excluded_reason,
        )
        excluded = excluded_job_score()
        if trace is not None:
            trace._record_final(excluded)
        return excluded
    if trace is not None:
        trace.events.append("posting:TARGET")

    result = JobScorer(candidate_skills).score(job)
    if trace is not None:
        trace._record_base(job, result)

    # Stage 11A: an explicit senior/lead-level TITLE must not reach
    # MAYBE/APPLY for a candidate who has explicitly (and unambiguously)
    # targeted junior roles. Only checked once a job has already reached
    # APPLY/MAYBE on ordinary skill-match grounds.
    if result.recommendation in ("APPLY", "MAYBE"):
        candidate_target = get_candidate_target_seniority()
        if candidate_target == "JUNIOR":
            title_seniority = classify_title_seniority(job.title)
            if trace is not None:
                trace.seniority_classification = title_seniority.level
                trace.events.append(f"seniority:{title_seniority.level}:{candidate_target}")
            if title_seniority.level == "SENIOR":
                if trace is not None:
                    trace.events.append(f"seniority_excluded:{title_seniority.matched_signal}")
                    trace.events.append("role_not_applicable:seniority_excluded")
                    trace.events.append("cardinality_not_applicable:seniority_excluded")
                    trace.events.append("sparse_rescue_not_applicable:seniority_excluded")
                logger.info(
                    "job_excluded_seniority_mismatch job_title=%s source=%s "
                    "candidate_target_seniority=%s matched_signal=%s",
                    job.title,
                    job.source,
                    candidate_target,
                    title_seniority.matched_signal,
                )
                excluded = excluded_job_score()
                if trace is not None:
                    trace._record_final(excluded)
                return excluded
        elif trace is not None:
            trace.events.append(f"seniority_not_applicable:candidate_target:{candidate_target}")
    elif trace is not None:
        trace.events.append(f"seniority_not_applicable:base_{result.recommendation}")

    # Stage 11B: a confidently IRRELEVANT-titled job must not reach
    # MAYBE/APPLY for a candidate whose target roles unanimously name a
    # software-development role family.
    if result.recommendation in ("APPLY", "MAYBE"):
        candidate_domain = get_candidate_target_domain()
        if candidate_domain == "SOFTWARE_DEVELOPMENT":
            title_relevance = classify_title_relevance(job.title)
            if trace is not None:
                trace.role_relevance = title_relevance.level
                trace.events.append(f"role:{title_relevance.level}:{candidate_domain}")
            if title_relevance.level == "IRRELEVANT":
                if trace is not None:
                    trace.events.append(f"role_excluded:{title_relevance.matched_signal}")
                    trace.events.append("cardinality_not_applicable:role_excluded")
                    trace.events.append("sparse_rescue_not_applicable:role_excluded")
                logger.info(
                    "job_excluded_role_irrelevant job_title=%s source=%s "
                    "candidate_target_domain=%s matched_signal=%s",
                    job.title,
                    job.source,
                    candidate_domain,
                    title_relevance.matched_signal,
                )
                excluded = excluded_job_score()
                if trace is not None:
                    trace._record_final(excluded)
                return excluded
        elif trace is not None:
            trace.events.append(f"role_not_applicable:candidate_domain:{candidate_domain}")
    elif trace is not None:
        trace.events.append(f"role_not_applicable:base_{result.recommendation}")

    # Stage 11E: an APPLY/MAYBE recommendation must be supported by at
    # least MINIMUM_UNIQUE_EVIDENCE_SIGNALS DISTINCT normalized structured
    # skill signals -- not merely that many CATEGORY entries.
    if result.recommendation in ("APPLY", "MAYBE"):
        resolved_must_evidence = result.matched_must_have + result.missing_must_have
        evidence_cardinality = classify_evidence_cardinality(
            resolved_must_evidence, job.nice_to_have_skills
        )
        if evidence_cardinality == "LOW_CARDINALITY":
            unique_signals = unique_evidence_signals(
                resolved_must_evidence, job.nice_to_have_skills
            )
            if trace is not None:
                trace.events.append(f"low_cardinality_downgrade:{len(unique_signals)}")
            logger.info(
                "job_recommendation_downgraded_low_cardinality job_title=%s source=%s "
                "original_recommendation=%s score=%s unique_evidence_count=%s "
                "unique_evidence_signals=%s",
                job.title,
                job.source,
                result.recommendation,
                result.score,
                len(unique_signals),
                sorted(unique_signals),
            )
            result = result.model_copy(update={"recommendation": "SKIP"})
        elif trace is not None:
            trace.events.append(
                "cardinality_sufficient:"
                f"{len(unique_evidence_signals(resolved_must_evidence, job.nice_to_have_skills))}"
            )
    elif trace is not None:
        trace.events.append(f"cardinality_not_applicable:base_{result.recommendation}")

    # Stage 11C: a plausibly software-development-titled job must not be
    # left at an automatic SKIP purely because STRUCTURED skill extraction
    # was thin. Only ever raises an ALREADY-SKIP result to MAYBE.
    if result.recommendation == "SKIP":
        title_relevance = classify_title_relevance(job.title)
        title_seniority = classify_title_seniority(job.title)
        if trace is not None:
            if not trace.role_relevance:
                trace.role_relevance = title_relevance.level
            if not trace.seniority_classification:
                trace.seniority_classification = title_seniority.level
        senior_mismatch = title_seniority.level == "SENIOR" and (
            get_candidate_target_seniority() == "JUNIOR"
        )
        if (
            title_relevance.level == "RELEVANT"
            and not senior_mismatch
            and get_candidate_target_domain() == "SOFTWARE_DEVELOPMENT"
        ):
            # M2 fix (Astra Stage 12 audit): the same DISTINCT, cross-
            # category-deduplicated evidence count Stage 11E already uses
            # above -- not raw must_have_total + nice_to_have_total, which
            # let a single skill duplicated across both categories
            # (must=[python], nice=[python]) count as 2 signals and
            # incorrectly escape sparse-rescue eligibility relative to the
            # equivalent must=[python], nice=[] case.
            resolved_must_evidence = result.matched_must_have + result.missing_must_have
            unique_signal_count = len(
                unique_evidence_signals(resolved_must_evidence, job.nice_to_have_skills)
            )
            no_concrete_missing_must = len(result.missing_must_have) == 0
            evidence_quality = classify_evidence_quality(unique_signal_count)
            if evidence_quality == "SPARSE" and no_concrete_missing_must:
                if trace is not None:
                    trace.events.append(f"sparse_rescue_applied:{unique_signal_count}")
                logger.info(
                    "job_recommendation_floor_sparse_evidence job_title=%s source=%s "
                    "unique_evidence_count=%s original_score=%s matched_signal=%s",
                    job.title,
                    job.source,
                    unique_signal_count,
                    result.score,
                    title_relevance.matched_signal,
                )
                result = result.model_copy(update={"recommendation": "MAYBE"})
            elif trace is not None:
                trace.events.append(
                    f"sparse_rescue_not_eligible:{evidence_quality}:"
                    f"missing_must={len(result.missing_must_have)}"
                )
        elif trace is not None:
            trace.events.append(
                f"sparse_rescue_not_applicable:role:{title_relevance.level}:"
                f"senior_mismatch:{senior_mismatch}"
            )
    elif trace is not None:
        trace.events.append(f"sparse_rescue_not_applicable:base_{result.recommendation}")

    if trace is not None:
        trace._record_final(result)
    return result
