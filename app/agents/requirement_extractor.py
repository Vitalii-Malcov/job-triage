"""Deterministic extraction of LANGUAGE and EDUCATION requirements from a
job's title/description (Stage 6B).

Reuses app.agents.skill_extractor's segment/context-classification engine
(`_contextual_segments`, `_classify_mention`, `_MUST_HAVE_MARKERS`,
`_NICE_TO_HAVE_MARKERS`, `_PROFILE_REQUIREMENT_MARKERS`) rather than
duplicating it — the same must/nice/section-header logic that already
decides "is this technology mandatory or nice-to-have" is the correct tool
for deciding the same thing about a language or degree requirement. Nothing
in app/agents/skill_extractor.py is modified; this module only imports its
existing (module-private, but stable within this package) helpers.

Deliberately narrow scope, matching the project's existing skill-extraction
philosophy (CLAUDE.md: no hallucination, literal-mention only, no LLM):

- Only two language names are recognized (German/Deutsch, English/Englisch)
  — the two relevant to this project's German-market job sources. Extending
  to more languages is a matter of adding entries to `_LANGUAGE_NAMES`, not
  a design change.
- A language mention is only used when its own comma-delimited clause
  contains exactly one recognized language name AND exactly one CEFR/
  native level token — a clause naming two languages or two levels is
  skipped rather than guessed at, to avoid misattributing a level to the
  wrong language (see `extract_language_requirements`'s docstring for why
  this is clause-scoped rather than segment-scoped: one negated language
  in a segment must not suppress an unrelated, genuine requirement for a
  different language in the same segment).
- An explicitly negated language mention ("No German B2 required",
  "English B2 is not required", "Deutsch B2 nicht erforderlich", "Keine
  Englischkenntnisse erforderlich", ...) never produces a requirement at
  all — checked before must/nice classification via `_NEGATION_PATTERN`,
  not left to fall through to skill_extractor's own must/nice markers
  (which would otherwise classify "not required" as a nice-to-have signal,
  correct for a skill but wrong for a negated language level).
- Education requirement detection is a single coarse signal ("this vacancy
  requires *a* completed degree") — it does not attempt to determine
  degree level (Bachelor vs. Master). Stage 14B: field-of-study names
  literally present in that segment (Informatik, Wirtschaftsinformatik,
  ...) are reported in `fields` so the matcher never treats an arbitrary
  completed degree as that specific one. The first matching
  segment (in title-then-description order) wins; multiple mentions of the
  same requirement do not change the result.
- Stage 14B: "N years of experience" (`extract_experience_requirement`)
  and the list of must-section segments (`must_section_segments`) so the
  matcher can expose requirement text it could not evaluate instead of
  dropping it. The year count must directly modify the experience noun,
  and the technologies in that clause are kept as its `subjects`.
- Stage 14B: "<category> (z. B. A, B)" lists (`extract_example_groups`) --
  one grouped requirement whose examples are not individual requirements.
- Stage 14B (matcher v5): each language/education/experience requirement
  reports the character span(s) of its segment it actually evaluated, so
  the matcher can keep any other material clause of the same line ("Deutsch
  B2 und Erfahrung mit SAP ABAP") instead of treating the line as consumed.
- Stage 14B (matcher v6-v8): `REQUIREMENT_HEAD`, `starts_requirement_clause`
  and `independent_boundaries` are THE shared clause-ownership model -- a
  degree's fields and a duration's subjects come only from their own
  independent clause, and a duration's subjects combine as ALL / ANY /
  AMBIGUOUS (`subject_mode`). v8: a known head anchors its clause
  (`anchored_head`); the words before it are never checked against a
  modifier list, and experience is qualified by any attached modifier
  phrase (`qualified_experience_words`), not by a qualifier list.
- Stage 14B (matcher v8): `extract_category_requirements` -- a
  "<technology>-Frameworks" category is a requirement wherever it is named
  (title, description, with or without examples), identified by
  `category_key`.
- Certification requirements (Stage 6B spec section 14's "certifications
  same rule") are NOT extracted in v1 — no reliable literal-mention pattern
  for "this vacancy requires certification X" exists yet in this codebase,
  unlike the well-established technology-mention patterns skill_extractor
  already has. Documented as a known v1 limitation, not implemented
  opportunistically here.
"""

import re
from dataclasses import dataclass
from typing import Literal

