"""Stage 11A: a small, isolated title-seniority matcher.

Generic and profile-driven -- no company blacklist, no vacancy-specific
rules, no hardcoding for any one candidate. Two independent, conservative
questions, both answered from the SAME canonical signal vocabulary below:

1. `derive_candidate_target_seniority` -- does the candidate's own
   `CandidateProfile.target_roles` (Stage 6A) UNANIMOUSLY target JUNIOR
   roles? Returns UNKNOWN whenever this can't be determined conservatively
   (no target_roles at all, or ANY role that doesn't itself explicitly say
   "junior") -- see that function's docstring for why "cannot determine"
   must never be treated as a rejection signal.

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

**S11A-001 (Codex review): explicit positive allowlist, not a generic
suffix scan.** An earlier version matched ANY `\\w*leiter(?:in)?` and then
tried to deny-list known non-leadership homonyms as they were discovered
(Halbleiter, Wellenleiter, Flugbegleiter, ...) -- an open-ended, reactive
blacklist that new false positives (Stromleiter, Schutzleiter,
Neutralleiter, Außenleiter, Innenleiter, Phasenleiter, Kupferleiter, ...:
German electrical-engineering terms for "conductor", an entire word
family the denylist never anticipated) kept slipping through. **False
SENIOR is more dangerous than missed SENIOR** -- inverted here to a
closed, explicit positive allowlist of known organizational leadership
compounds, plus the standalone noun. An organizational compound not on
this list stays UNKNOWN rather than being guessed at; extend the list
deliberately, not by trying to out-enumerate every non-leadership
"-leiter" word instead.
"""

import re
from dataclasses import dataclass
from typing import Literal

CandidateSeniorityTarget = Literal["JUNIOR", "UNKNOWN"]
TitleSeniorityLevel = Literal["SENIOR", "UNKNOWN"]

_JUNIOR_PATTERN = re.compile(r"\bjunior\b", re.IGNORECASE)

# S11A-001: standalone "Leiter"/"Leiterin" plus an explicit, closed
# allowlist of organizational leadership compounds. Extend this list
# deliberately (a real title needing it, reviewed) -- never widen back to
# a generic "\w*leiter" suffix scan.
_LEITER_ALLOWLIST_WORDS = (
    "leiter",
    "leiterin",
    "teamleiter",
    "teamleiterin",
    "abteilungsleiter",
    "abteilungsleiterin",
    "bereichsleiter",
    "bereichsleiterin",
    "projektleiter",
    "projektleiterin",
    "entwicklungsleiter",
    "entwicklungsleiterin",
    "bauleiter",
    "bauleiterin",
)
_LEITER_PATTERN = r"\b(?:" + "|".join(_LEITER_ALLOWLIST_WORDS) + r")\b"

# The single canonical senior/lead-level signal vocabulary, shared by
# both directions above (candidate target derivation AND job title
# classification) so the two checks can never silently drift apart.
_SENIOR_LEVEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("senior", r"\bsenior\b"),
        ("lead", r"\blead\b"),
        ("principal", r"\bprincipal\b"),
        ("staff", r"\bstaff\b"),
        ("head", r"\bhead\b"),
        ("leitung", r"\bleitung\b"),
        ("leiter", _LEITER_PATTERN),
    )
)


def derive_candidate_target_seniority(target_roles: list[str]) -> CandidateSeniorityTarget:
    """S11A-002 (Codex review): JUNIOR only if EVERY target role
    explicitly says "junior" -- fails open (UNKNOWN) on any ambiguity,
    including a role that states no seniority level at all.

    An earlier version returned JUNIOR as long as AT LEAST ONE role said
    "junior" and none said senior -- too permissive: a candidate whose
    `target_roles` includes an unlabeled role like "Data Engineer"
    alongside "Junior Python Developer" has not unanimously stated a
    junior-only target, and "if candidate target seniority cannot be
    determined: do not penalize" means that ambiguity must resolve to
    UNKNOWN, not to a best-effort guess.

        ["Junior Python Developer", "Junior Backend Developer"] -> JUNIOR
        ["Junior Python Developer", "Data Engineer"]            -> UNKNOWN
        ["Junior Python Developer", "Senior Backend Developer"] -> UNKNOWN
        ["Python Backend Developer"]                            -> UNKNOWN
        []                                                      -> UNKNOWN
    """
    if not target_roles:
        return "UNKNOWN"
    if all(_JUNIOR_PATTERN.search(role) for role in target_roles):
        return "JUNIOR"
    return "UNKNOWN"


@dataclass(frozen=True)
class TitleSeniorityClassification:
    level: TitleSeniorityLevel
    # The specific signal name matched (e.g. "senior", "leiter"), or None
    # for UNKNOWN -- carried through to the caller's log line so a
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
