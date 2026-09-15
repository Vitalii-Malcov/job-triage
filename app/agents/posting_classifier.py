"""Generic, source-agnostic classification of whether a Job represents
genuine target employment eligible for the normal APPLY pipeline, or a
non-employment listing that must not receive an automatic APPLY
recommendation.

**Stage 10 shadow-mode pilot finding.** Bundesagentur's public "Jobsuche"
API mixes training-provider course offerings into the same keyword-search
result set as real job vacancies -- e.g. alfatraining Bildungszentrum
GmbH's "Programmierung mit Python"/"Python Advanced" courses scored APPLY
(91/100) under the legacy JobScorer purely because their thin,
single-skill extracted requirement set trivially satisfied a match (see
app.agents.job_scorer's MINIMUM_MUST_HAVE_SIGNALS_FOR_APPLY for the
companion evidence-sparsity fix).

**Stage 10 follow-up correction.** An earlier version of this module
treated `stellenangebotsart == "SELBSTAENDIGKEIT"` as ALWAYS a
non-employment course listing. Live verification against unrelated search
terms (`Handelsvertreter`, `Versicherungsmakler`, `Vertriebspartner`,
2026-09-15) disproved that: SELBSTAENDIGKEIT is Bundesagentur's genuine
"self-employed/freelance" employment category -- 26/30 "Vertriebspartner"
results and 8/30 "Handelsvertreter" results used it for real
commission-based sales/broker roles with zero relation to training. It is
now preference-gated (like AUSBILDUNG/PRAKTIKUM_TRAINEE below) rather than
unconditionally excluded -- the alfatraining course listings that
motivated this module are still excluded by default (no FREELANCE
preference set), but a candidate who has explicitly opted into freelance
work is no longer blocked from genuine self-employed openings.

Classification never inspects company name -- a training PROVIDER's own
real job openings (e.g. an administrator role AT a Bildungszentrum
itself) must still score normally, and a company whose name happens to
contain educational wording must never be excluded on that basis alone.
Signal is drawn from, in order:

1. The source's own structured posting-type field, when present
   (currently: Bundesagentur's `stellenangebotsart`, passed through as
   `Job.posting_type`). Most reliable -- set by the employer/poster at
   submission time, not inferred from free text.
2. A conservative, whole-word title pattern (German training/course
   vocabulary) as a fallback for sources with no structured type, or a
   posting mislabeled upstream. Deliberately title-only (never the full
   description) and whole-word (so German compounds like
   "Schülerkurse" -- a real trainer job title -- never false-positive
   purely by containing "kurs" as a substring).
"""

import re
from dataclasses import dataclass

# Bundesagentur "stellenangebotsart" values observed live (2026-09-15,
# keyword="Python", Frankfurt am Main +50km, 328 postings across 3 pages):
# ARBEIT 201, PRAKTIKUM_TRAINEE 16, SELBSTAENDIGKEIT 6, AUSBILDUNG 5.
# Unofficial, undocumented API -- see app/collectors/bundesagentur.py's
# module docstring for the same "verified live, not from stale community
# docs" caveat; this vocabulary may not be exhaustive.
#
# No posting type is unconditionally/always excluded -- every non-ARBEIT
# type observed is GENUINE employment (self-employment, apprenticeship,
# internship), just not necessarily the type this tool's candidate is
# targeting. "Obviously a course, not a job at all" is caught separately,
# below, by title pattern -- never by posting_type alone (see the
# SELBSTAENDIGKEIT correction above).

# Each entry: posting_type -> the CandidateJobPreferences.employment_types
# (Stage 6A) value that must be present for a posting of that type to
# enter the normal APPLY pipeline. Absent/empty preferences conservatively
# exclude all three.
POSTING_TYPE_REQUIRES_PREFERENCE: dict[str, str] = {
    "AUSBILDUNG": "APPRENTICESHIP",
    "PRAKTIKUM_TRAINEE": "INTERNSHIP",
    "SELBSTAENDIGKEIT": "FREELANCE",
}

_COURSE_TITLE_PATTERN = re.compile(
    r"\b(weiterbildung|schulung|kurs|lehrgang|qualifizierung)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class PostingClassification:
    is_target_employment: bool
    # Machine-readable reason (never free text from the posting itself) --
    # safe to log privacy-wise, same convention as the rest of this
    # project's structured logging.
    excluded_reason: str | None


def classify_posting(
    *,
    title: str,
    posting_type: str | None,
    allowed_employment_types: frozenset[str] = frozenset(),
) -> PostingClassification:
    """Decide whether `title`/`posting_type` represents target employment.

    `allowed_employment_types` is the candidate's own stated
    CandidateJobPreferences.employment_types (Stage 6A) -- pass an empty
    frozenset (the default) when no preference is known/available, which
    conservatively excludes AUSBILDUNG/PRAKTIKUM_TRAINEE/SELBSTAENDIGKEIT
    postings.
    """
    required_preference = POSTING_TYPE_REQUIRES_PREFERENCE.get(posting_type or "")
    if required_preference is not None and required_preference not in allowed_employment_types:
        return PostingClassification(False, f"posting_type_requires_preference:{posting_type}")

    if _COURSE_TITLE_PATTERN.search(title or ""):
        return PostingClassification(False, "course_title_pattern")

    return PostingClassification(True, None)
