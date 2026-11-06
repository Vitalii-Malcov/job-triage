"""Stage 23: shortlist quality gate -- a read-only layer between the fit
assessment (`app.services.job_fit_assessment.FitAssessment`) and a final TOP-N.

It does NOT score anything and does not duplicate the assessment: it reads the
criteria the assessment already computed and answers one question for the
operator -- "is this worth a final human look for a Bewerbung?"

- SKIP            : a CONFIRMED barrier (the fit tier is NOT_SUITABLE: off-target
                    specialization, senior title, stated and unmet experience or
                    language, out of reach, already applied, vacancy gone).
- APPLY_CANDIDATE : target specialization, skills at least partly matching, no
                    stated gap, no open verification. Still needs the operator's
                    final check -- never an instruction to apply.
- REVIEW          : critical facts are missing or a gap is stated but not
                    prohibitive. Missing information is never a SKIP; every
                    open point is listed in `questions`.

Every verdict carries its reasons. Nothing here writes, sends or changes a
status.
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import SingletonThreadPool, StaticPool

from app.services.job_fit_assessment import FitAssessment, assess_job

logger = logging.getLogger(__name__)

GateVerdict = Literal["APPLY_CANDIDATE", "REVIEW", "SKIP"]
Liveness = Literal["ACTIVE", "GONE", "UNKNOWN"]

# Skills statuses that count as evidence of a Python/backend fit.
_SKILLS_OK = {"STRONG", "PARTIAL"}
_ROLE_OK = {"PRIMARY", "SECONDARY"}


@dataclass(frozen=True)
class GateResult:
    job_id: int
    verdict: GateVerdict
    reasons: tuple[str, ...]
    questions: tuple[str, ...] = field(default_factory=tuple)
    strengths: tuple[str, ...] = field(default_factory=tuple)


def _line(criterion) -> str:
    evidence = f' ("{criterion.evidence}")' if criterion.evidence else ""
    return f"{criterion.name} {criterion.status}: {criterion.reason}{evidence}"


def evaluate(assessment: FitAssessment, *, liveness: Liveness = "UNKNOWN") -> GateResult:
    """Pure: the verdict from an assessment and the (optional) live check."""
    tier = assessment.fit_tier
    if liveness == "GONE":
        return GateResult(
            assessment.job_id, "SKIP", ("the vacancy is no longer online (BA liveness check)",)
        )
    if tier == "NOT_SUITABLE":
        return GateResult(assessment.job_id, "SKIP", tuple(assessment.tier_reasons))

    by_name = {c.name: c for c in assessment.criteria}
    questions = [_line(c) for c in assessment.verifications]
    questions += [
        _line(c)
        for c in assessment.criteria
        if c.name == "domain" and c.status in {"OFF_DOMAIN_REQUIRED", "OFF_DOMAIN_TRAINING"}
    ]
    if liveness == "UNKNOWN":
        questions.append("confirm the vacancy is still online before any Bewerbung")

    role = by_name.get("role")
    skills = by_name.get("skills")
    entry = by_name.get("entry_level")
    strengths = [
        _line(c)
        for c in (role, skills, entry)
        if c is not None and c.status in _ROLE_OK | _SKILLS_OK | {"ENTRY_LEVEL"}
    ]

    reasons: list[str] = []
    if tier in {"NEEDS_OPERATOR", "STRETCH"}:
        reasons += assessment.tier_reasons
    if role is None or role.status not in _ROLE_OK:
        reasons.append(
            "role: title matches no configured target role"
            + (f" ({role.status})" if role is not None else "")
        )
    if skills is None or skills.status not in _SKILLS_OK:
        reasons.append("skills: no usable evidence of a skill match")
    if entry is not None and entry.status in {"EXPERIENCED", "MIXED"}:
        reasons.append(_line(entry))
    open_points = [q for q in questions if not q.startswith("confirm the vacancy")]
    if open_points and tier in {"GOOD_MATCH", "PARTIAL_MATCH"}:
        # Open verifications never make a good match a SKIP; they keep it
        # out of APPLY_CANDIDATE until the operator has answered them.
        reasons.append("open points to verify before a Bewerbung")

    if tier in {"GOOD_MATCH", "PARTIAL_MATCH"} and not reasons and not open_points:
        return GateResult(
            assessment.job_id,
            "APPLY_CANDIDATE",
            (f"fit tier {tier}", *assessment.tier_reasons[:3]),
            tuple(questions),
            tuple(strengths),
        )
    return GateResult(
        assessment.job_id,
        "REVIEW",
        tuple(reasons or [f"fit tier {tier}"]),
        tuple(questions),
        tuple(strengths),
    )


# --- automatic preparation / proposal gate (the single source of the rule) ---

# Fit tiers that may enter AUTOMATIC CV/Bewerbung preparation. Everything else
# is held for the operator: NOT_SUITABLE (confirmed barrier), NEEDS_OPERATOR
# (critical facts missing), STRETCH (stated gap) and any unknown tier
# (fail-closed). A high `jobs.score` or an APPLY recommendation never bypasses
# this. Being allowed here is NOT approval to apply.
AUTO_PREPARE_TIERS = frozenset({"GOOD_MATCH", "PARTIAL_MATCH"})

BLOCK_ASSESSMENT_UNAVAILABLE = "ASSESSMENT_UNAVAILABLE"


def automatic_block_reason(assessment: FitAssessment) -> str | None:
    """Pure: None when the vacancy may be prepared automatically, else a fixed
    reason code (no vacancy text, safe to log)."""
    tier = assessment.fit_tier
    if tier in AUTO_PREPARE_TIERS:
        return None
    return (
        f"FIT_TIER_{tier}" if isinstance(tier, str) and tier.isidentifier() else "FIT_TIER_UNKNOWN"
    )


class _NoIsolatedConnection(RuntimeError):
    """The caller's engine cannot give the assessment its own connection."""


