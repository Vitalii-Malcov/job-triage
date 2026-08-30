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
  bare CandidateSkill row): `min(1.0, (len(relevant_experiences) +
  len(relevant_projects)) / count(REQUIRED skill requirements)) * 100`.
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

**algorithm_version.** `"v1"` — bumped whenever this scoring/matching logic
changes in a way that would produce a different result for the same
inputs, so a cached analysis from a future v2 is never silently compared
to or reused as a v1 result (see app/db/candidate_job_match_repository.py's
cache-identity key).
"""

from dataclasses import dataclass, field

from app.agents.job_scorer import normalize_skill
from app.agents.requirement_extractor import (
    canonicalize_language_name,
    extract_education_requirement,
    extract_language_requirements,
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

ALGORITHM_VERSION = "v1"

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
    """
    seen: dict[str, tuple[str, str]] = {}
    for text in job.must_have_skills:
        seen[normalize_skill(text)] = (text, "REQUIRED")
    for text in job.nice_to_have_skills:
        key = normalize_skill(text)
        if key not in seen:
            seen[key] = (text, "PREFERRED")
    return list(seen.values())


def _match_skill_requirements(job: JobMatchInput, trusted_skills: list) -> list[RequirementMatch]:
    trusted_index: dict[str, object] = {}
    for skill in trusted_skills:
        trusted_index.setdefault(normalize_skill(skill.name), skill)

    results: list[RequirementMatch] = []
    for text, importance in _skill_requirements(job):
        normalized = normalize_skill(text)
        candidate_skill = trusted_index.get(normalized)
        if candidate_skill is not None:
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
    if completed:
        status = "MATCH"
        reason = "At least one trusted, completed education record is on file."
    else:
        status = "MISSING"
        reason = "No trusted, completed education record is on file."

    return RequirementMatch(
        requirement="Completed degree",
        normalized_requirement="completed-degree",
        requirement_type="EDUCATION",
        importance=req.importance,
        match_status=status,
        candidate_evidence=[
            CandidateEvidenceRef(entity_type="EDUCATION", entity_id=edu.id, value=edu.institution)
            for edu in completed
        ],
        job_evidence=[req.evidence_text],
        reason=reason,
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
) -> CandidateJobMatchData:
    trusted_skills = _trusted(profile.skills)
    trusted_experiences = _trusted(profile.experiences)
    trusted_education = _trusted(profile.education)
    trusted_projects = _trusted(profile.projects)
    trusted_languages = _trusted(profile.languages)

    all_requirements: list[RequirementMatch] = []
    all_requirements.extend(_match_skill_requirements(job, trusted_skills))
    all_requirements.extend(_match_language_requirements(job, trusted_languages))
    education_match = _match_education_requirement(job, trusted_education)
    if education_match is not None:
        all_requirements.append(education_match)

    required_skill_norms = {normalize_skill(text) for text, importance in _skill_requirements(job)}
    relevant_experiences = _relevant_experiences(trusted_experiences, required_skill_norms)
    relevant_projects = _relevant_projects(trusted_projects, required_skill_norms)

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

    required_skill_requirement_count = sum(
        1 for r in all_requirements if r.requirement_type == "SKILL" and r.importance == "REQUIRED"
    )
    if required_skill_requirement_count:
        experience_support_fraction = min(
            1.0,
            (len(relevant_experiences) + len(relevant_projects)) / required_skill_requirement_count,
        )
        experience_support_score = _score(experience_support_fraction, _NEUTRAL_NO_REQUIREMENTS)
    else:
        experience_support_score = _NEUTRAL_NO_REQUIREMENTS

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
