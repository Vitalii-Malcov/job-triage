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

from dataclasses import dataclass, field
from typing import Literal

from app.services.job_fit_assessment import FitAssessment

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
