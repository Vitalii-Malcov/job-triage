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

from app.agents.evidence_cardinality_classifier import (
    classify_evidence_cardinality,
    unique_evidence_signals,
)
from app.agents.evidence_quality_classifier import classify_evidence_quality
from app.agents.job_scorer import JobScorer
from app.agents.posting_classifier import classify_posting
from app.agents.role_relevance_classifier import classify_title_relevance
from app.agents.seniority_classifier import classify_title_seniority
from app.models.job import Job, JobScore

logger = logging.getLogger(__name__)


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
) -> JobScore:
    """The full Stage 10/11A/11B/11E/11C classify-then-score decision for
    `job`, given the posting_type to classify against and the candidate's
    already-resolved preferences. Pure -- callable any number of times with
    the same arguments for the same result, exactly like the
    `_score_for_posting_type` body this was extracted from.
    """
    classification = classify_posting(
        title=job.title,
        posting_type=effective_posting_type,
        allowed_employment_types=frozenset(allowed_employment_types),
    )
    if not classification.is_target_employment:
        logger.info(
            "job_excluded_non_target_posting job_title=%s source=%s reason=%s",
            job.title,
            job.source,
            classification.excluded_reason,
        )
        return excluded_job_score()

    result = JobScorer(candidate_skills).score(job)

    # Stage 11A: an explicit senior/lead-level TITLE must not reach
    # MAYBE/APPLY for a candidate who has explicitly (and unambiguously)
    # targeted junior roles. Only checked once a job has already reached
    # APPLY/MAYBE on ordinary skill-match grounds.
    if result.recommendation in ("APPLY", "MAYBE"):
        candidate_target = get_candidate_target_seniority()
        if candidate_target == "JUNIOR":
            title_seniority = classify_title_seniority(job.title)
            if title_seniority.level == "SENIOR":
                logger.info(
                    "job_excluded_seniority_mismatch job_title=%s source=%s "
                    "candidate_target_seniority=%s matched_signal=%s",
                    job.title,
                    job.source,
                    candidate_target,
                    title_seniority.matched_signal,
                )
                return excluded_job_score()

    # Stage 11B: a confidently IRRELEVANT-titled job must not reach
    # MAYBE/APPLY for a candidate whose target roles unanimously name a
    # software-development role family.
    if result.recommendation in ("APPLY", "MAYBE"):
        candidate_domain = get_candidate_target_domain()
        if candidate_domain == "SOFTWARE_DEVELOPMENT":
            title_relevance = classify_title_relevance(job.title)
            if title_relevance.level == "IRRELEVANT":
                logger.info(
                    "job_excluded_role_irrelevant job_title=%s source=%s "
                    "candidate_target_domain=%s matched_signal=%s",
                    job.title,
                    job.source,
                    candidate_domain,
                    title_relevance.matched_signal,
                )
                return excluded_job_score()

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

    # Stage 11C: a plausibly software-development-titled job must not be
    # left at an automatic SKIP purely because STRUCTURED skill extraction
    # was thin. Only ever raises an ALREADY-SKIP result to MAYBE.
    if result.recommendation == "SKIP":
        title_relevance = classify_title_relevance(job.title)
        title_seniority = classify_title_seniority(job.title)
        senior_mismatch = title_seniority.level == "SENIOR" and (
            get_candidate_target_seniority() == "JUNIOR"
        )
        if (
            title_relevance.level == "RELEVANT"
            and not senior_mismatch
            and get_candidate_target_domain() == "SOFTWARE_DEVELOPMENT"
        ):
            must_have_total = len(result.matched_must_have) + len(result.missing_must_have)
            nice_to_have_total = len(job.nice_to_have_skills)
            no_concrete_missing_must = len(result.missing_must_have) == 0
            evidence_quality = classify_evidence_quality(must_have_total, nice_to_have_total)
            if evidence_quality == "SPARSE" and no_concrete_missing_must:
                logger.info(
                    "job_recommendation_floor_sparse_evidence job_title=%s source=%s "
                    "must_have_total=%s nice_to_have_total=%s original_score=%s "
                    "matched_signal=%s",
                    job.title,
                    job.source,
                    must_have_total,
                    nice_to_have_total,
                    result.score,
                    title_relevance.matched_signal,
                )
                result = result.model_copy(update={"recommendation": "MAYBE"})

    return result
