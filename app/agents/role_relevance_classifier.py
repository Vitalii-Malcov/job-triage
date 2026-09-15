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
software-development role. Only genuine role-family words/phrases
(Python/Backend/Frontend/Fullstack/Web/Software Developer, Entwickler,
Software Engineer, AI Engineer, ...) count as positive signals --
"Python Engineer"/"Backend Engineer" are legitimately UNKNOWN (bare
"Engineer" alone is not a listed role-family signal either), not a
regression: UNKNOWN fails open, exactly as intended.

**S11B-002: bare "Developer" is ALSO not a sufficient positive signal on
its own.** "Business Developer", "Senior Business Developer", "Property
Developer", and "Real Estate Developer" are real, common job titles
where "Developer" means a business/deal originator or a real-estate
builder, not a software role. "Developer" only counts as a positive
signal via an explicit closed allowlist of software-development phrases
(`_DEVELOPER_PHRASE_PATTERNS`) -- the same S11A-001 allowlist lesson
applied one level up, not a generic "any word + Developer" scan.

**Positive and irrelevant signals are evaluated INDEPENDENTLY, not as a
first-match-wins ordered scan.** All signal sets are checked against the
full title before any decision is made:

    positive only                 -> RELEVANT
    no positive, STRONG irrelevant -> IRRELEVANT
    no positive, only WEAK ambiguity (no STRONG) -> UNKNOWN
    positive AND (STRONG or WEAK) -> UNKNOWN (genuinely mixed signal,
                                     e.g. "Presales Software Engineer",
                                     "Software Engineer Elektrotechnik")
    neither                        -> UNKNOWN

This is deliberately NOT "RELEVANT always wins" -- that policy is what
let a title like "Python Presales Consultant" slip through purely
because it also matched a technology word. A title carrying BOTH a real
role-family signal and a real irrelevant-family signal is ambiguous by
construction and must fail open (UNKNOWN), not be resolved by pattern
list order.

**S11B-003: STRONG (role-phrase) vs. WEAK (bare domain word) irrelevant
signals.** A bare domain/technology word ("qgis", "elektrotechnik",
"projektmanagement") is deliberately too weak to classify IRRELEVANT on
its own -- "QGIS Developer", "Software Engineer Elektrotechnik", and
"Python Engineer Projektmanagement" must not be rejected merely because
a domain word appears in an otherwise plausible or ambiguous technical
title. Only an explicit non-software ROLE phrase ("QGIS Expert(in)",
"Ingenieur Elektrotechnik", "Elektroingenieur(in)", "Elektrotechniker
(in)", "Projektmanager(in)", "Berater (im) Projektmanagement") is a
STRONG signal, confidently IRRELEVANT by itself. See
`_WEAK_AMBIGUITY_PATTERNS` and `classify_title_relevance`'s truth table.

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

# ROLE-FAMILY words/phrases that confidently indicate a software-
# development role -- deliberately NOT technology/tool words ("Python",
# "Backend", "MongoDB", ...) and, per S11B-002, deliberately NOT bare
# "Developer" either: "Business Developer", "Property Developer", and
# "Real Estate Developer" are real, common non-software job titles where
# "Developer" means something else entirely (a business/deal originator,
# a real-estate builder). "Developer" only counts as a positive signal
# when paired with an explicit software-development context word in the
# SAME phrase -- an closed allowlist of complete phrases, not a generic
# "any word + Developer" scan (which would just reintroduce the same
# over-broad-word problem one level up).
_DEVELOPER_PHRASE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("python-developer", r"\bpython\s+developer\b"),
    ("backend-developer", r"\bbackend\s+developer\b"),
    ("frontend-developer", r"\bfront[\s-]?end\s+developer\b"),
    ("fullstack-developer", r"\bfull[\s-]?stack\s+developer\b"),
    ("web-developer", r"\bweb\s+developer\b"),
    ("software-developer", r"\bsoftware\s+developer\b"),
)

_RELEVANT_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        *_DEVELOPER_PHRASE_PATTERNS,
        ("software-engineer", r"\bsoftware\s+engineer\b"),
        ("ai-engineer", r"\bai\s+engineer\b"),
        ("entwickler", _ENTWICKLER_PATTERN),
    )
)

