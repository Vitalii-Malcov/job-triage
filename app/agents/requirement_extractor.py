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
  degree level (Bachelor vs. Master) or field of study. The first matching
  segment (in title-then-description order) wins; multiple mentions of the
  same requirement do not change the result.
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
    _classify_mention,
    _contextual_segments,
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
        return EducationRequirement(importance=importance, evidence_text=segment.strip())
    return None
