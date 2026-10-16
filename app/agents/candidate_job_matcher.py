"""Deterministic Candidate Profile <-> Job matching (Stage 6B).

**Purpose.** Answers, evidence-first and without any LLM/network/embedding
call: which confirmed candidate facts match this vacancy, which
requirements are missing or partial, which experiences/projects are
relevant, and which claims are safe to reuse later in Stage 6C CV/Bewerbung
generation. This module performs no I/O of its own — `compute_match` is a
pure function over already-loaded data.

**Hard trust rule (Stage 6B spec section 2).** Every candidate-side fact
considered here — skills, experiences, projects, education, languages —
must pass `app.models.candidate_profile.is_usable_for_generation` (BOTH a
trusted source AND confidence == CONFIRMED) before it is used as evidence.
This is the exact same function Stage 6A's CP-M-01 fix introduced; it is
imported, never reimplemented or weakened here.

**Requirement extraction, reused not duplicated.** SKILL requirements come
directly from `JobRecord.must_have_skills_json` /
`nice_to_have_skills_json` — already extracted at collection time by
app.agents.skill_extractor, not re-parsed here. LANGUAGE and EDUCATION
requirements come from app.agents.requirement_extractor, which itself
reuses skill_extractor's segment/context-classification engine. This module
adds no new text-mining of its own beyond calling those two.

**Skill normalization/aliasing, reused not duplicated.** Skill identity
comparison uses `app.agents.job_scorer.normalize_skill` — the same
casefold + whitespace-collapse + small explicit alias table
(`postgres`/`postgresql`, `fast api`/`fastapi`, ...) already used by
JobScorer, so a skill that JobScorer would count as a match is never
treated differently here. No fuzzy/semantic matching is added on top: two
skills either normalize to the same string or they don't. This is a
deliberate under-match-rather-than-over-match choice (spec section 6/7) —
e.g. "AWS" and "cloud" never match, and (per the spec's own worked
example) "Flask" is MISSING for a candidate who only has "Python", not
PARTIAL.

**MATCH/PARTIAL/MISSING/UNKNOWN rules, per requirement_type:**

- SKILL: MATCH if a trusted candidate skill normalizes to the same string
  as the requirement; otherwise MISSING. No PARTIAL — this project has no
  defensible, non-fuzzy skill-subset relationship (spec section 7's own
  example: "SQL" vs. "MySQL" is deliberately NOT implemented as PARTIAL in
  v1, since the only way to justify it would be a hand-maintained
  technology-hierarchy table, which is exactly the kind of judgment call
  the spec asks to avoid inventing without an explicit, controlled alias).
- LANGUAGE: no trusted CandidateLanguage record for that language at all
  -> MISSING (positively checked, no evidence). A trusted record exists
  but its level is UNKNOWN -> UNKNOWN (we know the candidate claims this
  language, but cannot confirm or deny it meets the bar). A trusted record
  exists with a known CEFR/NATIVE level: candidate_level >= required_level
  (C2 > C1 > B2 > B1 > A2 > A1, NATIVE > C2) -> MATCH; candidate_level <
  required_level -> PARTIAL (an explicit, defensible relationship: same
  language, insufficient level, not "no evidence at all").
- EDUCATION: only ever emitted when the job text contains the coarse
  "completed degree required" signal (see requirement_extractor). MATCH if
  at least one trusted CandidateEducation record has completed=True;
  otherwise MISSING — this covers both "no education records at all" and
  "education records exist but none are completed" identically, since both
  are positive evidence the requirement isn't satisfied (spec section 14:
  an incomplete degree must never match a "completed" requirement).

**Score formula (section 9/31 — fully explainable, no opaque AI
percentage).** Every requirement contributes `1.0` (MATCH), `0.5`
(PARTIAL), or `0.0` (MISSING/UNKNOWN) points.

- `required_skill_score` = mean points of all REQUIRED-importance
  requirements (any type) * 100. If there are none, `50` (an explicit
  "we don't know" neutral value — mirrors JobScorer's own
  `must_score = 0.5` when a job has no extracted must-have skills at all;
  absence of extracted requirements is ambiguous, not "nothing required").
- `preferred_skill_score` = mean points of all PREFERRED-importance
  requirements * 100. If there are none, `100` (trivially satisfied —
  mirrors JobScorer's `nice_score = 1.0` when there are no nice-to-haves;
  unlike the REQUIRED case, "nothing preferred" has no ambiguity).
- `coverage_score` = a single blended number across every requirement,
  weighted by importance (REQUIRED=3, PREFERRED=1, UNKNOWN-importance=0.5
  — "low-confidence influence" per spec section 8) as
  `sum(weight * points) / sum(weight) * 100`. `50` if there are no
  requirements at all (same ambiguous-neutral rationale as above).
- `experience_support_score` = how much of the REQUIRED skill coverage is
  actually backed by a relevant_experience/relevant_project (not just a
  bare CandidateSkill row): v2+ = `count(REQUIRED skills named in some
  relevant experience/project) / count(REQUIRED skill requirements) * 100`
  (v1 counted backing rows instead of backed skills), then scaled by a
  years-of-experience requirement's points (Stage 14B, below).
  `50` if there are no REQUIRED skill requirements (same rationale).
- `overall_score` = `round(required_skill_score * 0.6 +
  preferred_skill_score * 0.2 + experience_support_score * 0.2)` —
  required coverage dominates, preferred coverage and
  experience/project backing each contribute a fifth (spec section 9's
  suggested 60/20/20 concept, generalized from "skills only" to "every
  requirement type" since language/education requirements are also
  REQUIRED/PREFERRED facts, not a separate axis).

Every sub-score is independently bounded [0, 100] by construction (a mean
of values in [0, 1] scaled by 100, or an explicit neutral default), so
`overall_score` (a weighted average of three such bounded values with
weights summing to 1.0) is always in [0, 100] with no clamping required
and no divide-by-zero path.

**Score means "coverage of vacancy requirements by trusted candidate
evidence" — not "chance of getting the job"** (spec section 8/9).

**Stage 14B (v2, v3): no silent requirement loss.** A score is only as honest
as the requirement list behind it, so v2 makes that list fail closed (v3:
closing/contact headings end a requirement section, so an application
footer is no longer read as unevaluated required text):

- SKILL requirements are the persisted lists UNIONED with a fresh
  `skill_extractor.extract_skills` over the description; skills the stored
  (older) extraction lacked are named in a warning.
- A generic skill (`_GENERIC_SKILL_RELATED`, e.g. "testing") with only a
  related, more specific trusted skill (e.g. Pytest) is UNKNOWN (human
  review), never MATCH.
- EXPERIENCE ("at least N years"): MATCH only when merged date ranges of
  trusted experiences that are themselves relevant (use a required skill)
  reach N years; PARTIAL below that; UNKNOWN when a relevant row lacks
  dates; MISSING when no relevant experience exists. Projects never count.
  When such a requirement exists, `experience_support_score` is multiplied
  by its points, so projects alone cannot yield a high experience score.
- EDUCATION naming fields ("Informatik, Wirtschaftsinformatik oder
  vergleichbar"): MATCH only for a completed education literally naming one
  of them; another completed degree is UNKNOWN (comparable is a human call).
- OTHER: every remaining segment of a recognized requirement section that
  produced no evaluated requirement becomes a REQUIRED, UNKNOWN entry —
  so unevaluated required text always keeps `required_skill_score` < 100.

**v4 (Codex acceptance review):**

- Mixed lines: evaluated SKILL spans are removed from a requirement line
  and its material residual is the OTHER requirement ("Kenntnisse in
  Python und SAP ABAP" -> Python evaluated, "SAP ABAP" UNKNOWN); OTHER
  requirements are deduplicated by casefolded, whitespace-collapsed text.
- "<category> (z. B. A, B)" is ONE grouped SKILL requirement
  (`group:<category>`): MATCH only when a trusted skill is one of the
  vacancy's own named examples, otherwise UNKNOWN; the examples are not
  individual requirements. Grouped requirements are excluded from the
  experience-support denominator (no single named skill to back).
- EXPERIENCE years are credited only from experience using a technology
  named in that requirement's own clause; with no such technology the
  requirement is UNKNOWN (or MISSING with no experience at all), never
  MATCH. Year counts must directly modify the experience noun.

**v5 (Codex final review):**

- Every typed requirement consumes only the span it evaluated: a language
  name + level, a degree's wording/fields/"oder vergleichbar", a duration
  phrase (plus its clause only when that clause names no technology), a
  technology mention (widened only over attached words like "-basierter"
  or a plural "s"), the noun a parenthesised technology qualifies only
  when `_TECHNOLOGY_CATEGORY_NOUNS` says it names that technology's own
  category ("API-Entwicklung (REST)"), and example groups. The rest of a
  required line is split into clauses at `,` `;` parentheses and
  conjunctions; each clause with a material (non-filler) word is its own
  REQUIRED/UNKNOWN OTHER requirement. Domains ("Softwareentwicklung") and
  category nouns ("Frameworks") are material, never filler.
- A duration binds only to its own clause ("3 Jahre ... mit Python und
  Erfahrung mit Kubernetes" -> Python only).
- "Python-Frameworks (z. B. Django, Flask)" is a group of its own; the base
  technology never satisfies it.
- Skill importance: the current extraction (title + description) wins over
  a stale stored list; stored lists only add skills it does not name.
- OTHER identity also ignores trailing punctuation.

**v6 (Codex final review, clause ownership):**

- One clause model shared by every requirement type
  (`requirement_extractor.REQUIREMENT_HEAD` / `independent_clause_bounds`):
  a clause headed by an experience/knowledge/degree word is independent,
  and no typed requirement reads fields or subjects across it.
- A clause no typed requirement evaluated is kept whole when a head word
  heads it, even if every word is generic ("Deutsch B2 und mehrjährige
  Berufserfahrung" -> "mehrjährige Berufserfahrung" REQUIRED/UNKNOWN).
- A technology named only inside a category name ("Python-Frameworks (z.
  B. Django)", "LLM-Frameworks") is not its own requirement, and a stale
  stored list cannot resurrect it; an independent mention still is.
- A duration ends at an independent parenthetical ("(Erfahrung mit
  Kubernetes)"); "mit Python und Kubernetes" needs one experience using
  both, "Python oder Kubernetes" either, any other wording is never MATCH.
- Degree fields come only from the degree's own clause.

**v7 (Codex v6 review, one canonical clause model):**

- `requirement_extractor.REQUIREMENT_HEAD` + (v7) `CLAUSE_MODIFIER` +
  `starts_requirement_clause` are the only head vocabulary: degree and
  duration boundaries (`independent_boundaries`), the residual net's head
  test and its shared-complement test all use them, after any number of
  modifiers ("und sehr gute praktische Kenntnisse in Informatik",
  "and proficiency in Computer Science").
- Qualified experience ("Berufserfahrung", "mehrjährige/praktische
  Erfahrung", "professional/practical experience") is material in every
  clause: a SKILL requirement for the same technology does not evaluate
  it; only a typed EXPERIENCE duration span does. Only a BARE head shares
  a complement ("Kenntnisse und Erfahrung in Python").
- Titles use the description's category-only rule, so "Python-Frameworks
  Engineer" cannot authorize a stale stored "python".
- ANY durations are evaluated per alternative; years of different
  alternatives are never summed.

**v8 (Codex v7 review, head-anchored clauses):**

- A known `REQUIREMENT_HEAD` anchors its clause: after a boundary, the
  words before the head (never across a conjunction or preposition) are
  its own modifiers whatever they are --
  "und umfangreiche Kenntnisse in Informatik" ends a Mathematik degree's
  fields; v7's modifier whitelist (`CLAUSE_MODIFIER`) is gone.
- Experience is QUALIFIED when any non-article, non-discourse word is
  attached to its head ("fundierte/solide/gute Erfahrung", "strong
  experience"), so it stays a REQUIRED/UNKNOWN requirement; only a bare
  head shares a complement ("Kenntnisse und Erfahrung in Python").
- A "<technology>-Frameworks" category is ONE grouped requirement wherever
  it is named -- title ("Python-Frameworks Engineer", REQUIRED), description
  example group or bare token -- deduplicated by `category_key` with the
  description's examples kept; without examples it is always UNKNOWN.

**v9 (known v8 gaps):**

- The head scan and the qualified-experience modifier walk have no word
  count limit (v8: 5); only structure ends them -- a hard separator, a
  conjunction, a preposition or another head -- so a long chain of unknown
  modifiers never merges a requirement into the previous clause.
- A technology hyphenated to an experience head ("gute Python-Erfahrung",
  "Python-Erfahrung") is no longer consumed by its technology span: the
  technology is the SKILL requirement, the experience compound stays a
  REQUIRED/UNKNOWN requirement (displayed with its technology) unless a
  typed EXPERIENCE duration span owns it.

**v10 (experience subject preservation):**

- A qualified experience requirement keeps the technologies it is about:
  masking a SKILL span no longer generalizes "professional Python
  experience" to "professional experience" or "Python- und Docker-
  Erfahrung" to "Docker-Erfahrung". Its text and identity name every
  technology subject; it stays REQUIRED/UNKNOWN (an OTHER requirement is
  never MATCH), so a skill, a project or other-technology work never
  establishes it.
- A German hyphen ellipsis ("Python- und", "Python-,") is never a clause
  boundary.

**v11 (bare technology experience):**

- An experience head directly after an evaluated technology of the same
  clause ("Python experience", "Docker Erfahrung") is a technology
  compound, qualified like "Python-Erfahrung": it stays a REQUIRED/UNKNOWN
  requirement naming that technology, never erased by masking the SKILL
  span. Never across a clause boundary ("Python; experience").
- "N years (of) <technology> experience" is a typed duration, so the
  phrase is owned by `experience-Ny` and never duplicated as OTHER.

**algorithm_version.** `"v1"` — bumped whenever this scoring/matching logic
changes in a way that would produce a different result for the same
inputs, so a cached analysis from a future v2 is never silently compared
to or reused as a v1 result (see app/db/candidate_job_match_repository.py's
cache-identity key).
"""

