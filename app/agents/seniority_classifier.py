"""Stage 11A: a small, isolated title-seniority matcher.

Generic and profile-driven -- no company blacklist, no vacancy-specific
rules, no hardcoding for any one candidate. Two independent, conservative
questions, both answered from the SAME canonical signal vocabulary below:

1. `derive_candidate_target_seniority` -- does the candidate's own
   `CandidateProfile.target_roles` (Stage 6A) explicitly target JUNIOR
   roles, with no senior/lead-level target also present? Returns UNKNOWN
   whenever this can't be determined conservatively (no target_roles at
   all, or a mixed/ambiguous set) -- see that function's docstring for
   why "cannot determine" must never be treated as a rejection signal.

2. `classify_title_seniority` -- does a job's own TITLE contain an
   explicit senior/lead-level marker?

Both are consumed by `app.services.collector_runner._score_for_posting_type`
to force an already-APPLY/MAYBE-scored job back to SKIP when a junior-
targeting candidate is looking at an explicitly senior-titled posting --
mirroring `app.agents.posting_classifier`'s existing exclusion pattern
exactly (same forced-SKIP/zero-score shape, no new magic score
threshold). `posting_type` classification itself is untouched by this
module.

**Explicit-signal, title-scoped, whole-word only.** Description text is
never inspected here -- a posting whose DESCRIPTION happens to mention
"senior" (e.g. "reports to a senior manager") must not be excluded, only
an explicit marker in the TITLE itself. Every pattern is a whole-word (or
whole-compound-word, for the German noun/suffix forms) regex, never a
bare substring check -- "Seniorenberater" (elder-care advisor),
"Leadership", and similar words that merely CONTAIN one of these markers
must not match; regex word boundaries (`\\b`) guarantee this rather than
`marker in title.casefold()`.
"""

import re
from dataclasses import dataclass
from typing import Literal

CandidateSeniorityTarget = Literal["JUNIOR", "UNKNOWN"]
TitleSeniorityLevel = Literal["SENIOR", "UNKNOWN"]

_JUNIOR_PATTERN = re.compile(r"\bjunior\b", re.IGNORECASE)

# German "-leiter"/"-leiterin" is a genuinely productive compounding
# suffix for leadership titles ("Abteilungsleiter" = department head,
# "Projektleiter" = project lead, "Entwicklungsleiter" = head of
# development, "Bereichsleiter" = division head, ...) -- an open-ended
# set no fixed prefix whitelist could enumerate generically. But
# "Leiter" is also a genuine German homonym for "conductor" (physics:
# "elektrischer Leiter"), which produces a small, closed set of
# well-known engineering/physics compound nouns that are NOT leadership
# titles: "Halbleiter" (semiconductor), "Supraleiter" (superconductor),
# "Ableiter"/"Blitzableiter" (arrester/lightning rod), "Nichtleiter"
# (insulator/non-conductor). These are real, common terms in German
# engineering job postings (e.g. "Halbleiter-Ingenieur") and must not be
# misread as a leadership signal merely because they end in "-leiter" --
# excluded by exact whole-word negative lookahead below, not by
# disabling compound matching altogether.
_LEITER_NON_ROLE_COMPOUNDS = (
    "halbleiter",
    "supraleiter",
    "ableiter",
    "blitzableiter",
    "nichtleiter",
)
_LEITER_COMPOUND_PATTERN = (
    r"\b(?!(?:" + "|".join(_LEITER_NON_ROLE_COMPOUNDS) + r")\b)\w*leiter(?:in)?\b"
)

# The single canonical senior/lead-level signal vocabulary, shared by
# both directions above (candidate target derivation AND job title
# classification) so the two checks can never silently drift apart.
# "teamleiter" is kept as its own explicit entry (ahead of the generic
# compound pattern below) purely so a Teamleiter title is attributed to
# that specific, more informative signal name in logs/tests -- the
# generic "leiter" pattern would also match it, but list order means the
# first match wins.
_SENIOR_LEVEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("senior", r"\bsenior\b"),
        ("lead", r"\blead\b"),
        ("principal", r"\bprincipal\b"),
        ("staff", r"\bstaff\b"),
        ("head", r"\bhead\b"),
        ("teamleiter", r"\bteamleiter\b"),
        ("leitung", r"\bleitung\b"),
        # Standalone "Leiter"/"Leiterin" AND any leadership compound
        # ending in "-leiter"/"-leiterin" (Abteilungsleiter,
        # Abteilungsleiterin, Projektleiter, Entwicklungsleiter,
        # Bereichsleiter, ...) -- except the closed set of non-role
        # physics/engineering homonyms above.
        ("leiter", _LEITER_COMPOUND_PATTERN),
    )
)


def _has_senior_level_signal(text: str) -> bool:
    return any(pattern.search(text or "") for _, pattern in _SENIOR_LEVEL_PATTERNS)


def derive_candidate_target_seniority(target_roles: list[str]) -> CandidateSeniorityTarget:
    """Conservative by design: JUNIOR only if at least one target role
    explicitly says "junior" AND no target role also carries a
    senior/lead-level signal.

    A candidate whose `target_roles` mixes "Junior Python Developer" with
    e.g. "Lead Python Developer" has not unambiguously stated a
    junior-only target -- returning UNKNOWN there (rather than guessing
    which target was meant) is what "if candidate target seniority cannot
    be determined: do not penalize" requires. An empty `target_roles`
    list is the same "cannot be determined" case.
    """
    if not target_roles:
        return "UNKNOWN"
    has_junior = any(_JUNIOR_PATTERN.search(role) for role in target_roles)
    has_senior = any(_has_senior_level_signal(role) for role in target_roles)
    if has_junior and not has_senior:
        return "JUNIOR"
    return "UNKNOWN"


@dataclass(frozen=True)
class TitleSeniorityClassification:
    level: TitleSeniorityLevel
    # The specific signal name matched (e.g. "senior", "teamleiter"), or
    # None for UNKNOWN -- carried through to the caller's log line so a
    # seniority-based exclusion is auditable down to the exact matched
    # word, not just a boolean.
    matched_signal: str | None


def classify_title_seniority(title: str) -> TitleSeniorityClassification:
    """Whole-word/whole-compound title scan for an explicit senior/lead-
    level marker -- see the module docstring for why this never inspects
    description text or does bare substring matching.
    """
    for name, pattern in _SENIOR_LEVEL_PATTERNS:
        if pattern.search(title or ""):
            return TitleSeniorityClassification(level="SENIOR", matched_signal=name)
    return TitleSeniorityClassification(level="UNKNOWN", matched_signal=None)
