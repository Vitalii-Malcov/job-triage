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
from app.services.shortlist_gate import (
    AUTO_PREPARE_TIERS,
    BLOCK_WORK_TIME_NOT_CONFIRMED,
    work_time_evidence_confirmed,
)
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
# A developer vacancy the pipeline would take on its own except that its working time is not
# confirmed as full time (unknown or conflicting): held by the gate, so a human has to read it.
WORK_TIME_NOT_CONFIRMED = BLOCK_WORK_TIME_NOT_CONFIRMED

# Reason codes that can be the main reason of a queue entry, in the order they are offered.
REASON_CODES = (
    THIN_SKILL_EVIDENCE,
    NEEDS_ENRICHMENT,
    WORK_TIME_NOT_CONFIRMED,
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
    WORK_TIME_NOT_CONFIRMED: "Full time is not confirmed (unknown or conflicting hours): read the "
    "posting for the weekly hours before deciding.",
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

# Short plain-language label of each reason code, shown above the detailed reason.
REASON_LABELS = {
    THIN_SKILL_EVIDENCE: "Skills not confirmed from the text",
    NEEDS_ENRICHMENT: "Description missing or too short",
    WORK_TIME_NOT_CONFIRMED: "Full time not confirmed",
    ROUTE_REVIEW_REQUIRED: "Workplace not confirmed reachable",
    REMOTE_UNVERIFIED: "Remote work not confirmed",
    HYBRID_ADMIN_ROLE: "Admin/support mixed with developer role",
    DATA_REVIEW_REQUIRED: "Key facts missing",
    ROLE_UNCLEAR: "Title has no clear IT signal",
    STATED_GAP: "Posting states a gap",
    LANGUAGE_REVIEW: "German level to check",
}

_GENERIC_CLARIFY = "Read the posting and decide; the reason says what is open."
# A title with one of these is an administration/support (or mixed) role; it never belongs to the
# Junior Python/Backend group, in SQL and in Python alike.
_ADMIN_WORDS = ("admin", "support", "helpdesk", "service desk", "servicedesk", "betreuer")
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
    location: str = ""
    # CLEAR | IN_PREPARATION | POSSIBLE_DUPLICATE: an existing Bewerbung that touches this
    # vacancy (ALREADY_APPLIED never reaches the queue)
    duplicate_status: str = "CLEAR"


def is_junior_python_title(title: str) -> bool:
    """Plain substring test on purpose: the UI selects this group in SQL (`ilike`) and the
    two sides have to agree exactly."""
    text = (title or "").casefold()
    return (
        any(w in text for w in _JUNIOR_WORDS)
        and any(w in text for w in _TECH_WORDS)
        and not any(w in text for w in _ADMIN_WORDS)
    )


def junior_python_words() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return _JUNIOR_WORDS, _TECH_WORDS, _ADMIN_WORDS


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
    elif assessment.fit_tier in AUTO_PREPARE_TIERS and (
        (
            triage.category == "PRIMARY"
            and (
                _status(assessment, "work_time") != "FULL_TIME"
                or not work_time_evidence_confirmed(assessment)
            )
        )
        # Stage 60: an ADJACENT role that read as full time was gate-open before and is now
        # held only for missing evidence; it must not vanish from every list either
        or (
            triage.category == "ADJACENT"
            and _status(assessment, "work_time") == "FULL_TIME"
            and not work_time_evidence_confirmed(assessment)
        )
    ):
        # the gate holds it (`WORK_TIME_NOT_CONFIRMED`): a suitable developer role must not
        # vanish from every list because its hours are not stated
        code = WORK_TIME_NOT_CONFIRMED
        reason = f"working time is {_status(assessment, 'work_time')}, not confirmed full time"
        if _status(assessment, "work_time") == "FULL_TIME":
            reason = "full time is only a text reading; no structured evidence (Stage 60)"
    elif (
        triage.category == "EXCLUDED"
        and triage.code == NOT_TARGET_ROLE
        and (
            job.recommendation == NEEDS_ENRICHMENT
            or _status(assessment, "skills") == "NOT_EVALUATED"
        )
        and is_search_target_title(job.title)
    ):
        # triage excluded it only because there is no skill evidence (missing or outdated
        # analysis, or a description that is too short): unknown, not unsuitable
        code = NEEDS_ENRICHMENT
        reason = "skill evidence is missing; nothing is known about skills, hours or place"
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
        location=getattr(job, "location", "") or "",
        duplicate_status=_status(assessment, "duplicate"),
    )


_FAMILY_RANK = {"DEVELOPER": 0, "ADJACENT": 1, "OTHER": 2, "HYBRID_ADMIN": 3}
_FORMAT_RANK = {"FULL_REMOTE": 0, "HYBRID": 1}
PROFESSIONS = ("junior", "developer", "adjacent", "other", "hybrid_admin")


def in_profession(item: ReviewItem, profession: str) -> bool:
    if profession == "junior":
        return item.junior_python
    return not profession or item.family == profession.upper()


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
