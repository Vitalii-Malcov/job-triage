"""Stage 11C: a small, isolated STRUCTURED-EVIDENCE-QUALITY classifier.

Generic and profile-independent -- no company/title-specific rules, no
hardcoding for any one vacancy. Answers exactly one narrow question: is
the STRUCTURED skill evidence extracted for a posting (must-have +
nice-to-have, deduplicated) thin enough that the ordinary JobScorer
math's SKIP outcome should not be trusted as a confident negative?

**Principle: strong absence of evidence is not evidence of mismatch.**
A posting with ZERO or ONE total DISTINCT extracted structured skill
signals (must-have + nice-to-have combined) hasn't given the scorer
enough to work with -- JobScorer's own `must_score`/`nice_score` neutral
defaults (0.5) for an empty set already acknowledge this per-component,
but the combined weighted formula can still land under the MAYBE floor
(60) purely from that thinness, not from any genuine, identified
mismatch.

**This is deliberately NOT the same thing as a real skill gap.** A
posting with a well-populated must-have set (e.g. 3 items) where most
match and one genuinely doesn't (a real, specific gap the extractor
DID find) is evidence-based scoring working correctly, not sparse
evidence -- see `app.services.collector_runner`'s own docstring on how
this is wired for the concrete Stage 11 pilot example that draws this
line (Junior Cyber Security Developer: 3 must-haves extracted, 2
matched, 1 genuinely missing -- NOT sparse; JUNIOR SOFTWARE DEVELOPER
DACH: 0 must-haves AND 0 nice-to-haves extracted at all -- genuinely
sparse).

**M2 fix (Astra Stage 12 audit): DISTINCT evidence, not raw category
counts.** This module used to take two raw counts (must-have total,
nice-to-have total) and sum them -- so a single skill duplicated across
both categories (`must=[python], nice=[python]`) counted as 2 signals,
the SAME total as two genuinely different skills, incorrectly pushing
it out of SPARSE (and therefore out of Stage 11C's own rescue
eligibility) relative to the equivalent, NOT-more-evidenced
`must=[python], nice=[]` case (which correctly stayed SPARSE and was
rescued). Callers now pass in the single, already-deduplicated
evidence count -- the exact same
`app.agents.evidence_cardinality_classifier.unique_evidence_signals`
count Stage 11E already computes for its own (different) purpose, so
there is one dedup implementation, not two independently-drifting ones.

The SPARSE_EVIDENCE_THRESHOLD (2) mirrors
`app.agents.job_scorer.MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY`'s own
established precedent (Stage 10): that constant already treats fewer
than 2 must-have signals as too thin to trust for a confident APPLY.
This module applies the SAME threshold symmetrically -- to the combined,
deduplicated must+nice total, not must-have alone, since an extracted
nice-to-have is also genuine structured evidence -- for a confident
SKIP.
"""

from typing import Literal

EvidenceQualityLevel = Literal["SUFFICIENT", "SPARSE"]

SPARSE_EVIDENCE_THRESHOLD = 2


def classify_evidence_quality(unique_evidence_count: int) -> EvidenceQualityLevel:
    """Pure count-based classification -- callers pass in the TOTAL number
    of DISTINCT normalized structured evidence signals (must-have union
    nice-to-have, after cross-category dedup), not raw per-category
    counts and not skill names. See the module docstring for why this is
    a single deduplicated total, not must-have-count + nice-have-count.
    """
    if unique_evidence_count < SPARSE_EVIDENCE_THRESHOLD:
        return "SPARSE"
    return "SUFFICIENT"