from app.agents.skill_extractor import (
    _MUST_HAVE_MARKERS,
    _NICE_TO_HAVE_MARKERS,
    _PROFILE_REQUIREMENT_MARKERS,
    _TECHNOLOGY_PATTERNS,
    _classify_mention,
    _contextual_segments,
    _postfix_nice_scopes_all_mentions,
    category_token_spans,
    example_category_span,
    example_lists,
)

RequirementImportance = Literal["REQUIRED", "PREFERRED", "UNKNOWN"]
SubjectMode = Literal["ALL", "ANY", "AMBIGUOUS"]

_LANGUAGE_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("German", re.compile(r"(?<!\w)(?:german|deutsch)(?:kenntnisse)?(?!\w)", re.IGNORECASE)),
    ("English", re.compile(r"(?<!\w)(?:english|englisch)(?:kenntnisse)?(?!\w)", re.IGNORECASE)),
)

_CEFR_LEVEL_PATTERN = re.compile(
    r"(?<!\w)(A1|A2|B1|B2|C1|C2)(?:[- ]?niveau|[- ]?level)?(?!\w)", re.IGNORECASE
)
_NATIVE_PATTERN = re.compile(
    r"(?<!\w)(?:native|muttersprache|muttersprachlich(?:e[rs]?)?)(?!\w)", re.IGNORECASE
)

_NEGATION_PATTERN = re.compile(
    r"\bno\b.{0,40}\b(?:required|necessary)\b|"
    r"\bnot\s+(?:required|necessary|mandatory|preferred)\b|"
    r"\b(?:isn|aren)'?t\s+(?:required|necessary|mandatory|preferred)\b|"
    r"\bnicht\s+(?:erforderlich|notwendig|vorausgesetzt)\b|"
    r"\bkeine?\b.{0,40}\b(?:erforderlich|notwendig)\b",
    re.IGNORECASE,
)

_DEGREE_PATTERN = re.compile(
    r"(?<!\w)(?:bachelor|master|"
    r"abgeschlossen(?:e[rs]?)?\s+(?:hochschul)?studium|"
    r"hochschulabschluss|"
    r"(?:university|college)\s+degree|"
    r"completed\s+degree)(?!\w)",
    re.IGNORECASE,
)


# Stage 14B: a named field of study inside the degree segment ("Studium
# der Informatik, Wirtschaftsinformatik oder vergleichbare Qualifikation").
# Literal names only; "comparable" is never resolved automatically.
_DEGREE_FIELDS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("Wirtschaftsinformatik", r"(?<!\w)wirtschaftsinformatik(?!\w)"),
        ("Informatik", r"(?<![\w-])informatik(?!\w)"),
        ("Computer Science", r"(?<!\w)computer\s+science(?!\w)"),
        ("Mathematik", r"(?<!\w)mathematik(?!\w)"),
        ("Physik", r"(?<!\w)physik(?!\w)"),
    )
)