# Explicit, closed allowlist of title families clearly outside software
# development -- deliberately narrow and specific (not broad words like
# bare "Berater"/"Consultant"/"Ingenieur", which also legitimately appear
# in real software-development titles). STRONG: alone (no positive
# signal present) is enough to classify IRRELEVANT outright. Evaluated
# independently of the relevant-signal scan above -- see module
# docstring's conflict policy.
_IRRELEVANT_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("personalcontroller", r"\bpersonalcontroller(?:in)?\b"),
        ("systemadministrator", r"\bsystemadministrator(?:in)?\b"),
        ("administrator", r"\badministrator(?:in)?\b"),
        ("presales", r"\bpresales\b"),
        # S11B-003/S11B-004: explicit ROLE PHRASES, not the bare domain
        # word "qgis" alone -- "QGIS Developer"/"QGIS Python Engineer"/
        # "QGIS Software Engineer" must not be forced IRRELEVANT merely
        # because a GIS tool is named; only an explicit non-dev role
        # attached to it (Expert(e/in)/Spezialist(in)) is confidently
        # irrelevant. `[\s-]+` (not just `\s+`) covers both the spaced
        # ("QGIS Experte") and hyphenated ("QGIS-Experte") real-world
        # connector forms.
        ("qgis-expert", r"\bqgis[\s-]+(?:expert(?:e|in)?|spezialist(?:in)?)\b"),
        # S11B-003/S11B-004: explicit ROLE PHRASES, not bare
        # "elektrotechnik" -- "Software Engineer Elektrotechnik"/"Python
        # Engineer Elektrotechnik" must not be forced IRRELEVANT merely
        # because the electrical-engineering DOMAIN is named; only an
        # explicit electrical-engineering ROLE title is confidently
        # irrelevant. `ingenieur(?:in)?` covers the feminine
        # "Ingenieurin" form; the optional `(?:f(?:ü|ue)r\s+)?` covers
        # both "Ingenieur Elektrotechnik" (no connector) and "Ingenieur
        # für/fuer Elektrotechnik" (explicit connector -- "fuer" is the
        # ASCII transliteration of "für", "ü" -> "ue", not a single-
        # character substitution, so `f[üu]r` alone would NOT match it).
        ("ingenieur-elektrotechnik", r"\bingenieur(?:in)?\s+(?:f(?:ü|ue)r\s+)?elektrotechnik\b"),
        ("elektroingenieur", r"\belektroingenieur(?:in)?\b"),
        ("elektrotechniker", r"\belektrotechniker(?:in)?\b"),
        # S11B-003/S11B-004: explicit ROLE PHRASES, not bare
        # "projektmanagement" -- "Software Developer Projektmanagement
        # Tools"/"Python Engineer Projektmanagement" must not be forced
        # IRRELEVANT merely because the project-management DOMAIN is
        # named; only an explicit project-management ROLE title is
        # confidently irrelevant. `berater(?:in)?` covers the feminine
        # "Beraterin" form, and the optional `(?:im\s+)?` covers both
        # "Berater Projektmanagement" and "Berater im Projektmanagement".
        ("projektmanager", r"\bprojektmanager(?:in)?\b"),
        ("berater-projektmanagement", r"\bberater(?:in)?\s+(?:im\s+)?projektmanagement\b"),
        # S11B-001: "Wissenschaftliche(r) Mitarbeiter(in)" -- the standard
        # German academic-research-ASSISTANT title family (Stage 11 pilot
        # false positives: Uniklinikum Frankfurt, Statistisches
        # Bundesamt). Requires the explicit ROLE noun "Mitarbeiter(in)"
        # to actually be present, not merely the adjective
        # "wissenschaftlich..." alone -- the adjective alone also
        # legitimately modifies real software-development titles
        # ("Wissenschaftlicher Programmierer", "Wissenschaftlicher
        # Softwarearchitekt", "Wissenschaftlicher Data Engineer",
        # "Wissenschaftliche Hilfskraft Softwareentwicklung"), none of
        # which say "Mitarbeiter". A short bounded gap (<=15 chars)
        # between the two words tolerates the real pilot titles' messy
        # gender-inflection punctuation ("Wissenschaftliche/r
        # Mitarbeiterin", "Wissenschaftliche/-r Mitarbeiter/-in",
        # "Wissenschaftliche*r Mitarbeiter*in") without becoming a
        # generic "wissenschaftlich...anything...mitarbeiter" scan that
        # could bridge two unrelated words in a longer title.
        ("wissenschaftliche-mitarbeiter", r"\bwissenschaftlich[a-z]*\b.{0,15}?\bmitarbeiter"),
    )
)

# S11B-003: WEAK ambiguity signals -- bare domain/technology words that
# must NEVER, on their own (no positive signal, no STRONG irrelevant
# phrase present), be enough to classify IRRELEVANT ("do NOT reject
# solely because 'Elektrotechnik' appears somewhere in an otherwise
# ambiguous technical title"). They still participate in the conflict
# check: a title carrying BOTH a real positive role-phrase AND one of
# these bare domain words is genuinely ambiguous ("Software Engineer
# Elektrotechnik", "Software Developer Projektmanagement Tools") and
# must fail open to UNKNOWN rather than resolve to RELEVANT purely on
# the strength of the positive phrase. Bare-alone (no positive, no
# STRONG phrase) also resolves to UNKNOWN, never IRRELEVANT -- see
# classify_title_relevance's truth table.
_WEAK_AMBIGUITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("qgis", r"\bqgis\b"),
        ("elektrotechnik", r"\belektrotechnik\b"),
        ("projektmanagement", r"\bprojektmanagement\b"),
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
    relevance. Three signal sets, evaluated independently (never a
    first-match-wins ordered scan):

        positive_signal   -- a genuine software-development role phrase
        strong_signal      -- a genuine non-software ROLE phrase, e.g.
                              "QGIS Expert", "Ingenieur Elektrotechnik"
        weak_signal         -- a bare domain/technology word, e.g. "qgis",
                              "elektrotechnik" (S11B-003)

    Truth table:

        positive, no strong, no weak -> RELEVANT
        no positive, strong          -> IRRELEVANT
        no positive, no strong, weak -> UNKNOWN  (bare domain word alone
                                                    is not enough)
        positive AND (strong OR weak) -> UNKNOWN (genuinely mixed)
        neither                       -> UNKNOWN

    See the module docstring for the full rationale.
    """
    text = title or ""

    positive_signal = next(
        (name for name, pattern in _RELEVANT_TITLE_PATTERNS if pattern.search(text)), None
    )
    strong_signal = next(
        (name for name, pattern in _IRRELEVANT_TITLE_PATTERNS if pattern.search(text)), None
    )
    weak_signal = next(
        (name for name, pattern in _WEAK_AMBIGUITY_PATTERNS if pattern.search(text)), None
    )
    any_irrelevant_signal = strong_signal is not None or weak_signal is not None

    if positive_signal is not None and not any_irrelevant_signal:
        return TitleRelevanceClassification(level="RELEVANT", matched_signal=positive_signal)
    if strong_signal is not None and positive_signal is None:
        return TitleRelevanceClassification(level="IRRELEVANT", matched_signal=strong_signal)
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