def _assess_isolated(db: Session, job_id: int) -> FitAssessment:
    """A LIVE assessment in its own short read-only session on its OWN connection:
    never the `job_fit_assessments` cache (a stale or missing cache cannot admit a
    job), never the caller's identity map (fresh rows), and a failure or the rollback
    that ends this read cannot touch the caller's transaction. A bind that cannot give
    a separate connection (a bare Connection, or a single shared in-memory SQLite
    connection) is refused -- the gate then fails closed instead of sharing state."""
    bind = db.get_bind()
    if not isinstance(bind, Engine) or isinstance(bind.pool, (StaticPool, SingletonThreadPool)):
        raise _NoIsolatedConnection(type(bind).__name__)
    with Session(bind=bind) as read_session:
        return assess_job(read_session, job_id)


def automatic_preparation_block(db: Session, job_id: int) -> str | None:
    """Fail-closed: None only when a fresh assessment says the vacancy may be
    prepared automatically. Writes nothing, changes no status, sends nothing."""
    try:
        return automatic_block_reason(_assess_isolated(db, job_id))
    except Exception as exc:  # noqa: BLE001 -- unknown state must hold the job back
        logger.warning(
            "shortlist_gate_assessment_failed job_id=%s error_type=%s", job_id, type(exc).__name__
        )
        return BLOCK_ASSESSMENT_UNAVAILABLE


def confirmed_barrier(db: Session, job_id: int) -> str | None:
    """For PROPOSALS to the operator (Telegram cards), where a human still
    decides: only a CONFIRMED barrier (NOT_SUITABLE) or an unavailable
    assessment holds a vacancy back. Missing facts (NEEDS_OPERATOR) are exactly
    what the operator is asked to look at, so they are not blocked here."""
    try:
        tier = _assess_isolated(db, job_id).fit_tier
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "shortlist_gate_assessment_failed job_id=%s error_type=%s", job_id, type(exc).__name__
        )
        return BLOCK_ASSESSMENT_UNAVAILABLE
    return "FIT_TIER_NOT_SUITABLE" if tier == "NOT_SUITABLE" else None