_YEAR_WORDS = {
    "ein": 1, "einem": 1, "einen": 1, "one": 1,
    "zwei": 2, "two": 2, "drei": 3, "three": 3, "vier": 4, "four": 4,
    "fünf": 5, "five": 5, "sechs": 6, "six": 6, "sieben": 7, "seven": 7,
    "acht": 8, "eight": 8, "neun": 9, "nine": 9, "zehn": 10, "ten": 10,
}  # fmt: skip
_YEARS_NUMBER = r"(?P<years>\d{1,2}|" + "|".join(_YEAR_WORDS) + r")"
_EXP_ADJECTIVE = (
    r"(?:(?:einschlägige|relevante|praktische|professionelle|fundierte|"
    r"professional|relevant|practical|industry|hands-on|work)\s+)?"
)
# Stage 14B: the year count must directly modify the experience noun --
# "seit 20 Jahren bestehenden Team" or "30 Jahren Erfahrung am Markt" never
# count. Accepted shapes: "N Jahre (adj) Berufserfahrung/Erfahrung",
# "N-jährige Berufserfahrung", "Berufserfahrung von (mindestens) N Jahren",
# "N(+) years (of) (adj) experience".
_EXPERIENCE_YEARS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?<!\w)" + _YEARS_NUMBER + r"\s*\+?\s*jahre\s+" + _EXP_ADJECTIVE
        + r"(?:berufs|praxis)?erfahrung(?:en)?(?!\w)",
        r"(?<!\w)" + _YEARS_NUMBER + r"[- ]?jährige\s+" + _EXP_ADJECTIVE
        + r"(?:berufs|praxis)?erfahrung(?!\w)",
        r"(?<!\w)(?:berufs|praxis)?erfahrung\s+von\s+(?:mindestens\s+|über\s+)?"
        + _YEARS_NUMBER + r"\s*\+?\s*jahren(?!\w)",
        r"(?<!\w)" + _YEARS_NUMBER + r"\s*\+?\s*years?\s+(?:of\s+)?" + _EXP_ADJECTIVE
        + r"experience(?!\w)",
    )
)  # fmt: skip
_CLAUSE_BOUNDARY = re.compile(r",|;|\s+sowie\s+|\s+as\s+well\s+as\s+", re.IGNORECASE)
# v7/v8: THE clause model, shared by every requirement type and the
# matcher's residual net. One vocabulary says what can head an independent
# requirement clause (experience, knowledge, a degree); one predicate
# (`starts_requirement_clause`) says whether a text begins one. No other
# head list exists (see the contract test).
_HEAD_STEMS = (
    r"erfahrung(?:en)?|kenntniss?e?n?|know-how|verständnis|expertise|"
    r"experience|knowledge|understanding|proficiency|skills|"
    r"studium|abschl(?:uss|üsse)|degree"
)
REQUIREMENT_HEAD = re.compile(r"(?<!\w)[\w-]*?(?:" + _HEAD_STEMS + r")(?!\w)", re.IGNORECASE)
# v8: the head ANCHORS its clause. Whatever words stand between a boundary
# and a known head are that head's own local modifiers ("umfangreiche",
# "außerordentlich gute", "advanced working") -- no adjective list is
# consulted, so an unfamiliar modifier can never hide a clause. The scan is
# bounded: at most `MAX_HEAD_MODIFIERS` words, and it stops at a hard
# separator, a coordinating conjunction or a preposition (a CLOSED word
# class: a head behind one is some other phrase's complement, "und in der
# Softwareentwicklung").
MAX_HEAD_MODIFIERS = 5
_HARD_SEPARATOR = re.compile(r"[,;():]|\.(?=\s|$)")
_CLAUSE_STOP_WORD = re.compile(
    r"und|oder|sowie|bzw|sondern|aber|and|or|plus|but|nor|"
    r"in|im|ins|mit|von|vom|zu|zum|zur|auf|für|über|bei|beim|aus|nach|an|am|ans|um|"
    r"durch|gegen|ohne|unter|vor|neben|zwischen|seit|wegen|während|bis|als|wie|"
    r"of|with|for|on|at|by|from|to|into|about|as|like|than|within|including",
    re.IGNORECASE,
)
_WORD = re.compile(r"[\w-]+")


def anchored_head(text: str) -> re.Match[str] | None:
    """v8: the `REQUIREMENT_HEAD` word that anchors a clause starting at
    `text`, or None -- the first word that is a head, reached before the
    first hard separator through at most `MAX_HEAD_MODIFIERS` words none of
    which is a conjunction or preposition."""
    separator = _HARD_SEPARATOR.search(text)
    end = separator.start() if separator else len(text)
    for count, word in enumerate(_WORD.finditer(text, 0, end)):
        token = word.group().strip("-")
        if REQUIREMENT_HEAD.fullmatch(token):
            return word
        if count >= MAX_HEAD_MODIFIERS or _CLAUSE_STOP_WORD.fullmatch(token):
            return None
    return None


def starts_requirement_clause(text: str) -> bool:
    """v7/v8: whether `text` begins a requirement clause of its own -- a
    known head anchors it (`anchored_head`)."""
    return anchored_head(text) is not None


# v5-v7: "... und Erfahrung mit X", ", Kenntnisse in X", "(Erfahrung mit X)",
# "und abgeschlossenes Studium" or ";" starts a NEW, independent
# requirement -- a year count or degree never extends across it. "Bachelor-
# und Masterabschluss" is one degree, not two clauses.
_BOUNDARY_CANDIDATE = re.compile(
    r"(?<!-)\s+(?:und|sowie|and|plus|as\s+well\s+as)\s+|\s*[,(]\s*|;", re.IGNORECASE
)


def independent_boundaries(segment: str) -> list[tuple[int, int]]:
    """v7: (start, end) of every boundary in `segment` after which an
    independent requirement clause starts."""
    return [
        match.span()
        for match in _BOUNDARY_CANDIDATE.finditer(segment)
        if match.group() == ";" or starts_requirement_clause(segment[match.end() :])
    ]