import re
from dataclasses import dataclass, field
from datetime import date

from app.agents.job_scorer import normalize_skill
from app.agents.requirement_extractor import (
    _CLAUSE_STOP_WORD,
    EXPERIENCE_HEAD,
    REQUIREMENT_HEAD,
    canonicalize_language_name,
    category_key,
    degree_field_names,
    extract_category_requirements,
    extract_education_requirement,
    extract_experience_requirement,
    extract_language_requirements,
    must_section_segments,
    qualified_experience_words,
    starts_requirement_clause,
)
from app.agents.skill_extractor import (
    _TECHNOLOGY_PATTERNS,
    example_or_category_skills,
    extract_skills,
)
from app.models.candidate_job_match import (
    CandidateEvidenceRef,
    CandidateJobMatchData,
    RelevantEducation,
    RelevantExperience,
    RelevantLanguage,
    RelevantProject,
    RequirementMatch,
    SafeCandidateClaim,
)
from app.models.candidate_profile import CandidateProfile, is_usable_for_generation

ALGORITHM_VERSION = "v11"

# Stage 14B: a GENERIC skill requirement that a more specific trusted
# candidate skill may relate to, but never silently satisfies. A related
# skill turns MISSING into UNKNOWN (human review), never into MATCH; only an
# exactly normalized candidate skill matches.
_GENERIC_SKILL_RELATED: dict[str, frozenset[str]] = {
    normalize_skill(generic): frozenset(normalize_skill(name) for name in related)
    for generic, related in (
        ("testing", ("pytest", "unit testing", "unittest", "selenium", "playwright")),
    )
}

