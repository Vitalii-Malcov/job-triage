"""Stage 11B: a small, isolated title-based ROLE RELEVANCE matcher.

Generic and profile-driven -- no company blacklist, no vacancy-specific
rules, no hardcoding for any one candidate. Two independent, conservative
questions, mirroring app.agents.seniority_classifier's own shape exactly
(same fail-open philosophy, same explicit-allowlist lesson from Stage
11A's S11A-001 rather than a naive/generic substring scan):

1. `derive_candidate_target_domain` -- does the candidate's own
   `CandidateProfile.target_roles` (Stage 6A) UNANIMOUSLY name a
   software-development role family? Returns UNKNOWN whenever this can't
   be determined conservatively (no target_roles at all, or ANY role
   whose own title isn't itself classified RELEVANT below).

2. `classify_title_relevance` -- does a job's own TITLE confidently
   belong to, or confidently NOT belong to, the software-development role
   family?

Both are consumed by `app.services.collector_runner._score_for_posting_type`
to force an already-APPLY/MAYBE-scored job back to SKIP when a
software-development-targeting candidate is looking at a confidently
IRRELEVANT-titled posting -- mirroring the existing Stage 10
(`app.agents.posting_classifier`) and Stage 11A
(`app.agents.seniority_classifier`) exclusion pattern exactly (same
forced-SKIP/zero-score shape, no new magic score threshold). Stage 11A's
seniority logic, `posting_type` classification, and the ordinary
JobScorer math are all untouched by this module.

**Role relevance is based on ROLE words, not technology words.**
Standalone technology mentions ("Python", "Backend", "MongoDB", ...) are
NOT by themselves relevant-role signals -- "Python Systemadministrator"
and "Backend Administrator" are exactly the titles this precision fix
(post-review hardening) exists to keep OUT of RELEVANT: a technology word
describes WHAT the role touches, not WHETHER the role itself is a
software-development role. Only genuine role-family words (Developer/
Entwickler, Software Engineer, AI Engineer, ...) count as positive
signals -- "Python Engineer"/"Backend Engineer" are legitimately UNKNOWN
(bare "Engineer" alone is not a listed role-family signal either), not a
regression: UNKNOWN fails open, exactly as intended.

**Positive and irrelevant signals are evaluated INDEPENDENTLY, not as a
first-match-wins ordered scan.** Both signal sets are checked against the
full title before any decision is made:

    positive only      -> RELEVANT
    irrelevant only     -> IRRELEVANT
    positive AND irrelevant -> UNKNOWN (genuinely mixed signal, e.g.
                                "Presales Software Engineer",
                                "QGIS Developer")
    neither             -> UNKNOWN

This is deliberately NOT "RELEVANT always wins" -- that policy is what
let a title like "Python Presales Consultant" slip through purely
because it also matched a technology word. A title carrying BOTH a real
role-family signal and a real irrelevant-family signal is ambiguous by
construction and must fail open (UNKNOWN), not be resolved by pattern
list order.

**Explicit-signal, title-scoped, whole-word only -- same S11A-001 lesson
applied from the start here, not learned the hard way a second time.**
Description text is never inspected. Every pattern is a whole-word (or
whole-compound-word, via an explicit closed allowlist for German
"-entwickler(in)" compounds) regex, never a bare substring check.
"""

import re
from dataclasses import dataclass
from typing import Literal

CandidateTargetDomain = Literal["SOFTWARE_DEVELOPMENT", "UNKNOWN"]
TitleRelevanceLevel = Literal["RELEVANT", "IRRELEVANT", "UNKNOWN"]

# S11A-001-style explicit allowlist (not a generic "\w*entwickler" suffix
# scan) for German "-entwickler"/"-entwicklerin" compounds -- covers the
# required "Anwendungsentwickler" case plus the other common, unambiguous
# software-development compounds. Standalone "Entwickler"/"Entwicklerin"
# is included here too so "Python Entwickler" and "KI-Entwickler" (the
# hyphen already gives a word boundary before "Entwickler") both match
# via this one pattern.
_ENTWICKLER_ALLOWLIST_WORDS = (
    "entwickler",
    "entwicklerin",
    "softwareentwickler",
    "softwareentwicklerin",
    "anwendungsentwickler",
    "anwendungsentwicklerin",
    "webentwickler",
    "webentwicklerin",
    "backendentwickler",
    "backendentwicklerin",
    "frontendentwickler",
    "frontendentwicklerin",
    "fullstackentwickler",
    "fullstackentwicklerin",
)
_ENTWICKLER_PATTERN = r"\b(?:" + "|".join(_ENTWICKLER_ALLOWLIST_WORDS) + r")\b"