def independent_clause_bounds(segment: str, start: int, end: int) -> tuple[int, int]:
    """v6: the independent clause of `segment` containing [start, end)."""
    boundaries = independent_boundaries(segment)
    clause_start = max((b_end for _, b_end in boundaries if b_end <= start), default=0)
    clause_end = min((b_start for b_start, _ in boundaries if b_start >= end), default=len(segment))
    return clause_start, clause_end


# v7: an experience head whose wording adds hiring meaning beyond "knows the
# technology" -- professional/practical/multi-year experience. Material
# wherever no typed EXPERIENCE requirement evaluated it.
_QUALIFIED_EXPERIENCE_HEAD = re.compile(
    r"(?:berufs|praxis|arbeits|projekt)erfahrung(?:en)?", re.IGNORECASE
)
_EXPERIENCE_HEAD = re.compile(r"[\w-]*?(?:erfahrung(?:en)?|experience)", re.IGNORECASE)
# v8: words that sit before an experience head without qualifying it -- the
# CLOSED classes of articles and discourse adverbs ("eine Erfahrung",
# "idealerweise Erfahrung"). Anything else attached to the head qualifies it.
_NON_QUALIFYING = re.compile(
    r"ein(?:e[mnrs]?)?|der|die|das|den|dem|des|a|an|the|"
    r"idealerweise|ideally|zudem|auch|also|gerne|möglichst|ggf",
    re.IGNORECASE,
)


def qualified_experience_words(words: list[str]) -> list[int]:
    """v7/v8: indexes in `words` (one clause, in order) of qualified
    experience wording -- a lexicalized head ("Berufserfahrung") or an
    experience head with a non-empty attached modifier phrase ("fundierte
    Erfahrung", "sehr gute praktische Erfahrung", "solid experience").

    v8: structural, not a qualifier list -- the modifier phrase is the
    contiguous words right before the head (at most `MAX_HEAD_MODIFIERS`,
    never across a conjunction or preposition); it qualifies the head when
    any of its words is not an article or discourse adverb. A bare head
    ("Kenntnisse und Erfahrung in Python") stays bare."""
    indexes: list[int] = []
    for index, word in enumerate(words):
        token = word.strip("-")
        if not _EXPERIENCE_HEAD.fullmatch(token):
            continue
        first = index
        while (
            first > 0
            and index - first < MAX_HEAD_MODIFIERS
            and not _CLAUSE_STOP_WORD.fullmatch(words[first - 1].strip("-"))
            and not REQUIREMENT_HEAD.fullmatch(words[first - 1].strip("-"))
        ):
            first -= 1
        if _QUALIFIED_EXPERIENCE_HEAD.fullmatch(token) or any(
            not _NON_QUALIFYING.fullmatch(words[i].strip("-")) for i in range(first, index)
        ):
            indexes.extend(i for i in range(first, index + 1) if i not in indexes)
    return indexes


# v5: degree wording an education requirement evaluates inside its clause.
_EDUCATION_WORD = re.compile(
    r"(?<!\w)[\w-]*?(?:bachelor|master|diplom|studium|studiengang|abschluss|degree|"
    r"abgeschlossen\w*|completed|university|college|hochschule)[\w-]*(?!\w)",
    re.IGNORECASE,
)
_COMPARABLE_QUALIFICATION = re.compile(
    r"(?<!\w)(?:vergleichbare[nrs]?|ähnliche[nrs]?|verwandte[nrs]?|entsprechende[nrs]?|"
    r"comparable|similar|related|equivalent)"
    r"(?:\s+(?:qualifikation(?:en)?|ausbildung|fachrichtung|field|qualification))?(?!\w)",
    re.IGNORECASE,
)


def _clause_end(segment: str, position: int) -> int:
    """v5: end of the clause continuing at `position` -- the first `,`,
    `;`, "sowie" or new independent "und Erfahrung/Kenntnisse ..." clause."""
    ends = [b_start for b_start, _ in independent_boundaries(segment) if b_start >= position]
    if (match := _CLAUSE_BOUNDARY.search(segment, position)) is not None:
        ends.append(match.start())
    return min(ends, default=len(segment))