_CEFR_ORDER = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "NATIVE": 7}
_MATCH_POINTS = {"MATCH": 1.0, "PARTIAL": 0.5, "MISSING": 0.0, "UNKNOWN": 0.0}
_IMPORTANCE_WEIGHT = {"REQUIRED": 3.0, "PREFERRED": 1.0, "UNKNOWN": 0.5}
_NEUTRAL_NO_REQUIREMENTS = 50
_TRIVIALLY_SATISFIED = 100


@dataclass(frozen=True)
class JobMatchInput:
    """The subset of a JobRecord's already-persisted fields the matcher
    needs. Kept as its own small dataclass (rather than passing JobRecord
    directly) so this module has no SQLAlchemy dependency and stays a pure
    function of plain data, matching JobScorer's own `Job` (Pydantic, not
    ORM) input convention.
    """

    job_id: int
    title: str
    description: str
    must_have_skills: list[str] = field(default_factory=list)
    nice_to_have_skills: list[str] = field(default_factory=list)


def _trusted(items: list, source_attr: str = "source", confidence_attr: str = "confidence") -> list:
    return [
        item
        for item in items
        if is_usable_for_generation(getattr(item, source_attr), getattr(item, confidence_attr))
    ]


def _skill_requirements(job: JobMatchInput) -> list[tuple[str, str]]:
    """Return (original_text, importance) pairs, deduplicated by normalized
    identity — a must-have wins over a nice-to-have naming the same skill
    (shouldn't happen given skill_extractor's own dedup, but defensive).

    Stage 14B: the persisted lists are unioned with a fresh extraction so a
    requirement an older extractor dropped at collection time cannot
    silently vanish from the match. v5: the CURRENT extraction decides
    importance -- a stored must-have the title names (collection extracts
    title + description) first, then the fresh description extraction; a
    stored list only adds skills neither names, so a stale stored must-have
    never promotes a currently PREFERRED mention.
    """
    title_skills = set(extract_skills(job.title, None).must_have_skills)
    fresh = extract_skills(None, job.description)
    # v4: a technology the vacancy names ONLY as an example ("z. B.
    # LangChain") belongs to its grouped requirement, so a stored list from
    # an older extractor must not resurrect it as an individual one. v6: nor
    # one it names only inside a category ("Python-Frameworks"), v7: title too.
    example_only = {
        normalize_skill(name) for name in example_or_category_skills(job.title, job.description)
    } - {
        normalize_skill(t)
        for t in [*title_skills, *fresh.must_have_skills, *fresh.nice_to_have_skills]
    }
    seen: dict[str, tuple[str, str]] = {}
    title_norms = {normalize_skill(t) for t in title_skills}
    for text in job.must_have_skills:
        if normalize_skill(text) in title_norms:
            seen.setdefault(normalize_skill(text), (text, "REQUIRED"))
    for text in fresh.must_have_skills:
        seen.setdefault(normalize_skill(text), (text, "REQUIRED"))
    for text in fresh.nice_to_have_skills:
        seen.setdefault(normalize_skill(text), (text, "PREFERRED"))
    for text in job.must_have_skills:
        if normalize_skill(text) not in example_only:
            seen.setdefault(normalize_skill(text), (text, "REQUIRED"))
    for text in job.nice_to_have_skills:
        if normalize_skill(text) not in example_only:
            seen.setdefault(normalize_skill(text), (text, "PREFERRED"))
    return list(seen.values())


_IMPORTANCE_RANK = {"REQUIRED": 2, "PREFERRED": 1, "UNKNOWN": 0}


@dataclass
class _Category:
    category: str
    examples: tuple[str, ...]
    example_text: str
    importance: str
    evidence: list[str]


def _category_requirements(job: JobMatchInput) -> dict[str, _Category]:
    """v8: the vacancy's category requirements, one per `category_key`. The
    title's "Python-Frameworks Engineer" and the description's "Python-
    Frameworks (z. B. Django, Flask)" are ONE requirement: the description's
    examples are kept, the strongest importance wins (a title category is
    REQUIRED) and every source text is job evidence."""
    merged: dict[str, _Category] = {}
    mentions = extract_category_requirements(job.title, job.description)
    # description first: its wording and examples are the richer source
    for group in sorted(mentions, key=lambda g: g.source == "title"):
        key = category_key(group.category)
        existing = merged.get(key)
        if existing is None:
            merged[key] = _Category(
                group.category,
                group.examples,
                group.example_text,
                group.importance,
                [group.evidence_text],
            )
            continue
        if not existing.example_text and group.example_text:
            existing.category = group.category
            existing.examples = group.examples
            existing.example_text = group.example_text
        if _IMPORTANCE_RANK[group.importance] > _IMPORTANCE_RANK[existing.importance]:
            existing.importance = group.importance
        if group.evidence_text not in existing.evidence:
            existing.evidence.append(group.evidence_text)
    return merged


def _match_example_groups(job: JobMatchInput, trusted_skills: list) -> list[RequirementMatch]:
    """v4: one requirement per "<category> (z. B. A, B)". MATCH only when a
    trusted skill IS one of the vacancy's own named examples (the vacancy
    itself states that example belongs to the category); otherwise UNKNOWN
    -- whether some other trusted skill belongs to the category is a human
    call, so it is never MISSING-by-assumption nor MATCH-by-guess.

    v8: a category named without examples ("Python-Frameworks Engineer",
    "Kenntnisse in LLM-Frameworks") is the same kind of requirement with no
    example to match: always UNKNOWN. Its base technology never satisfies
    it -- no taxonomy says which skills belong to the category."""
    trusted_index: dict[str, object] = {}
    for skill in trusted_skills:
        trusted_index.setdefault(normalize_skill(skill.name), skill)
    results: list[RequirementMatch] = []
    for normalized, group in _category_requirements(job).items():
        hits = [
            trusted_index[normalize_skill(e)]
            for e in group.examples
            if normalize_skill(e) in trusted_index
        ]
        requirement = group.category
        if group.example_text:
            requirement = f"{group.category} (examples: {group.example_text})"
        if hits:
            status = "MATCH"
            reason = (
                f"Trusted skill {', '.join(repr(s.name) for s in hits)} is one of the "
                f"vacancy's own examples of {group.category!r}."
            )
        elif group.example_text:
            status = "UNKNOWN"
            reason = (
                f"No trusted skill is one of the named examples ({group.example_text}); whether "
                f"another trusted skill counts as {group.category!r} needs human review "
                "(HUMAN_REVIEW_REQUIRED)."
            )
        else:
            status = "UNKNOWN"
            reason = (
                f"The vacancy names the category {group.category!r} without examples; whether "
                "a trusted skill belongs to it needs human review (HUMAN_REVIEW_REQUIRED)."
            )
        results.append(
            RequirementMatch(
                requirement=requirement,
                normalized_requirement=normalized,
                requirement_type="SKILL",
                importance=group.importance,
                match_status=status,
                candidate_evidence=[
                    CandidateEvidenceRef(entity_type="SKILL", entity_id=s.id, value=s.name)
                    for s in hits
                ],
                job_evidence=list(group.evidence),
                reason=reason,
            )
        )
    return results


