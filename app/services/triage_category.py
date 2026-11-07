"""Triage category: the professional direction of a vacancy, kept apart from
how complete our data about it is.

`FitAssessment.fit_tier` answers "may the pipeline prepare this on its own?".
This module answers a different question for the operator and for ranking:
"what kind of vacancy is it, and why is it held?".

- PRIMARY       : a Python / backend / software developer role the candidate can
                  pursue now (tier GOOD_MATCH or PARTIAL_MATCH).
- ADJACENT      : a real but non-target role (data, DevOps, automation ...) with
                  usable skill evidence. Shown separately, ranked after PRIMARY.
- MANUAL_REVIEW : a possibly suitable role with open facts (an unconfirmed car
                  route, missing data), a stated gap the operator may still accept, or a
                  hybrid administration/developer title. Stays visible with its reason; it is never
                  prepared automatically and an unknown route never becomes consent.
- EXCLUDED      : a confirmed barrier (administration, support, senior, out of
                  reach ...) or a role with no developer signal.

Pure and read-only: no database access, no status change, no sending. The
category never replaces the fit tier -- `shortlist_gate.AUTO_PREPARE_TIERS` stays
the only rule that admits automatic preparation.
"""

from dataclasses import dataclass
from typing import Literal

from app.agents.target_role import hybrid_support_admin_family, is_developer_title
from app.services.job_fit_assessment import FitAssessment

Category = Literal["PRIMARY", "ADJACENT", "MANUAL_REVIEW", "EXCLUDED"]

# Why a MANUAL_REVIEW vacancy is held. ROUTE_REVIEW_REQUIRED is a derived label, not stored.
ROUTE_REVIEW_REQUIRED = "ROUTE_REVIEW_REQUIRED"
DATA_REVIEW_REQUIRED = "DATA_REVIEW_REQUIRED"
ROLE_UNCLEAR = "ROLE_UNCLEAR"
NOT_TARGET_ROLE = "NOT_TARGET_ROLE"
CONFIRMED_BARRIER = "CONFIRMED_BARRIER"
HYBRID_ADMIN_ROLE = "HYBRID_ADMIN_ROLE"
STATED_GAP = "STATED_GAP"

# configured role statuses (or none configured): a title outside them still counts as adjacent
# when the skills match
_CONFIGURED_ROLES = frozenset({"PRIMARY", "SECONDARY", "OTHER", "NOT_CONFIGURED"})
_SKILL_EVIDENCE = frozenset({"STRONG", "PARTIAL"})
# Location outcomes that leave the car route open (the tier sends them to the operator).
_ROUTE_OPEN = frozenset(
    {
        "DISTANCE_UNVERIFIED",
        "LOCATION_UNVERIFIED",
        "APPROXIMATE_NEAR_LIMIT",
        "ESTIMATE_NEAR_LIMIT",
        "ESTIMATE_BEYOND_RELOCATION",
        "UNKNOWN",
        "NOT_CONFIGURED",
    }
)

# Lower is better; the shortlist ranks PRIMARY before ADJACENT whatever the match score.
_RANK = {"PRIMARY": 0, "ADJACENT": 1, "MANUAL_REVIEW": 2, "EXCLUDED": 3}
UNKNOWN_RANK = 1


@dataclass(frozen=True)
class TriageCategory:
    category: Category
    code: str
    reason: str

    @property
    def rank(self) -> int:
        return _RANK[self.category]


def classify(assessment: FitAssessment) -> TriageCategory:
    tier = assessment.fit_tier
    status = {c.name: c.status for c in assessment.criteria}
    role = status.get("role")
    skills_ok = status.get("skills") in _SKILL_EVIDENCE

    if tier == "NOT_SUITABLE":
        first = assessment.tier_reasons[0] if assessment.tier_reasons else "confirmed barrier"
        return TriageCategory("EXCLUDED", CONFIRMED_BARRIER, first)

    family = hybrid_support_admin_family(assessment.title)
    if family is not None:
        return TriageCategory(
            "MANUAL_REVIEW",
            HYBRID_ADMIN_ROLE,
            f"title mixes a {family} role with a developer role; the operator reads the posting",
        )

    # The professional direction comes from the TITLE, not from which role names happen to be
    # configured as priorities: DevOps or a data analyst stays ADJACENT even if configured.
    developer = is_developer_title(assessment.title)
    if developer or (role in _CONFIGURED_ROLES and skills_ok):
        if tier == "STRETCH":
            reason = assessment.tier_reasons[0] if assessment.tier_reasons else "stated gap"
            return TriageCategory("MANUAL_REVIEW", STATED_GAP, reason)
        if tier == "NEEDS_OPERATOR":
            return _manual(assessment, status)
        if developer:
            return TriageCategory("PRIMARY", f"TIER_{tier}", f"developer role, fit tier {tier}")
        return TriageCategory(
            "ADJACENT", f"TIER_{tier}", "not a developer title; skills match, ranked after PRIMARY"
        )

    if role == "NO_IT_SIGNAL" and skills_ok:
        return TriageCategory(
            "MANUAL_REVIEW", ROLE_UNCLEAR, "title has no IT signal; the operator decides"
        )
    return TriageCategory("EXCLUDED", NOT_TARGET_ROLE, f"role {role}, no usable skill evidence")


def _manual(assessment: FitAssessment, status: dict[str, str]) -> TriageCategory:
    reasons = assessment.tier_reasons
    first = reasons[0] if reasons else "facts to verify"
    if status.get("skills") == "WEAK":
        # the tier shows the open fact first; the operator must also see the skill gap
        first += " [skills WEAK: low required-skill coverage]"
    if status.get("location") in _ROUTE_OPEN:
        return TriageCategory("MANUAL_REVIEW", ROUTE_REVIEW_REQUIRED, first)
    return TriageCategory("MANUAL_REVIEW", DATA_REVIEW_REQUIRED, first)