@dataclass(frozen=True)
class ExperienceRequirement:
    """ "At least N years of (professional) experience" -- Stage 14B.

    `subjects`: technologies named in the same clause from the duration on
    ("3 Jahre Berufserfahrung mit Python" -> ("python",)); the matcher only
    credits experience that uses them. `subject_text`: that clause, kept
    for human review when no technology is named ("... in der
    Softwareentwicklung")."""

    years: int
    importance: RequirementImportance
    evidence_text: str
    subjects: tuple[str, ...] = ()
    subject_text: str = ""
    # v6: how several subjects combine -- "ALL" ("mit Python und
    # Kubernetes": the duration needs every one), "ANY" ("Python oder
    # Kubernetes": one alternative suffices), "AMBIGUOUS" (any other
    # wording between them: never a definite MATCH). One subject is "ALL".
    subject_mode: SubjectMode = "ALL"
    # v5: (start, end) in `evidence_text` this requirement evaluated -- only
    # the duration phrase when it names a technology subject (the rest of
    # the clause is evaluated or kept elsewhere), the whole clause when it
    # names none (that clause IS the UNKNOWN requirement's text).
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class ExampleGroupRequirement:
    """Stage 14B: "<category> (z. B. A, B)" -- ONE grouped requirement for
    the category; A and B are the vacancy's own examples of it, never
    individual hard requirements. `examples`: recognized technology names
    in the list; `example_text`: the raw listed text; `span`: (start, end)
    of category + list inside `evidence_text`."""

    category: str
    examples: tuple[str, ...]
    example_text: str
    importance: RequirementImportance
    evidence_text: str
    span: tuple[int, int]
    # v8: "title" for a category the job title names ("Python-Frameworks
    # Engineer"); "description" otherwise.
    source: Literal["title", "description"] = "description"


@dataclass(frozen=True)
class LanguageRequirement:
    language: str
    level: str
    importance: RequirementImportance
    evidence_text: str
    # v5: (start, end) in `evidence_text` of the language name and its level.
    span: tuple[int, int] | None = None


@dataclass(frozen=True)
class EducationRequirement:
    importance: RequirementImportance
    evidence_text: str
    # Stage 14B: fields of study literally named in the degree segment;
    # empty means the vacancy asks only for "a completed degree".
    fields: tuple[str, ...] = ()
    # v5: spans in `evidence_text` of the degree wording, the named fields
    # and an "or comparable" phrase inside the degree's own clause.
    spans: tuple[tuple[int, int], ...] = ()


def canonicalize_language_name(name: str) -> str | None:
    """Map a freeform language name (as entered in a Candidate Profile
    language record, e.g. "Deutsch", "German (business)") to the same
    canonical form `_LANGUAGE_NAMES` produces for job-side mentions, or
    None if it isn't one of the two names this module recognizes.
    """
    for canonical, pattern in _LANGUAGE_NAMES:
        if pattern.search(name):
            return canonical
    return None


def _segment_markers(
    segment: str, inherited_context: str | None
) -> tuple[list[re.Match[str]], list[re.Match[str]]]:
    must_markers = list(_MUST_HAVE_MARKERS.finditer(segment))
    if inherited_context == "must":
        must_markers.extend(_PROFILE_REQUIREMENT_MARKERS.finditer(segment))
    nice_markers = list(_NICE_TO_HAVE_MARKERS.finditer(segment))
    return must_markers, nice_markers


def _importance_for(
    mention: re.Match[str],
    must_markers: list[re.Match[str]],
    nice_markers: list[re.Match[str]],
    inherited_context: str | None,
    segment: str,
) -> RequirementImportance:
    classification = _classify_mention(
        mention, must_markers, nice_markers, inherited_context, segment, False
    )
    if classification == "must":
        return "REQUIRED"
    if classification == "nice":
        return "PREFERRED"
    return "UNKNOWN"


def _segments_with_title(
    title: str | None, description: str | None
) -> list[tuple[str, str | None]]:
    segments = _contextual_segments(description or "")
    if title and title.strip():
        # The title is its own segment with no inherited must/nice section
        # context — unlike extract_skills, a language/degree mention in the
        # title has no strong "this implies REQUIRED" precedent, so it is
        # classified the same way as any other context-less segment
        # (falls back to UNKNOWN importance unless the title itself
        # contains a must/nice marker word).
        segments = [(title, None), *segments]
    return segments


