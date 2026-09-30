"""Stage 11E: a small, isolated UNIQUE-EVIDENCE-CARDINALITY classifier.

Generic and profile-independent -- no company/title-specific rules, no
hardcoding for any one vacancy. Answers exactly one narrow question: how
many DISTINCT (normalized, deduplicated) structured skill signals
actually support a job's recommendation?

**Category entries are not evidence identities.** Stage 11E's own
pre-implementation analysis found that `must_score`/`nice_score`'s
cardinality (`len(must)`, `len(nice)`) can double-count a SINGLE piece
of underlying evidence: the SAME skill can appear in both
`must_have_skills` and `nice_to_have_skills` (a live example found in
the Stage 11 pilot snapshot: "Data Consultant" resolved `{"python"}` as
its must-have evidence while `nice_to_have_skills` was ALSO `["python"]`
-- two category entries, one real signal), or under two different
textual aliases of one skill ("REST API" / "rest"). Naively summing
`len(must) + len(nice)` counts those as 2; this module counts them as 1.

This module never re-derives JobScorer's OWN must-have resolution logic
(Stage 11E is explicitly forbidden from touching or duplicating it).
Callers pass in the ALREADY-RESOLVED evidence:
`JobScore.matched_must_have + JobScore.missing_must_have` (the exact set
JobScorer itself decided the job's must-have requirements to be) and the
job's own `nice_to_have_skills`. This module's only job is to normalize
(reusing `app.agents.job_scorer.normalize_skill`, not a new
normalization layer) and deduplicate ACROSS those two collections.

**Consumed by `app.services.collector_runner._score_for_posting_type`**
to force an already-APPLY/MAYBE-scored job back to SKIP when fewer than
2 distinct evidence signals support it -- positioned AFTER Stage 11A
(seniority) and Stage 11B (role relevance), so their own specific,
audit-logged exclusions always fire first, and BEFORE Stage 11C
(sparse-evidence rescue), so a genuinely relevant, thin-evidence
software-development posting can still be rescued by that already-
approved, independently-gated mechanism. Mirrors the existing Stage 10/
11A/11B/11C pattern exactly: this module classifies, `collector_runner`
decides and logs -- no side effects here.
"""

from collections.abc import Collection
from typing import Literal

from app.agents.job_scorer import normalize_skill

EvidenceCardinalityLevel = Literal["SUFFICIENT", "LOW_CARDINALITY"]

# Mirrors app.agents.job_scorer.MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY's own
# established precedent (Stage 10) -- extended here from must-have-only
# to the combined DISTINCT must+nice evidence set.
MINIMUM_UNIQUE_EVIDENCE_SIGNALS = 2


def unique_evidence_signals(
    resolved_must_evidence: Collection[str], nice_to_have_evidence: Collection[str]
) -> frozenset[str]:
    """The set of DISTINCT normalized skill identities across both
    collections -- the same skill named in both `resolved_must_evidence`
    and `nice_to_have_evidence` (or under two different textual aliases
    of the same normalized skill) counts once.
    """
    return frozenset(
        normalized
        for skill in (*resolved_must_evidence, *nice_to_have_evidence)
        if (normalized := normalize_skill(skill))
    )


def classify_evidence_cardinality(
    resolved_must_evidence: Collection[str], nice_to_have_evidence: Collection[str]
) -> EvidenceCardinalityLevel:
    """LOW_CARDINALITY if fewer than MINIMUM_UNIQUE_EVIDENCE_SIGNALS
    distinct evidence identities support the job; SUFFICIENT otherwise.
    """
    count = len(unique_evidence_signals(resolved_must_evidence, nice_to_have_evidence))
    if count < MINIMUM_UNIQUE_EVIDENCE_SIGNALS:
        return "LOW_CARDINALITY"
    return "SUFFICIENT"