def _unstored_skill_requirements(job: JobMatchInput) -> list[str]:
    stored = {normalize_skill(text) for text in [*job.must_have_skills, *job.nice_to_have_skills]}
    return [text for text, _ in _skill_requirements(job) if normalize_skill(text) not in stored]


def _match_skill_requirements(job: JobMatchInput, trusted_skills: list) -> list[RequirementMatch]:
    trusted_index: dict[str, object] = {}
    for skill in trusted_skills:
        trusted_index.setdefault(normalize_skill(skill.name), skill)

    results: list[RequirementMatch] = []
    for text, importance in _skill_requirements(job):
        normalized = normalize_skill(text)
        candidate_skill = trusted_index.get(normalized)
        related = [
            skill
            for key, skill in trusted_index.items()
            if key in _GENERIC_SKILL_RELATED.get(normalized, frozenset())
        ]
        if candidate_skill is None and related:
            results.append(
                RequirementMatch(
                    requirement=text,
                    normalized_requirement=normalized,
                    requirement_type="SKILL",
                    importance=importance,
                    match_status="UNKNOWN",
                    candidate_evidence=[
                        CandidateEvidenceRef(entity_type="SKILL", entity_id=s.id, value=s.name)
                        for s in related
                    ],
                    job_evidence=[text],
                    reason=(
                        f"Generic requirement {text!r}: related trusted skills "
                        f"{', '.join(repr(s.name) for s in related)} exist, but no explicit "
                        "taxonomy rule makes them satisfy it (HUMAN_REVIEW_REQUIRED)."
                    ),
                )
            )
        elif candidate_skill is not None:
            results.append(
                RequirementMatch(
                    requirement=text,
                    normalized_requirement=normalized,
                    requirement_type="SKILL",
                    importance=importance,
                    match_status="MATCH",
                    candidate_evidence=[
                        CandidateEvidenceRef(
                            entity_type="SKILL",
                            entity_id=candidate_skill.id,
                            value=candidate_skill.name,
                        )
                    ],
                    job_evidence=[text],
                    reason=f"Trusted candidate skill {candidate_skill.name!r} matches.",
                )
            )
        else:
            results.append(
                RequirementMatch(
                    requirement=text,
                    normalized_requirement=normalized,
                    requirement_type="SKILL",
                    importance=importance,
                    match_status="MISSING",
                    candidate_evidence=[],
                    job_evidence=[text],
                    reason=f"No trusted candidate skill matches {text!r}.",
                )
            )
    return results


def _match_language_requirements(
    job: JobMatchInput, trusted_languages: list
) -> list[RequirementMatch]:
    by_canonical: dict[str, object] = {}
    for lang in trusted_languages:
        canonical = canonicalize_language_name(lang.language)
        if canonical is not None:
            by_canonical.setdefault(canonical, lang)

    results: list[RequirementMatch] = []
    for req in extract_language_requirements(job.title, job.description):
        requirement_text = f"{req.language} {req.level}"
        normalized = f"{req.language.casefold()}-{req.level.casefold()}"
        candidate_lang = by_canonical.get(req.language)

        if candidate_lang is None:
            status = "MISSING"
            evidence: list[CandidateEvidenceRef] = []
            reason = f"No trusted {req.language} language record found."
        elif candidate_lang.level == "UNKNOWN":
            status = "UNKNOWN"
            evidence = [
                CandidateEvidenceRef(
                    entity_type="LANGUAGE",
                    entity_id=candidate_lang.id,
                    value=candidate_lang.language,
                )
            ]
            reason = f"Trusted {req.language} record has no stated CEFR level."
        else:
            candidate_rank = _CEFR_ORDER[candidate_lang.level]
            required_rank = _CEFR_ORDER[req.level]
            evidence = [
                CandidateEvidenceRef(
                    entity_type="LANGUAGE",
                    entity_id=candidate_lang.id,
                    value=candidate_lang.language,
                )
            ]
            if candidate_rank >= required_rank:
                status = "MATCH"
                reason = f"Trusted {req.language} level {candidate_lang.level} meets {req.level}."
            else:
                status = "PARTIAL"
                reason = (
                    f"Trusted {req.language} level {candidate_lang.level} is below {req.level}."
                )

        results.append(
            RequirementMatch(
                requirement=requirement_text,
                normalized_requirement=normalized,
                requirement_type="LANGUAGE",
                importance=req.importance,
                match_status=status,
                candidate_evidence=evidence,
                job_evidence=[req.evidence_text],
                reason=reason,
            )
        )
    return results


def _match_education_requirement(
    job: JobMatchInput, trusted_education: list
) -> RequirementMatch | None:
    req = extract_education_requirement(job.title, job.description)
    if req is None:
        return None

    completed = [edu for edu in trusted_education if edu.completed]
    evidence = completed
    requirement = "Completed degree"
    if req.fields:
        requirement = f"Completed degree ({', '.join(req.fields)} or comparable)"
    if not completed:
        status = "MISSING"
        reason = "No trusted, completed education record is on file."
    elif not req.fields:
        status = "MATCH"
        reason = "At least one trusted, completed education record is on file."
    else:
        # Stage 14B: a field-specific requirement is matched only by a
        # completed education literally naming one of those fields; any
        # other completed degree may be "comparable", which is a human call.
        evidence = [edu for edu in completed if set(_education_fields(edu)) & set(req.fields)]
        if evidence:
            status = "MATCH"
            reason = f"Trusted, completed education names {', '.join(req.fields)}."
        else:
            evidence = completed
            status = "UNKNOWN"
            reason = (
                f"A trusted, completed degree is on file, but none names "
                f"{', '.join(req.fields)}; whether it is a comparable qualification "
                "needs human review (HUMAN_REVIEW_REQUIRED)."
            )

    return RequirementMatch(
        requirement=requirement,
        normalized_requirement="completed-degree",
        requirement_type="EDUCATION",
        importance=req.importance,
        match_status=status,
        candidate_evidence=[
            CandidateEvidenceRef(entity_type="EDUCATION", entity_id=edu.id, value=edu.institution)
            for edu in evidence
        ],
        job_evidence=[req.evidence_text],
        reason=reason,
    )


def _education_fields(edu) -> tuple[str, ...]:
    return degree_field_names(" ".join(filter(None, (edu.field_of_study, edu.program, edu.degree))))


def _merged_days(periods: list[tuple[date, date]]) -> int:
    total = 0
    current_start: date | None = None
    current_end: date | None = None
    for start, end in sorted(periods):
        if current_end is None or start > current_end:
            if current_end is not None:
                total += (current_end - current_start).days
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += (current_end - current_start).days
    return total