def _clause_span(segment: str, mention: re.Match[str]) -> tuple[int, int]:
    """The comma-delimited clause containing `mention`, e.g. for "German is
    not required, but English B2 is required." and a mention of "English",
    returns the span of " but English B2 is required." only.

    Mirrors the same clause-boundary arithmetic
    app.agents.skill_extractor._classify_mention already computes
    internally for its own must/nice marker scoping (duplicated here, not
    imported, since that function doesn't expose its clause bounds and
    this is one line of arithmetic, not a decision — skill_extractor.py
    itself is never modified, see module docstring).
    """
    start = segment.rfind(",", 0, mention.start()) + 1
    end = segment.find(",", mention.end())
    if end == -1:
        end = len(segment)
    return start, end


def extract_language_requirements(
    title: str | None, description: str | None
) -> list[LanguageRequirement]:
    """Extract explicit "<language> <CEFR level>" (or native) mentions.

    Each language mention is resolved against its own comma-delimited
    clause (M-01 fix, section 10): "German is not required, but English B2
    is required." must yield only an English B2 REQUIRED row, not discard
    the whole segment merely because it also names German. A clause is
    used for a given mention only when it contains exactly one recognized
    language name and exactly one level token (CEFR or native) — see
    module docstring for why an ambiguous clause is skipped rather than
    guessed at.

    **Negation (M-01).** A clause matching `_NEGATION_PATTERN` ("No German
    B2 required", "German B2 is not required", "Deutsch B2 nicht
    erforderlich", "Keine Deutschkenntnisse erforderlich", ...) never
    produces a requirement at all — checked *before* must/nice
    classification, not after. This is deliberately a separate check from
    `_classify_mention`'s own must/nice markers: `_NICE_TO_HAVE_MARKERS`
    (shared with SKILL extraction) treats "not required"/"nicht
    erforderlich" as a nice-to-have signal, which is the right call for a
    skill ("Docker experience not required, but a plus") but wrong for a
    language level — a negated language requirement is *not a
    requirement*, not a demoted-to-PREFERRED one.
    """
    results: list[LanguageRequirement] = []
    for segment, inherited_context in _segments_with_title(title, description):
        must_markers, nice_markers = _segment_markers(segment, inherited_context)
        for language_name, language_mention in (
            (name, match)
            for name, pattern in _LANGUAGE_NAMES
            for match in pattern.finditer(segment)
        ):
            clause_start, clause_end = _clause_span(segment, language_mention)
            clause = segment[clause_start:clause_end]

            language_names_in_clause = [
                name for name, pattern in _LANGUAGE_NAMES if pattern.search(clause)
            ]
            if len(language_names_in_clause) != 1:
                continue

            cefr_matches = list(_CEFR_LEVEL_PATTERN.finditer(clause))
            native_matches = list(_NATIVE_PATTERN.finditer(clause))
            if len(cefr_matches) + len(native_matches) != 1:
                continue

            if _NEGATION_PATTERN.search(clause):
                continue

            level = cefr_matches[0].group(1).upper() if cefr_matches else "NATIVE"
            importance = _importance_for(
                language_mention, must_markers, nice_markers, inherited_context, segment
            )
            level_match = (cefr_matches or native_matches)[0]
            offset = len(segment) - len(segment.lstrip())
            span = (
                min(language_mention.start(), clause_start + level_match.start()) - offset,
                max(language_mention.end(), clause_start + level_match.end()) - offset,
            )
            results.append(
                LanguageRequirement(
                    language=language_name,
                    level=level,
                    importance=importance,
                    evidence_text=segment.strip(),
                    span=span,
                )
            )
    return results


def extract_education_requirement(
    title: str | None, description: str | None
) -> EducationRequirement | None:
    """Extract a single, coarse "this vacancy requires a completed degree"
    signal, if present — see module docstring for scope. Returns the first
    matching segment (title-then-description order); later mentions of the
    same requirement do not change the result.
    """
    for segment, inherited_context in _segments_with_title(title, description):
        degree_matches = list(_DEGREE_PATTERN.finditer(segment))
        if not degree_matches:
            continue
        must_markers, nice_markers = _segment_markers(segment, inherited_context)
        importance = _importance_for(
            degree_matches[0], must_markers, nice_markers, inherited_context, segment
        )
        # v5: what this requirement evaluated -- degree wording, field names
        # and "oder vergleichbar" inside the degree's own clause; anything
        # else on the line ("... und Kenntnisse in SAP ABAP") stays material.
        # v6: the fields come from that clause only, so "Studium der
        # Mathematik und Kenntnisse in Informatik" never asks for Informatik.
        degree = degree_matches[0]
        offset = len(segment) - len(segment.lstrip())
        clause_start, clause_end = independent_clause_bounds(segment, degree.start(), degree.end())
        fields = degree_field_names(segment[clause_start:clause_end])
        spans = tuple(
            (match.start() - offset, match.end() - offset)
            for pattern in (
                _EDUCATION_WORD,
                _COMPARABLE_QUALIFICATION,
                *(field_pattern for _, field_pattern in _DEGREE_FIELDS),
            )
            for match in pattern.finditer(segment, clause_start, clause_end)
        )
        return EducationRequirement(
            importance=importance, evidence_text=segment.strip(), fields=fields, spans=spans
        )
    return None


