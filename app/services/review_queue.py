"""Manual review queue: the vacancies that wait for a decision by the operator.

Pure and read-only. It answers three questions about one vacancy from data the pipeline already
produced (the fit assessment and the triage category): is it waiting for a human, why, and what
exactly has to be clarified. It stores nothing, changes no status, prepares nothing and never
replaces a gate: a vacancy in this queue is NOT cleared for an automatic application.
`fit_tier`, `AUTO_PREPARE_TIERS`, the late re-check, approval and duplicate protection stay the
only rules about preparing or sending anything.

Membership:

* triage category MANUAL_REVIEW (route, data, remote, hybrid role, stated gap, thin skills ...);
* a vacancy whose description is missing or too short (scorer result NEEDS_ENRICHMENT) with a
  software-development-like title that triage excluded only because no skill evidence exists --
  a title-only stub is unknown, not unsuitable;
* never a vacancy with a confirmed barrier (fit tier NOT_SUITABLE) and never one already
  applied for.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.agents.target_role import is_developer_title, is_search_target_title
from app.services.fit_cache import applied
from app.services.job_fit_assessment import FitAssessment
from app.services.triage_category import (
    DATA_REVIEW_REQUIRED,
    HYBRID_ADMIN_ROLE,
    NOT_TARGET_ROLE,
    REMOTE_UNVERIFIED,
    ROLE_UNCLEAR,
    ROUTE_REVIEW_REQUIRED,
    STATED_GAP,
    THIN_SKILL_EVIDENCE,
    classify,
)

NEEDS_ENRICHMENT = "NEEDS_ENRICHMENT"
LANGUAGE_REVIEW = "LANGUAGE_REVIEW"

# Reason codes that can be the main reason of a queue entry, in the order they are offered.
REASON_CODES = (
    THIN_SKILL_EVIDENCE,
    NEEDS_ENRICHMENT,
    ROUTE_REVIEW_REQUIRED,
    REMOTE_UNVERIFIED,
    HYBRID_ADMIN_ROLE,
    DATA_REVIEW_REQUIRED,
    ROLE_UNCLEAR,
    STATED_GAP,
)

_CLARIFY = {
    THIN_SKILL_EVIDENCE: "Skills could not be confirmed from the text: read the requirements and "
    "decide whether the role fits your skills.",
    NEEDS_ENRICHMENT: "The description is missing or too short: open the source and read the full "
    "posting before deciding. Nothing about skills, hours or place is assumed.",
    ROUTE_REVIEW_REQUIRED: "No route to the workplace is confirmed: check whether the place is "
    "reachable, or whether the role is remote.",
    REMOTE_UNVERIFIED: "Remote work is not confirmed as full and allowed from Germany: read what "
    "the posting says about the place of work.",
    HYBRID_ADMIN_ROLE: "The title mixes an administration or support role with a developer role: "
    "read what the job mostly is.",
    DATA_REVIEW_REQUIRED: "Key facts are missing: read the posting.",
    ROLE_UNCLEAR: "The title has no clear IT signal: decide whether this is a software role.",
    STATED_GAP: "The posting states a gap (see the reason): decide whether you accept it.",
}

_GENERIC_CLARIFY = "Read the posting and decide; the reason says what is open."
_JUNIOR_WORDS = ("junior", "trainee", "berufseinsteiger", "einsteiger", "entry")
_TECH_WORDS = ("python", "backend", "back-end")


@dataclass(frozen=True)
class ReviewItem:
    job_id: int
    title: str
    company: str
    source: str
    url: str | None
    first_seen_at: datetime | None
    score: int
    recommendation: str
    fit_tier: str
    # DEVELOPER (software title) | ADJACENT (data, DevOps, automation ...) | OTHER |
    # HYBRID_ADMIN (admin/support mixed with a developer role: always last)
    family: str
    junior_python: bool
    work_format: str
    work_time: str
    reason_code: str
    reason: str
    clarify: list[str] = field(default_factory=list)
    also: list[str] = field(default_factory=list)  # further codes that apply


def is_junior_python_title(title: str) -> bool:
    """Plain substring test on purpose: the UI selects this group in SQL (`ilike`) and the
    two sides have to agree exactly."""
    text = (title or "").casefold()
    return any(w in text for w in _JUNIOR_WORDS) and any(w in text for w in _TECH_WORDS)


def junior_python_words() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _JUNIOR_WORDS, _TECH_WORDS


def _status(assessment: FitAssessment, name: str) -> str:
    return next((c.status for c in assessment.criteria if c.name == name), "UNKNOWN")


def review_item(job, assessment: FitAssessment, url: str | None) -> ReviewItem | None:
    """The queue entry of `job`, or None when it does not wait for a human."""
    if assessment.fit_tier == "NOT_SUITABLE" or applied(assessment):
        return None
    triage = classify(assessment)
    code, reason = triage.code, triage.reason
    if triage.category == "MANUAL_REVIEW":
        pass
    elif (
        triage.category == "EXCLUDED"
        and triage.code == NOT_TARGET_ROLE
        and job.recommendation == NEEDS_ENRICHMENT
        and is_search_target_title(job.title)
    ):
        code = NEEDS_ENRICHMENT
        reason = "description missing or too short; nothing is known about skills, hours or place"
    else:
        return None
    also = []
    if code != NEEDS_ENRICHMENT and job.recommendation == NEEDS_ENRICHMENT:
        also.append(NEEDS_ENRICHMENT)
    if code != REMOTE_UNVERIFIED and triage.work_format == "REMOTE_UNVERIFIED":
        also.append(REMOTE_UNVERIFIED)
    if _status(assessment, "language") in {"GAP", "GAP_TO_VERIFY", "MAJOR_GAP"}:
        also.append(LANGUAGE_REVIEW)
    clarify = [_CLARIFY.get(code, _GENERIC_CLARIFY)] + [_CLARIFY[a] for a in also if a in _CLARIFY]
    if LANGUAGE_REVIEW in also:
        clarify.append(
            "German is asked above your confirmed level: check whether it is required or "
            "only preferred."
        )
    if code == HYBRID_ADMIN_ROLE:
        # an administration/support role mixed with a developer role is never ranked as a
        # developer vacancy and never as a Junior Python/Backend one
        family = "HYBRID_ADMIN"
    elif is_developer_title(job.title):
        family = "DEVELOPER"
    else:
        family = "ADJACENT" if _status(assessment, "skills") in {"STRONG", "PARTIAL"} else "OTHER"
    return ReviewItem(
        job_id=job.id,
        title=job.title,
        company=job.company,
        source=job.source,
        url=url,
        first_seen_at=job.first_seen_at,
        score=job.score,
        recommendation=job.recommendation,
        fit_tier=assessment.fit_tier,
        family=family,
        junior_python=family != "HYBRID_ADMIN" and is_junior_python_title(job.title),
        work_format=triage.work_format,
        work_time=_status(assessment, "work_time"),
        reason_code=code,
        reason=reason,
        clarify=clarify,
        also=also,
    )


_FAMILY_RANK = {"DEVELOPER": 0, "ADJACENT": 1, "OTHER": 2, "HYBRID_ADMIN": 3}
_FORMAT_RANK = {"FULL_REMOTE": 0, "HYBRID": 1}


def sort_key(item: ReviewItem) -> tuple:
    """Junior Python/Backend first, then developer roles before adjacent ones, then fully
    remote before hybrid before the rest, then the higher score, then the newer vacancy."""
    return (
        0 if item.junior_python else 1,
        _FAMILY_RANK.get(item.family, 2),
        _FORMAT_RANK.get(item.work_format, 2),
        -item.score,
        -item.job_id,
    )