# ROLE-FAMILY words that confidently indicate a software-development
# role -- deliberately NOT technology/tool words ("Python", "Backend",
# "MongoDB", ...), which describe subject matter, not role family. See
# module docstring for why standalone "python"/"backend" were removed.
_RELEVANT_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("developer", r"\bdeveloper\b"),
        ("software-engineer", r"\bsoftware\s+engineer\b"),
        ("ai-engineer", r"\bai\s+engineer\b"),
        ("entwickler", _ENTWICKLER_PATTERN),
    )
)

# Explicit, closed allowlist of title families clearly outside software
# development -- deliberately narrow and specific (not broad words like
# bare "Berater"/"Consultant"/"Ingenieur", which also legitimately appear
# in real software-development titles). Evaluated independently of the
# relevant-signal scan above -- see module docstring's conflict policy.
_IRRELEVANT_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("personalcontroller", r"\bpersonalcontroller(?:in)?\b"),
        ("systemadministrator", r"\bsystemadministrator(?:in)?\b"),
        ("administrator", r"\badministrator(?:in)?\b"),
        ("qgis", r"\bqgis\b"),
        ("presales", r"\bpresales\b"),
        ("projektmanagement", r"\bprojektmanagement\b"),
        ("elektrotechnik", r"\belektrotechnik\b"),
        # "Wissenschaftliche(r) Mitarbeiter(in)" -- the standard German
        # academic-research-assistant title family (Stage 11 pilot false
        # positives: Uniklinikum Frankfurt, Statistisches Bundesamt).
        ("wissenschaftliche-mitarbeiter", r"\bwissenschaftliche[rn]?\b"),
    )
)


@dataclass(frozen=True)
class TitleRelevanceClassification:
    level: TitleRelevanceLevel
    # The specific signal name matched (e.g. "developer", "qgis"), or
    # None for UNKNOWN -- carried through to the caller's log line so a
    # relevance-based exclusion is auditable down to the exact matched
    # word, not just a boolean.
    matched_signal: str | None


def classify_title_relevance(title: str) -> TitleRelevanceClassification:
    """Whole-word/whole-compound title scan for software-development role
    relevance -- see the module docstring for the independent-evaluation
    conflict policy (positive-only -> RELEVANT, irrelevant-only ->
    IRRELEVANT, both or neither -> UNKNOWN, fail-open).
    """
    text = title or ""

    positive_signal = next(
        (name for name, pattern in _RELEVANT_TITLE_PATTERNS if pattern.search(text)), None
    )
    irrelevant_signal = next(
        (name for name, pattern in _IRRELEVANT_TITLE_PATTERNS if pattern.search(text)), None
    )

    if positive_signal is not None and irrelevant_signal is None:
        return TitleRelevanceClassification(level="RELEVANT", matched_signal=positive_signal)
    if irrelevant_signal is not None and positive_signal is None:
        return TitleRelevanceClassification(level="IRRELEVANT", matched_signal=irrelevant_signal)
    return TitleRelevanceClassification(level="UNKNOWN", matched_signal=None)


def derive_candidate_target_domain(target_roles: list[str]) -> CandidateTargetDomain:
    """Conservative by design, mirroring
    app.agents.seniority_classifier.derive_candidate_target_seniority's
    S11A-002 fix exactly: SOFTWARE_DEVELOPMENT only if EVERY target role
    itself classifies as RELEVANT -- fails open (UNKNOWN) on any
    ambiguity, including an empty list or a role classify_title_relevance
    can't confidently place.
    """
    if not target_roles:
        return "UNKNOWN"
    if all(classify_title_relevance(role).level == "RELEVANT" for role in target_roles):
        return "SOFTWARE_DEVELOPMENT"
    return "UNKNOWN"