def degree_field_names(text: str | None) -> tuple[str, ...]:
    """Stage 14B: the `_DEGREE_FIELDS` names literally present in `text`
    (job segment or a candidate's education program/degree/field)."""
    return tuple(name for name, pattern in _DEGREE_FIELDS if pattern.search(text or ""))


def must_section_segments(description: str | None) -> list[str]:
    """Stage 14B: every description segment inside a recognized
    requirement section ("Ihr Profil", "Profil", "Anforderungen", ...), in
    order -- the text the vacancy itself marks as required."""
    return [
        segment.strip()
        for segment, context in _contextual_segments(description or "")
        if context == "must" and segment.strip()
    ]


def extract_experience_requirement(
    title: str | None, description: str | None
) -> ExperienceRequirement | None:
    """Stage 14B: the first segment where a year count directly modifies an
    experience noun (`_EXPERIENCE_YEARS_PATTERNS`), e.g. "Mindestens drei
    Jahre Berufserfahrung ..."; the largest such count in that segment
    wins. Subjects are the technologies named in the same clause from the
    duration on. Returns None if no such segment exists."""
    for segment, inherited_context in _segments_with_title(title, description):
        year_matches = [
            match for pattern in _EXPERIENCE_YEARS_PATTERNS for match in pattern.finditer(segment)
        ]
        if not year_matches:
            continue

        def _years(match: re.Match[str]) -> int:
            token = match.group("years").casefold()
            return int(token) if token.isdigit() else _YEAR_WORDS[token]

        best = max(year_matches, key=_years)
        years = _years(best)
        if years < 1:
            continue
        # v5: the duration binds only to its own clause -- "3 Jahre ... mit
        # Python und Erfahrung mit Kubernetes" is a Python-only duration.
        clause_end = _clause_end(segment, best.end())
        clause = segment[best.start() : clause_end]
        subjects = tuple(
            dict.fromkeys(name for name, pattern in _TECHNOLOGY_PATTERNS if pattern.search(clause))
        )
        subject_mode = _subject_mode(clause)
        must_markers, nice_markers = _segment_markers(segment, inherited_context)
        importance = _importance_for(best, must_markers, nice_markers, inherited_context, segment)
        offset = len(segment) - len(segment.lstrip())
        span_end = best.end() if subjects else clause_end
        return ExperienceRequirement(
            years=years,
            importance=importance,
            evidence_text=segment.strip(),
            subjects=subjects,
            subject_text=clause.strip(" .,;"),
            span=(best.start() - offset, span_end - offset),
            subject_mode=subject_mode,
        )
    return None


_ALL_CONNECTOR = re.compile(r"(?:und|and|&)", re.IGNORECASE)
_ANY_CONNECTOR = re.compile(r"(?:oder|or|bzw\.?)", re.IGNORECASE)


def _subject_mode(clause: str) -> SubjectMode:
    """v6: how the technologies of a quantified clause combine, from the
    exact words between consecutive mentions. Only a pure "und"/"and" chain
    is ALL and only a pure "oder"/"or" chain is ANY; a mix or any other
    wording ("Python in Kubernetes-Umgebungen") is AMBIGUOUS."""
    mentions: list[re.Match[str]] = sorted(
        (m for _, pattern in _TECHNOLOGY_PATTERNS for m in pattern.finditer(clause)),
        key=lambda m: m.start(),
    )
    gaps: list[str] = []
    previous_end: int | None = None
    for mention in mentions:
        if previous_end is not None and mention.start() < previous_end:
            continue
        if previous_end is not None:
            gaps.append(clause[previous_end : mention.start()].strip())
        previous_end = mention.end()
    if all(_ALL_CONNECTOR.fullmatch(gap) for gap in gaps):
        return "ALL"
    if all(_ANY_CONNECTOR.fullmatch(gap) for gap in gaps):
        return "ANY"
    return "AMBIGUOUS"