def _match_experience_requirement(
    job: JobMatchInput,
    trusted_experiences: list,
    as_of: date,
) -> RequirementMatch | None:
    """Stage 14B: "at least N years of experience". v4: years are credited
    only from trusted EXPERIENCE rows that use a technology named in THAT
    requirement's own clause (`ExperienceRequirement.subjects`) -- relevance
    to some other vacancy skill is not enough. With no technology subject
    the requirement can never MATCH: UNKNOWN (human review) when trusted
    experience exists, MISSING when there is none. Projects never count.
    Overlapping periods are merged, never double counted."""
    req = extract_experience_requirement(job.title, job.description)
    if req is None:
        return None
    subject_norms = {normalize_skill(s) for s in req.subjects}
    using_any = [
        exp
        for exp in trusted_experiences
        if subject_norms & {normalize_skill(t) for t in exp.technologies}
    ]
    if req.subjects:
        joiner = {"ALL": " and ", "ANY": " or ", "AMBIGUOUS": ", "}[req.subject_mode]
        requirement = (
            f"At least {req.years} year(s) of professional experience with "
            f"{joiner.join(req.subjects)}"
        )
        # v6: "mit Python und Kubernetes" needs rows that use EVERY subject;
        # one subject's years never satisfy the whole quantified clause.
        # v7: "Python oder Kubernetes" is evaluated per alternative -- each
        # subject's own rows, merged only with each other; years of
        # different alternatives are never summed.
        if req.subject_mode == "ALL":
            alternatives = [
                [
                    exp
                    for exp in using_any
                    if subject_norms <= {normalize_skill(t) for t in exp.technologies}
                ]
            ]
        else:
            alternatives = [
                [
                    exp
                    for exp in using_any
                    if subject in {normalize_skill(t) for t in exp.technologies}
                ]
                for subject in sorted(subject_norms)
            ]
        # one row per experience even if it uses several alternatives
        relevant = list({id(exp): exp for rows in alternatives for exp in rows}.values())
    else:
        requirement = (
            f"At least {req.years} year(s) of professional experience ({req.subject_text})"
        )
        alternatives = []
        relevant = []
    evidence = [
        CandidateEvidenceRef(entity_type="EXPERIENCE", entity_id=exp.id, value=exp.job_title)
        for exp in relevant
    ]
    covered = {normalize_skill(t) for exp in using_any for t in exp.technologies}
    if req.subjects and req.subject_mode == "AMBIGUOUS" and using_any:
        status = "UNKNOWN"
        reason = (
            f"The required experience ({req.subject_text!r}) names several technologies "
            "without a clear 'and'/'or' relation; whether trusted experience satisfies it "
            "needs human review (HUMAN_REVIEW_REQUIRED)."
        )
    elif req.subjects and req.subject_mode == "ALL" and not relevant and subject_norms <= covered:
        evidence = [
            CandidateEvidenceRef(entity_type="EXPERIENCE", entity_id=exp.id, value=exp.job_title)
            for exp in using_any
        ]
        status = "UNKNOWN"
        reason = (
            "Every named technology is used in some trusted professional experience, but no "
            "single experience uses all of them, so the combined duration cannot be "
            "established (HUMAN_REVIEW_REQUIRED)."
        )
    elif not req.subjects and trusted_experiences:
        status = "UNKNOWN"
        reason = (
            f"The required experience ({req.subject_text!r}) names no technology that can be "
            "linked to structured experience; whether trusted experience qualifies needs "
            "human review (HUMAN_REVIEW_REQUIRED)."
        )
    elif not relevant:
        status = "MISSING"
        reason = (
            "No trusted professional experience uses the technology this requirement names; "
            "projects are not counted as professional experience."
            if req.subjects
            else "No trusted professional experience is on file."
        )
    else:
        status, reason = _duration_status(alternatives, req.years, as_of)
    return RequirementMatch(
        requirement=requirement,
        normalized_requirement=f"experience-{req.years}y",
        requirement_type="EXPERIENCE",
        importance=req.importance,
        match_status=status,
        candidate_evidence=evidence,
        job_evidence=[req.evidence_text],
        reason=reason,
    )


def _duration_status(alternatives: list[list], years: int, as_of: date) -> tuple[str, str]:
    """v7: MATCH when ONE alternative's own merged rows reach `years`;
    otherwise UNKNOWN when an alternative's rows lack dates (it might),
    PARTIAL when some alternative has dated rows, MISSING never (callers
    handle "no rows")."""
    best_days = 0
    undated = False
    for rows in alternatives:
        if not rows:
            continue
        if any(
            exp.start_date is None or (exp.end_date is None and not exp.is_current) for exp in rows
        ):
            undated = True
            continue
        days = _merged_days([(exp.start_date, exp.end_date or as_of) for exp in rows])
        if days >= years * 365:
            return "MATCH", f"Relevant trusted experience covers {days // 365} year(s) or more."
        best_days = max(best_days, days)
    if undated:
        return (
            "UNKNOWN",
            "Relevant trusted experience has no complete dates; duration cannot be computed.",
        )
    return (
        "PARTIAL",
        f"Relevant trusted experience covers {best_days} day(s), below {years} year(s).",
    )


# Words that carry no requirement of their own once the evaluated parts of
# a MIXED line are removed ("Kenntnisse in Python und ..." -> "Kenntnisse",
# "in", "und"). Only applied to lines that had at least one evaluated span;
# a line with none is always reported whole. v5: domains and category nouns
# ("Softwareentwicklung", "Frameworks", "Tools") are NOT filler.
_RESIDUAL_FILLER = frozenset(
    word.casefold()
    for word in """
    kenntnisse kenntnis kenntnissen fachkenntnisse grundkenntnisse erfahrung
    erfahrungen berufserfahrung praxiserfahrung grundverständnis verständnis
    umgang know-how sicherer sichere sicher gute guten gut sehr fundierte
    fundierten solide tiefe praktische erste mehrjährige idealerweise
    wünschenswert vorteil mindestens min über mehr
    im in mit und oder sowie von der die das den dem des ein eine einer einem
    für auf zu am an als bei bzw z b zum beispiel ist sind
    moderne modernen modern bereich umfeld
    fließend fließende fließendes fließenden verhandlungssicher
    verhandlungssichere verhandlungssicheres verhandlungssicheren wort schrift
    niveau level sprachkenntnisse fluent business
    knowledge of experience with and or the a an skills strong good solid
    basic understanding proficiency hands-on practical e g eg at least more than
    """.split()  # casefolded: "fließend" -> "fliessend"
)
_TOKEN = re.compile(r"[^\s,;:()/.]+(?:\.[^\s,;:()/.]+)*")
# v5: residual clauses -- split at punctuation, parentheses and conjunctions.
# v10: never at a German hyphen ellipsis ("Python- und Docker-Erfahrung",
# "Python-, Docker-Erfahrung"): the coordinated words share the last head.
_RESIDUAL_CLAUSE_BOUNDARY = re.compile(
    r"(?<!-)[,;]|[()]|(?<!-)\s(?:und|oder|sowie|bzw\.?|and|or|plus|as\s+well\s+as)\s",
    re.IGNORECASE,
)
_LEADING_WORD = re.compile(r"(?<![\w-])([\w][\w-]*)\s+$")
# v10: "<technology>- und " / "<technology>-, " before an experience compound.
_HYPHEN_ELLIPSIS = re.compile(
    r"[^\s,;()]+-(?:\s*,\s*|\s+(?:und|oder|bzw\.?|sowie|and|or|&)\s+)$", re.IGNORECASE
)
# v5: hyphen parts that attach to a technology without naming anything else
# ("Git-basierter", "Python-Kenntnisse", "CI/CD-Pipelines"); "Python-
# Frameworks" is NOT the technology, so that token stays material.
_ATTACHED_PARTS = frozenset(
    """
    basiert basierte basierter basiertes basierten basierend basierende based
    kenntnisse kenntnis erfahrung erfahrungen skills entwicklung development
    programmierung programming umgebung umgebungen environment stack
    pipeline pipelines
    """.split()
)
# v5: a noun before "(<technology>)" is consumed only when it names that
# technology's own category -- "API-Entwicklung (REST)". "SAP ABAP (Python)"
# or "XYZ-Plattform (Python)" keep their noun.
_TECHNOLOGY_CATEGORY_NOUNS: dict[str, frozenset[str]] = {
    "rest": frozenset(
        {"api", "apis", "api-entwicklung", "api-design", "schnittstellen", "webservices"}
    ),
}


_EXPERIENCE_SUFFIX = re.compile(r"-[\w-]*?(?:erfahrung(?:en)?|experience)", re.IGNORECASE)


