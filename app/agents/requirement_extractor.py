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
    example_lists,
)

RequirementImportance = Literal["REQUIRED", "PREFERRED", "UNKNOWN"]

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


@dataclass(frozen=True)
class LanguageRequirement:
    language: str
    level: str
    importance: RequirementImportance
    evidence_text: str


@dataclass(frozen=True)
class EducationRequirement:
    importance: RequirementImportance
    evidence_text: str
    # Stage 14B: fields of study literally named in the degree segment;
    # empty means the vacancy asks only for "a completed degree".
    fields: tuple[str, ...] = ()


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
            results.append(
                LanguageRequirement(
                    language=language_name,
                    level=level,
                    importance=importance,
                    evidence_text=segment.strip(),
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
        fields = degree_field_names(segment)
        return EducationRequirement(
            importance=importance, evidence_text=segment.strip(), fields=fields
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
        boundary = _CLAUSE_BOUNDARY.search(segment, best.end())
        clause = segment[best.start() : boundary.start() if boundary else len(segment)]
        subjects = tuple(
            dict.fromkeys(name for name, pattern in _TECHNOLOGY_PATTERNS if pattern.search(clause))
        )
        must_markers, nice_markers = _segment_markers(segment, inherited_context)
        importance = _importance_for(best, must_markers, nice_markers, inherited_context, segment)
        return ExperienceRequirement(
            years=years,
            importance=importance,
            evidence_text=segment.strip(),
            subjects=subjects,
            subject_text=clause.strip(" .,;"),
        )
    return None


# Where a category phrase starts, scanning back from "(z. B." / "such as".
_CATEGORY_START = re.compile(
    r"(?:^|[,;:]|\s(?:und|oder|sowie|in|mit|von|im|and|or|with|of)\s)", re.IGNORECASE
)


_TRAILING_CONNECTOR = re.compile(r"\s+(?:wie|etwa|like)\s*$", re.IGNORECASE)


def extract_example_groups(description: str | None) -> list[ExampleGroupRequirement]:
    """Stage 14B: every "<category> (z. B. A, B)" / "<category> such as A,
    B" list in the description. The category is the phrase right before
    the opening parenthesis (or the marker), back to the previous clause
    word ("und", "in", "mit", ","). A list whose category itself names a
    technology ("Python (z. B. Django)") is not a group -- that technology
    is already its own requirement."""
    results: list[ExampleGroupRequirement] = []
    for raw_segment, inherited_context in _contextual_segments(description or ""):
        segment = raw_segment.strip()
        for item in example_lists(segment):
            head_end = item.marker_start
            paren = segment.rfind("(", 0, item.marker_start)
            if paren != -1 and not segment[paren + 1 : item.marker_start].strip():
                head_end = paren
            head = segment[:head_end].rstrip(" ,")
            starts = list(_CATEGORY_START.finditer(head))
            category_start = starts[-1].end() if starts else 0
            category = _TRAILING_CONNECTOR.sub("", head[category_start:]).strip(" ,;:")
            if not category or any(p.search(category) for _, p in _TECHNOLOGY_PATTERNS):
                continue
            listed = segment[item.marker_end : item.list_end]
            examples = tuple(
                dict.fromkeys(name for name, p in _TECHNOLOGY_PATTERNS if p.search(listed))
            )
            span_end = item.list_end
            if segment[span_end : span_end + 1] == ")":
                span_end += 1
            span_start = head.index(category, category_start)
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