def extract_example_groups(description: str | None) -> list[ExampleGroupRequirement]:
    """Stage 14B: every "<category> (z. B. A, B)" / "<category> such as A,
    B" list in the description. The category is the phrase right before
    the opening parenthesis (or the marker), back to the previous clause
    word ("und", "in", "mit", ","). A list whose category IS a technology
    ("Python (z. B. Django)") is not a group -- that technology is already
    its own requirement. v5: a category merely containing one
    ("Python-Frameworks", "LLM-Frameworks") is a group of its own; the base
    technology never stands in for it (v6: nor is it extracted as a
    requirement of its own from that category name, see
    skill_extractor.extract_skills)."""
    results: list[ExampleGroupRequirement] = []
    for raw_segment, inherited_context in _contextual_segments(description or ""):
        segment = raw_segment.strip()
        for item in example_lists(segment):
            category_span = example_category_span(segment, item)
            if category_span is None:
                continue
            span_start = category_span[0]
            category = segment[span_start : category_span[1]]
            listed = segment[item.marker_end : item.list_end]
            examples = tuple(
                dict.fromkeys(name for name, p in _TECHNOLOGY_PATTERNS if p.search(listed))
            )
            span_end = item.list_end
            if segment[span_end : span_end + 1] == ")":
                span_end += 1
            must_markers, nice_markers = _segment_markers(segment, inherited_context)
            anchor = re.compile(re.escape(category)).search(segment, span_start)
            # Same postfix rule extract_skills applies ("... sind von Vorteil").
            classification = _classify_mention(
                anchor,
                must_markers,
                nice_markers,
                inherited_context,
                segment,
                _postfix_nice_scopes_all_mentions(segment, [anchor], must_markers),
            )
            importance = {"must": "REQUIRED", "nice": "PREFERRED"}.get(classification, "UNKNOWN")
            results.append(
                ExampleGroupRequirement(
                    category=category,
                    examples=examples,
                    example_text=listed.strip(" ,"),
                    importance=importance,
                    evidence_text=segment,
                    span=(span_start, span_end),
                )
            )
    return results


def category_key(category: str) -> str:
    """v8: identity of a category requirement. A category built on one
    "<technology>-Frameworks" token is that token ("moderne Python-
    Frameworks" and the title's "Python-Frameworks" are one requirement);
    any other category is its own casefolded, whitespace-collapsed text."""
    tokens = category_token_spans(category)
    text = category[tokens[0][0] : tokens[0][1]] if len(tokens) == 1 else category
    return "group:" + " ".join(text.casefold().split())


def extract_category_requirements(
    title: str | None, description: str | None
) -> list[ExampleGroupRequirement]:
    """v8: every category requirement, one entry per mention (not yet
    deduplicated -- see `category_key`): the description's example groups
    (`extract_example_groups`), every other "<technology>-Frameworks" token
    in the description, and every such token in the title. A category the
    title names is REQUIRED (the title names the candidate being sought,
    the same rule as a title technology) and has no examples of its own."""
    results = extract_example_groups(description)
    for raw_segment, inherited_context in _contextual_segments(description or ""):
        segment = raw_segment.strip()
        lists = example_lists(segment)
        covered = [(item.marker_start, item.list_end) for item in lists] + [
            g.span for g in results if g.evidence_text == segment
        ]
        must_markers, nice_markers = _segment_markers(segment, inherited_context)
        for start, end in category_token_spans(segment):
            if any(c_start <= start and end <= c_end for c_start, c_end in covered):
                continue
            anchor = re.compile(re.escape(segment[start:end])).search(segment, start)
            classification = _classify_mention(
                anchor,
                must_markers,
                nice_markers,
                inherited_context,
                segment,
                _postfix_nice_scopes_all_mentions(segment, [anchor], must_markers),
            )
            results.append(
                ExampleGroupRequirement(
                    category=segment[start:end],
                    examples=(),
                    example_text="",
                    importance={"must": "REQUIRED", "nice": "PREFERRED"}.get(
                        classification, "UNKNOWN"
                    ),
                    evidence_text=segment,
                    span=(start, end),
                )
            )
    title_text = (title or "").strip()
    for start, end in category_token_spans(title_text):
        results.append(
            ExampleGroupRequirement(
                category=title_text[start:end],
                examples=(),
                example_text="",
                importance="REQUIRED",
                evidence_text=title_text,
                span=(start, end),
                source="title",
            )
        )
    return results