def _technology_spans(segment: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for skill, pattern in _TECHNOLOGY_PATTERNS:
        for mention in pattern.finditer(segment):
            start, end = mention.start(), mention.end()
            while (
                start > 0 and not segment[start - 1].isspace() and segment[start - 1] not in "(,;:/"
            ):
                start -= 1
            while (
                end < len(segment) and not segment[end].isspace() and segment[end] not in "),.;:/"
            ):
                end += 1
            # v9: "-Erfahrung" is an experience compound, not an attached
            # part -- the technology alone is evaluated, the experience
            # head stays visible to the residual net.
            if _EXPERIENCE_SUFFIX.fullmatch(segment, mention.end(), end):
                end = mention.end()
            attached = (
                segment[start : mention.start()] + "-" + segment[mention.end() : end]
            ).split("-")
            if all(len(part) <= 2 or part.casefold() in _ATTACHED_PARTS for part in attached):
                spans.append((start, end))
            open_paren = segment.rfind("(", 0, mention.start())
            close_paren = segment.find(")", mention.end())
            if (
                open_paren != -1
                and close_paren != -1
                and ")" not in segment[open_paren : mention.start()]
            ):
                before = segment[:open_paren].rstrip()
                noun_start = before.rfind(" ") + 1
                if before[noun_start:].casefold() in _TECHNOLOGY_CATEGORY_NOUNS.get(skill, ()):
                    spans.append((noun_start, len(before)))
    return spans


def _evaluated_spans(job: JobMatchInput, segment: str, groups: list) -> list[tuple[int, int]]:
    """v5: character spans of `segment` some typed requirement evaluated --
    technology mentions (`_technology_spans`), example groups, and the
    spans the language/education/experience extractors report."""
    spans = [group.span for group in groups if group.evidence_text == segment]
    spans.extend(_technology_spans(segment))
    for language in extract_language_requirements(job.title, job.description):
        if language.evidence_text == segment and language.span is not None:
            spans.append(language.span)
    education = extract_education_requirement(job.title, job.description)
    if education is not None and education.evidence_text == segment:
        spans.extend(education.spans)
    experience = extract_experience_requirement(job.title, job.description)
    if experience is not None and experience.evidence_text == segment and experience.span:
        spans.append(experience.span)
    return spans


def _is_material(token: str) -> bool:
    word = token.strip("-")
    return len(word) > 1 and word.casefold() not in _RESIDUAL_FILLER


@dataclass(frozen=True)
class _Clause:
    tokens: list[re.Match[str]]
    owned: bool  # some typed requirement evaluated part of it
    conjunction_after: bool  # the boundary after it is "und"/"and"/..., not punctuation
    start: int = 0  # v10: where the clause starts in the segment
    # v11: starts of experience heads that directly follow an evaluated
    # technology of this clause ("Python experience", "Python Erfahrung")
    compound: frozenset[int] = frozenset()


def _is_head(token: str) -> bool:
    return REQUIREMENT_HEAD.fullmatch(token.strip("-")) is not None


def _qualified_experience(clause: _Clause) -> list[re.Match[str]]:
    # v11: a technology compound is qualified like "Python-Erfahrung"
    words = ["-" + t.group() if t.start() in clause.compound else t.group() for t in clause.tokens]
    return [clause.tokens[i] for i in qualified_experience_words(words)]


def _technology_compounds(
    segment: str,
    tokens: list[re.Match[str]],
    technology: list[tuple[int, int]],
    clause_start: int,
) -> frozenset[int]:
    """v11: experience heads separated only by whitespace from an evaluated
    technology inside the same clause ("Python experience") -- the
    experience is WITH that technology, so masking the SKILL span must not
    turn it into a bare head. Never across the clause start ("Python;
    experience").
    """
    starts: set[int] = set()
    for token in tokens:
        word = token.group()
        if word.startswith("-") or not EXPERIENCE_HEAD.fullmatch(word):
            continue
        i = token.start()
        while i > clause_start and segment[i - 1] in " \t":
            i -= 1
        if clause_start < i < token.start() and any(
            t_start <= i - 1 < t_end for t_start, t_end in technology
        ):
            starts.add(token.start())
    return frozenset(starts)


def _shares_right_complement(clause: _Clause, right: _Clause | None, masked: str) -> bool:
    """v6/v7: a BARE head clause ("Kenntnisse", "knowledge of") coordinated
    with a right clause that itself starts with a requirement head
    (`starts_requirement_clause`, the extractors' own predicate) shares that
    clause's complement ("Kenntnisse und Erfahrung in Python"). A qualified
    head on either side ("mehrjährige Erfahrung", "practical experience",
    "Berufserfahrung") is never shared away -- the right side's qualifier is
    kept by `_residual_clauses` itself."""
    if right is None or not clause.conjunction_after or not right.tokens:
        return False
    if any(_is_material(t.group()) for t in clause.tokens) or _qualified_experience(clause):
        return False
    return starts_requirement_clause(masked[right.tokens[0].start() : right.tokens[-1].end()])


def _residual_clauses(segment: str, spans: list[tuple[int, int]]) -> list[str]:
    """v5: blank out `spans`, split the rest into clauses, and return each
    clause's material words (from its first to its last material token).

    v6 clause ownership: a clause none of whose text a typed requirement
    evaluated is independent of them, so it is kept even when every word is
    in `_RESIDUAL_FILLER` as long as a `REQUIREMENT_HEAD` word heads it
    ("Deutsch B2 und mehrjährige Berufserfahrung" -> "mehrjährige
    Berufserfahrung"); filler inside an owned clause ("Kenntnisse in
    Python") stays filler. Only a bare "Kenntnisse"/"Erfahrung" coordinated
    with an evaluated head ("Kenntnisse und Erfahrung in Python") is not a
    clause of its own.

    v7: qualified experience wording (`qualified_experience_words`:
    "Berufserfahrung", "mehrjährige Erfahrung", "practical experience") is
    material in EVERY clause -- a SKILL requirement for the same technology
    does not evaluate it; only a typed EXPERIENCE span (masked) does."""
    chars = list(segment)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    masked = "".join(chars)
    technology = _technology_spans(segment)
    pieces: list[_Clause] = []
    position = 0
    # v10: the hyphen of an ellipsis may be masked with its technology, so the
    # original segment decides ("Python- und Docker-Erfahrung" is one clause).
    boundaries = [
        b
        for b in _RESIDUAL_CLAUSE_BOUNDARY.finditer(masked)
        if b.group() in "()" or segment[b.start() - 1 : b.start()] != "-"
    ]
    for boundary in [*boundaries, None]:
        end = boundary.start() if boundary is not None else len(masked)
        pieces.append(
            _Clause(
                tokens=(tokens := list(_TOKEN.finditer(masked, position, end))),
                owned=any(
                    span_start < end and position < span_end for span_start, span_end in spans
                ),
                conjunction_after=boundary is not None and boundary.group() not in ",;()",
                start=position,
                compound=_technology_compounds(segment, tokens, technology, position),
            )
        )
        if boundary is not None:
            position = boundary.end()
    clauses: list[str] = []
    for index, clause in enumerate(pieces):
        kept = [t for t in clause.tokens if _is_material(t.group())]
        qualified = _qualified_experience(clause)
        kept.extend(t for t in qualified if t not in kept)
        if not kept and not clause.owned and any(_is_head(t.group()) for t in clause.tokens):
            right = pieces[index + 1] if index + 1 < len(pieces) else None
            if not _shares_right_complement(clause, right, masked):
                kept = clause.tokens
        if kept:
            kept.sort(key=lambda t: t.start())
            clauses.append(_clause_text(segment, masked, kept, qualified, clause.start))
    return clauses


def _clause_text(
    segment: str,
    masked: str,
    kept: list[re.Match[str]],
    qualified: list[re.Match[str]],
    clause_start: int,
) -> str:
    """The kept clause's text.

    v10: a qualified experience phrase keeps its technology subjects -- the
    technology is evaluated as a SKILL requirement, but the experience is
    WITH it, so masking the SKILL span must not generalize the experience
    requirement. Restored (only evaluated technology spans, never before
    the phrase's clause start): technologies inside the phrase
    ("professional Python experience", "gute Python-Erfahrung"), one
    directly before it ("Python-Erfahrung", "Python professional
    experience"), and hyphen-ellipsis coordinations before that ("Python-
    und Docker-Erfahrung", "Python-, Java- und Docker-Erfahrung")."""
    technology = _technology_spans(segment)
    display = list(masked)

    def restore(start: int, end: int) -> None:
        for i in range(start, end):
            if segment[i].isspace() or any(t_start <= i < t_end for t_start, t_end in technology):
                display[i] = segment[i]

    start = kept[0].start()
    group: list[re.Match[str]] = []
    for token in sorted(qualified, key=lambda t: t.start()):
        group.append(token)
        if not EXPERIENCE_HEAD.fullmatch(token.group().strip("-")):
            continue
        phrase_start = group[0].start()
        group = []
        coordinated = False
        restore(phrase_start, token.start())
        while True:
            # a technology directly before the phrase (at most whitespace between)
            gap = phrase_start
            while gap > clause_start and segment[gap - 1] in " \t":
                gap -= 1
            run = gap
            while run > clause_start and masked[run - 1] == " " and not segment[run - 1].isspace():
                run -= 1
            if run < gap and any(t_start <= run < t_end for t_start, t_end in technology):
                restore(run, phrase_start)
                phrase_start = run
                continue
            # "<technology>- und " coordinated with it
            ellipsis = _HYPHEN_ELLIPSIS.search(segment, clause_start, phrase_start)
            if ellipsis is not None:
                restore(ellipsis.start(), phrase_start)
                phrase_start = ellipsis.start()
                coordinated = True
                continue
            # a modifier of the whole coordination ("gute Python- und Docker-")
            word = _LEADING_WORD.search(masked, clause_start, phrase_start)
            if (
                coordinated
                and word is not None
                and not _CLAUSE_STOP_WORD.fullmatch(word.group(1))
                and not REQUIREMENT_HEAD.fullmatch(word.group(1))
            ):
                restore(word.start(), phrase_start)
                phrase_start = word.start()
                continue
            break
        start = min(start, phrase_start)
    return " ".join("".join(display)[start : kept[-1].end()].split())


def _other_identity(text: str) -> str:
    """Casefolded, whitespace-collapsed, trailing punctuation ignored (v5)."""
    return " ".join(text.casefold().split()).rstrip(" .!?;:,")


def _unevaluated_requirements(
    job: JobMatchInput,
) -> list[RequirementMatch]:
    """Stage 14B fail-closed net over every segment of a recognized
    requirement section ("Ihr Profil", "Profil", ...):

    - v5: the spans every typed requirement (SKILL, group, LANGUAGE,
      EDUCATION, EXPERIENCE) evaluated are removed and each remaining
      material clause ("Deutsch B2 und Erfahrung mit SAP ABAP" -> "SAP
      ABAP") becomes a REQUIRED, UNKNOWN OTHER requirement -- a recognized
      part never hides the rest of its line; a line with no evaluated span
      is reported whole;
    - OTHER requirements are deduplicated by casefolded, whitespace-
      collapsed text without trailing punctuation (repeated vacancy lines
      count once; every source line is kept as job evidence).

    An OTHER requirement can never be a MATCH."""
    # v8: every category mention (example groups and bare "<technology>-
    # Frameworks" tokens) is evaluated by its category requirement.
    groups = extract_category_requirements(job.title, job.description)
    by_identity: dict[str, RequirementMatch] = {}
    for segment in must_section_segments(job.description):
        spans = _evaluated_spans(job, segment, groups)
        for text in _residual_clauses(segment, spans) if spans else [segment]:
            identity = _other_identity(text)
            existing = by_identity.get(identity)
            if existing is not None:
                if segment not in existing.job_evidence:
                    existing.job_evidence.append(segment)
                continue
            by_identity[identity] = _other_requirement(text, identity, segment)
    return list(by_identity.values())


def _other_requirement(text: str, identity: str, segment: str) -> RequirementMatch:
    return RequirementMatch(
        requirement=text,
        normalized_requirement=identity,
        requirement_type="OTHER",
        importance="REQUIRED",
        match_status="UNKNOWN",
        candidate_evidence=[],
        job_evidence=[segment],
        reason=(
            "Required vacancy text that the deterministic matcher cannot evaluate "
            "(NOT_EVALUATED); human review required (HUMAN_REVIEW_REQUIRED)."
        ),
    )


def _relevant_experiences(
    trusted_experiences: list, required_skill_norms: set[str]
) -> list[RelevantExperience]:
    results = []
    for exp in trusted_experiences:
        matched = sorted(
            {tech for tech in exp.technologies if normalize_skill(tech) in required_skill_norms}
        )
        if matched:
            results.append(
                RelevantExperience(
                    experience_id=exp.id,
                    company=exp.company,
                    job_title=exp.job_title,
                    matched_skills=matched,
                )
            )
    return results


def _relevant_projects(
    trusted_projects: list, required_skill_norms: set[str]
) -> list[RelevantProject]:
    results = []
    for project in trusted_projects:
        matched = sorted(
            {tech for tech in project.technologies if normalize_skill(tech) in required_skill_norms}
        )
        if matched:
            results.append(
                RelevantProject(project_id=project.id, name=project.name, matched_skills=matched)
            )
    return results


def _score(fraction: float | None, default: int) -> int:
    if fraction is None:
        return default
    return round(fraction * 100)


def _coverage_fraction(requirements: list[RequirementMatch], importance: str) -> float | None:
    subset = [r for r in requirements if r.importance == importance]
    if not subset:
        return None
    return sum(_MATCH_POINTS[r.match_status] for r in subset) / len(subset)


def _weighted_coverage_fraction(requirements: list[RequirementMatch]) -> float | None:
    if not requirements:
        return None
    total_weight = sum(_IMPORTANCE_WEIGHT[r.importance] for r in requirements)
    weighted_points = sum(
        _IMPORTANCE_WEIGHT[r.importance] * _MATCH_POINTS[r.match_status] for r in requirements
    )
    return weighted_points / total_weight


def compute_match(
    job: JobMatchInput,
    profile: CandidateProfile,
    *,
    company_research_id: int | None,
    as_of: date | None = None,
) -> CandidateJobMatchData:
    """`as_of` is the end date for a current (open-ended) experience in the
    Stage 14B years-of-experience check; defaults to today."""
    as_of = as_of or date.today()
    trusted_skills = _trusted(profile.skills)
    trusted_experiences = _trusted(profile.experiences)
    trusted_education = _trusted(profile.education)
    trusted_projects = _trusted(profile.projects)
    trusted_languages = _trusted(profile.languages)

    all_requirements: list[RequirementMatch] = []
    all_requirements.extend(_match_skill_requirements(job, trusted_skills))
    all_requirements.extend(_match_example_groups(job, trusted_skills))
    all_requirements.extend(_match_language_requirements(job, trusted_languages))
    education_match = _match_education_requirement(job, trusted_education)
    if education_match is not None:
        all_requirements.append(education_match)

    required_skill_norms = {normalize_skill(text) for text, importance in _skill_requirements(job)}
    relevant_experiences = _relevant_experiences(trusted_experiences, required_skill_norms)
    relevant_projects = _relevant_projects(trusted_projects, required_skill_norms)

    experience_match = _match_experience_requirement(job, trusted_experiences, as_of)
    if experience_match is not None:
        all_requirements.append(experience_match)
    all_requirements.extend(_unevaluated_requirements(job))

    has_education_requirement = education_match is not None
    relevant_education = (
        [
            RelevantEducation(
                education_id=edu.id, institution=edu.institution, degree=edu.degree, completed=True
            )
            for edu in trusted_education
            if edu.completed
        ]
        if has_education_requirement
        else []
    )

    language_requirement_names = {
        req.language for req in extract_language_requirements(job.title, job.description)
    }
    relevant_languages = [
        RelevantLanguage(language_id=lang.id, language=lang.language, level=lang.level)
        for lang in trusted_languages
        if canonicalize_language_name(lang.language) in language_requirement_names
    ]

    matched_requirements = [r for r in all_requirements if r.match_status == "MATCH"]
    partial_requirements = [r for r in all_requirements if r.match_status == "PARTIAL"]
    missing_requirements = [r for r in all_requirements if r.match_status == "MISSING"]
    unknown_requirements = [r for r in all_requirements if r.match_status == "UNKNOWN"]

    required_skill_score = _score(
        _coverage_fraction(all_requirements, "REQUIRED"), _NEUTRAL_NO_REQUIREMENTS
    )
    preferred_skill_score = _score(
        _coverage_fraction(all_requirements, "PREFERRED"), _TRIVIALLY_SATISFIED
    )
    coverage_score = _score(_weighted_coverage_fraction(all_requirements), _NEUTRAL_NO_REQUIREMENTS)

    required_skill_norms_only = {
        r.normalized_requirement
        for r in all_requirements
        if r.requirement_type == "SKILL"
        and r.importance == "REQUIRED"
        # v4: a grouped "<category> (z. B. ...)" requirement is not a single
        # named skill an experience/project could list.
        and not r.normalized_requirement.startswith("group:")
    }
    if required_skill_norms_only:
        # v2+: the share of REQUIRED skills backed by at least one relevant
        # experience/project (v1 divided the number of backing ROWS by the
        # number of skills, so one row covering every skill scored low and
        # three rows covering one skill scored high).
        backed = {
            normalize_skill(name)
            for item in (*relevant_experiences, *relevant_projects)
            for name in item.matched_skills
        }
        experience_support_fraction = len(backed & required_skill_norms_only) / len(
            required_skill_norms_only
        )
        experience_support_score = _score(experience_support_fraction, _NEUTRAL_NO_REQUIREMENTS)
    else:
        experience_support_score = _NEUTRAL_NO_REQUIREMENTS
    if experience_match is not None:
        # Stage 14B: projects cannot stand in for a stated years-of-
        # experience requirement -- support scales with that requirement's
        # own points (MISSING/UNKNOWN -> 0, PARTIAL -> half).
        experience_support_score = round(
            experience_support_score * _MATCH_POINTS[experience_match.match_status]
        )

    overall_score = round(
        required_skill_score * 0.6 + preferred_skill_score * 0.2 + experience_support_score * 0.2
    )

    safe_candidate_claims = _build_safe_claims(
        profile,
        matched_requirements + partial_requirements + unknown_requirements,
        relevant_experiences,
        relevant_projects,
        relevant_education,
        relevant_languages,
    )

    warnings = _build_warnings(
        all_requirements,
        trusted_skills,
        trusted_experiences,
        trusted_education,
        trusted_languages,
        trusted_projects,
    )
    unstored = _unstored_skill_requirements(job)
    if unstored:
        warnings.append(
            "Vacancy text names skill requirements absent from the stored extraction "
            f"(re-extracted for this match): {', '.join(sorted(unstored))}"
        )

    return CandidateJobMatchData(
        job_id=job.job_id,
        candidate_profile_version=profile.profile_version,
        company_research_id=company_research_id,
        algorithm_version=ALGORITHM_VERSION,
        overall_score=overall_score,
        coverage_score=coverage_score,
        required_skill_score=required_skill_score,
        preferred_skill_score=preferred_skill_score,
        experience_support_score=experience_support_score,
        matched_requirements=matched_requirements,
        partial_requirements=partial_requirements,
        missing_requirements=missing_requirements,
        unknown_requirements=unknown_requirements,
        relevant_experiences=relevant_experiences,
        relevant_projects=relevant_projects,
        relevant_education=relevant_education,
        relevant_languages=relevant_languages,
        safe_candidate_claims=safe_candidate_claims,
        warnings=warnings,
    )


def _build_safe_claims(
    profile: CandidateProfile,
    evidenced_requirements: list[RequirementMatch],
    relevant_experiences: list[RelevantExperience],
    relevant_projects: list[RelevantProject],
    relevant_education: list[RelevantEducation],
    relevant_languages: list[RelevantLanguage],
) -> list[SafeCandidateClaim]:
    """Structured, factual claims (section 15) — deduplicated by
    (claim_type, source_id), covering only facts actually used as evidence
    somewhere in this match (an evidence-linked subset, not a dump of every
    trusted profile fact). Phrasing/prose is explicitly Stage 6C's job.
    """
    seen: set[tuple[str, int]] = set()
    claims: list[SafeCandidateClaim] = []

    def _add(claim_type: str, claim: str, source_entity: str, source_id: int) -> None:
        key = (claim_type, source_id)
        if key in seen:
            return
        seen.add(key)
        claims.append(
            SafeCandidateClaim(
                claim_type=claim_type,
                claim=claim,
                source_entity=source_entity,
                source_id=source_id,
                profile_version=profile.profile_version,
            )
        )

    for req in evidenced_requirements:
        for ref in req.candidate_evidence:
            if ref.entity_type == "SKILL":
                _add("SKILL", ref.value, "candidate_skill", ref.entity_id)
            elif ref.entity_type == "LANGUAGE":
                _add("LANGUAGE", ref.value, "candidate_language", ref.entity_id)
            elif ref.entity_type == "EDUCATION":
                _add("EDUCATION", ref.value, "candidate_education", ref.entity_id)

    for exp in relevant_experiences:
        _add(
            "EXPERIENCE",
            f"{exp.job_title} at {exp.company}",
            "candidate_experience",
            exp.experience_id,
        )
    for project in relevant_projects:
        _add("PROJECT", project.name, "candidate_project", project.project_id)
    for edu in relevant_education:
        _add("EDUCATION", edu.institution, "candidate_education", edu.education_id)
    for lang in relevant_languages:
        _add("LANGUAGE", f"{lang.language} ({lang.level})", "candidate_language", lang.language_id)

    return claims


def _build_warnings(
    all_requirements: list[RequirementMatch],
    trusted_skills: list,
    trusted_experiences: list,
    trusted_education: list,
    trusted_languages: list,
    trusted_projects: list,
) -> list[str]:
    warnings: list[str] = []
    for r in all_requirements:
        if r.importance != "REQUIRED":
            continue
        if r.match_status == "MISSING":
            warnings.append(f"Missing required {r.requirement_type.lower()}: {r.requirement}")
        elif r.match_status == "UNKNOWN":
            warnings.append(
                f"Required {r.requirement_type.lower()} status unknown: {r.requirement}"
            )

    if not (
        trusted_skills
        or trusted_experiences
        or trusted_education
        or trusted_languages
        or trusted_projects
    ):
        warnings.append(
            "Candidate profile has no confirmed facts yet — match coverage is not meaningful."
        )
    return warnings
