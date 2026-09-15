"""Stage 11C: a small, isolated STRUCTURED-EVIDENCE-QUALITY classifier.

Generic and profile-independent -- no company/title-specific rules, no
hardcoding for any one vacancy. Answers exactly one narrow question: is
the STRUCTURED skill evidence extracted for a posting (must-have +
nice-to-have skill counts) thin enough that the ordinary JobScorer
math's SKIP outcome should not be trusted as a confident negative?

**Principle: strong absence of evidence is not evidence of mismatch.**
A posting with ZERO or ONE total extracted structured skill signals
(must-have + nice-to-have combined) hasn't given the scorer enough to
work with -- JobScorer's own `must_score`/`nice_score` neutral defaults
(0.5) for an empty set already acknowledge this per-component, but the
combined weighted formula can still land under the MAYBE floor (60)
purely from that thinness, not from any genuine, identified mismatch.

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

The SPARSE_EVIDENCE_THRESHOLD (2) mirrors
`app.agents.job_scorer.MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY`'s own
established precedent (Stage 10): that constant already treats fewer
than 2 must-have signals as too thin to trust for a confident APPLY.
This module applies the SAME threshold symmetrically -- to the combined
must+nice total, not must-have alone, since an extracted nice-to-have
is also genuine structured evidence -- for a confident SKIP.
"""

from typing import Literal

EvidenceQualityLevel = Literal["SUFFICIENT", "SPARSE"]

SPARSE_EVIDENCE_THRESHOLD = 2


def classify_evidence_quality(
    must_have_total: int, nice_to_have_total: int
) -> EvidenceQualityLevel:
    """Pure count-based classification -- callers pass in the TOTAL
    must-have signal count (matched + missing, i.e. the full extracted
    must-have set size JobScorer itself computed) and the total
    nice-to-have signal count, not skill names. See the module docstring
    for why the threshold is a combined total, not must-have alone.
    """
    total = must_have_total + nice_to_have_total
    if total < SPARSE_EVIDENCE_THRESHOLD:
        return "SPARSE"
    return "SUFFICIENT"
