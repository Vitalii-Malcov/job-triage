"""Trusted rendering of Bewerbung prose from a validated provider plan
(Stage 6D blocker fix).

**The central invariant (spec section 28).** No provider-controlled text
ever reaches `subject`/`salutation`/`opening`/`body_paragraphs`/`closing`/
`signature_name`. A provider (`app.providers.bewerbung.base.BewerbungProvider`)
returns a raw, untrusted mapping; `parse_plan` is the only thing allowed to
turn it into a `BewerbungProviderPlan` (strict, `extra="forbid"` schema —
see `app.models.bewerbung`), and `resolve_plan` is the only thing allowed
to turn that plan's claim ids into actual evidence records — by exact
dictionary lookup against `EvidenceRegistry`, never by name-matching or any
other fuzzy fallback (spec section 5: "No: if unknown id: try matching by
name"). `render_draft` then builds every sentence from *those* records via
fixed, record-specific templates. A provider's own prose (if it tried to
smuggle any into an unexpected field) is rejected at the `extra="forbid"`
schema boundary before any of this even runs.

**Why this replaces regex-based post-generation validation.** The
previous design let a provider return free-form `opening`/`body_paragraphs`
/`closing` strings plus a self-reported `used_claim_ids` list, and tried to
catch unsupported factual content by scanning the resulting text for
specific patterns (forbidden skill names, CEFR codes, "abschluss" stems,
...). That is inherently incomplete — text the regexes don't recognize
still renders unchanged. Under this architecture that whole class of
defect is structurally impossible: the provider never supplies prose, so
there is no "arbitrary text" left to scan for hallucinated content in the
first place. Tests in `tests/test_bewerbung_renderer.py` prove this by
construction (e.g. a forbidden skill's name literally cannot appear in
`EvidenceRegistry` and therefore cannot appear in any rendered sentence),
not by re-scanning rendered output.

**Record-specific templates (spec section 6-13).** Every `EvidenceRecord`
subtype owns exactly the fields it needs to render its own claim type
truthfully:
  - `SkillEvidenceRecord` — a skill name and nothing else; cannot smuggle
    in an unrelated skill (spec section 6).
  - `ExperienceEvidenceRecord` — one specific experience's own company,
    role, and technologies; never a technology recorded elsewhere in the
    profile (spec section 7/13).
  - `ProjectEvidenceRecord` — a project's own name and technologies.
  - `LanguageEvidenceRecord` — one specific language's own level; a B1
    record can never render "B2" or "Muttersprache" (spec section 8/9) —
    `NATIVE` renders distinctly from every CEFR level.

**Education/certifications are deliberately not exposed to provider
selection at all in v1 (spec section 34/35's documented "safest option").**
`build_evidence` never emits `candidate_education`/`candidate_certification`
claims, so no rendered sentence can ever reference completion/certification
status — the safest possible answer to "how do we keep an incomplete degree
from being rendered as complete" is to never let a degree be rendered at
all yet. Company facts are excluded the same way: no `EvidenceRecord`
subtype exists for company-culture claims, so no template could ever
produce "innovative Kultur"-style prose (spec section 20/40) even if a
provider asked for it. Numeric/duration claims (spec section 21/22) are
absent for the identical reason: no `EvidenceRecord` subtype carries a
number, and no template interpolates one — `years_experience` exists on
`CVSkillItem` but is deliberately never read here, exactly mirroring the
education/certification exclusion.
"""

from typing import Literal

from pydantic import BaseModel, ValidationError

from app.models.bewerbung import (
    AllowedClaim,
    BewerbungEvidenceCandidate,
    BewerbungEvidenceJob,
    BewerbungEvidencePacket,
    BewerbungParagraph,
    BewerbungProviderPlan,
)
from app.models.candidate_job_match import CandidateJobMatch
from app.models.cv_draft import TailoredCVDraft

_MAX_PROJECT_CLAIMS = 3
_MAX_EXPERIENCE_CLAIMS = 3
_MAX_LANGUAGE_CLAIMS = 5


class BewerbungPlanRejectedError(Exception):
    """The provider's plan failed schema validation or claim-id resolution
    (spec section 5/24/29/47) — the draft is never rendered or persisted.
    `codes` are fixed, bounded strings (a violation kind plus, at most, a
    small numeric paragraph index) — never attacker-controlled text, so
    this is always safe to return in an HTTP error body or log line.
    """

    def __init__(self, codes: list[str]) -> None:
        self.codes = codes
        super().__init__("Provider plan failed validation.")


# --- evidence records (renderer-only; never sent to a provider) -----------


class SkillEvidenceRecord(BaseModel):
    kind: Literal["SKILL"] = "SKILL"
    name: str


class ExperienceEvidenceRecord(BaseModel):
    kind: Literal["EXPERIENCE"] = "EXPERIENCE"
    company: str
    role: str
    technologies: list[str] = []


class ProjectEvidenceRecord(BaseModel):
    kind: Literal["PROJECT"] = "PROJECT"
    name: str
    technologies: list[str] = []


class LanguageEvidenceRecord(BaseModel):
    kind: Literal["LANGUAGE"] = "LANGUAGE"
    language: str
    level: str


EvidenceRecord = (
    SkillEvidenceRecord | ExperienceEvidenceRecord | ProjectEvidenceRecord | LanguageEvidenceRecord
)
EvidenceRegistry = dict[str, EvidenceRecord]


class RenderedBewerbung(BaseModel):
    """The trusted renderer's complete output — every field here is safe
    to persist verbatim as the final draft content."""

    subject: str
    salutation: str
    opening: str
    body_paragraphs: list[BewerbungParagraph]
    closing: str


def _claim_id(source_entity: str, source_id: int) -> str:
    return f"{source_entity}:{source_id}"


def build_evidence(
    cv_draft: TailoredCVDraft,
    match: CandidateJobMatch,
    job_title: str,
    job_company: str,
    job_description: str,
) -> tuple[BewerbungEvidencePacket, EvidenceRegistry]:
    """Build both the provider-facing evidence packet and the
    renderer-only evidence registry from the same pinned 6C CV draft, in
    one pass, so the two can never drift out of sync (same claim ids, same
    filtering) — never from the live Candidate Profile (spec section 2).
    """
    header = cv_draft.header
    candidate = BewerbungEvidenceCandidate(
        professional_title=header.professional_title.value if header.professional_title else None,
        summary=cv_draft.professional_summary.value if cv_draft.professional_summary else None,
        first_name=header.first_name.value if header.first_name else None,
        last_name=header.last_name.value if header.last_name else None,
    )

    allowed_claims: list[AllowedClaim] = []
    registry: EvidenceRegistry = {}

    for skill in cv_draft.skills:
        claim_id = _claim_id("candidate_skill", skill.source_id)
        allowed_claims.append(
            AllowedClaim(
                id=claim_id,
                claim=skill.text,
                source_entity="candidate_skill",
                source_id=skill.source_id,
            )
        )
        registry[claim_id] = SkillEvidenceRecord(name=skill.text)

    for project in cv_draft.projects[:_MAX_PROJECT_CLAIMS]:
        claim_id = _claim_id("candidate_project", project.source_id)
        allowed_claims.append(
            AllowedClaim(
                id=claim_id,
                claim=project.name,
                source_entity="candidate_project",
                source_id=project.source_id,
            )
        )
        registry[claim_id] = ProjectEvidenceRecord(
            name=project.name, technologies=list(project.technologies)
        )

    # Only match-relevant experience (matched_skills non-empty) — a cover
    # letter references what's relevant to this vacancy, not the full
    # career history 6C already shows in full.
    relevant_experience = [exp for exp in cv_draft.experience if exp.matched_skills]
    for experience in relevant_experience[:_MAX_EXPERIENCE_CLAIMS]:
        claim_id = _claim_id("candidate_experience", experience.source_id)
        allowed_claims.append(
            AllowedClaim(
                id=claim_id,
                claim=f"{experience.job_title} bei {experience.company}",
                source_entity="candidate_experience",
                source_id=experience.source_id,
            )
        )
        # This experience's own technologies only — never the candidate's
        # global skill list (spec section 7/13/36).
        registry[claim_id] = ExperienceEvidenceRecord(
            company=experience.company,
            role=experience.job_title,
            technologies=list(experience.technologies),
        )

    # UNKNOWN level carries no renderable fact — excluded rather than
    # rendered as a vague/empty claim.
    for language in cv_draft.languages[:_MAX_LANGUAGE_CLAIMS]:
        if language.level == "UNKNOWN":
            continue
        claim_id = _claim_id("candidate_language", language.source_id)
        allowed_claims.append(
            AllowedClaim(
                id=claim_id,
                claim=f"{language.language} ({language.level})",
                source_entity="candidate_language",
                source_id=language.source_id,
            )
        )
        registry[claim_id] = LanguageEvidenceRecord(
            language=language.language, level=language.level
        )

    forbidden_claims = [
        requirement.requirement
        for requirement in (match.missing_requirements + match.unknown_requirements)
    ]

    evidence_job = BewerbungEvidenceJob(
        title=job_title,
        company=job_company,
        description=job_description,
        matched_requirements=[r.requirement for r in match.matched_requirements],
        partial_requirements=[r.requirement for r in match.partial_requirements],
    )

    packet = BewerbungEvidencePacket(
        candidate=candidate,
        job=evidence_job,
        allowed_claims=allowed_claims,
        forbidden_claims=forbidden_claims,
    )
    return packet, registry


def parse_plan(raw: object) -> BewerbungProviderPlan:
    """The only function allowed to turn a provider's raw, untrusted
    payload into a `BewerbungProviderPlan` (spec section 24/28) — a
    provider must never construct `BewerbungProviderPlan` itself and hand
    back a pre-validated instance (see
    `app.providers.bewerbung.base.BewerbungProvider.generate_plan`'s
    docstring for why). `extra="forbid"` on the model means an unexpected
    field (e.g. a smuggled `"free_text"`) fails here, before anything is
    rendered.
    """
    try:
        return BewerbungProviderPlan.model_validate(raw)
    except ValidationError as exc:
        raise BewerbungPlanRejectedError(["SCHEMA_INVALID"]) from exc


def resolve_plan(
    plan: BewerbungProviderPlan, registry: EvidenceRegistry
) -> list[list[EvidenceRecord]]:
    """Resolve every paragraph's claim ids against `registry` by exact
    lookup only (spec section 5) — an id not in the registry is rejected
    outright, never matched by name/label/fuzzy fallback. A claim id used
    in more than one paragraph is rejected too (deterministic, not
    silently deduplicated — spec section 5's "duplicate IDs: ... reject
    consistently").
    """
    seen: set[str] = set()
    resolved: list[list[EvidenceRecord]] = []
    for index, paragraph in enumerate(plan.paragraphs):
        records: list[EvidenceRecord] = []
        for claim_id in paragraph.claim_ids:
            if claim_id not in registry:
                raise BewerbungPlanRejectedError([f"UNKNOWN_CLAIM_ID:paragraph={index}"])
            if claim_id in seen:
                raise BewerbungPlanRejectedError([f"DUPLICATE_CLAIM_ID:paragraph={index}"])
            seen.add(claim_id)
            records.append(registry[claim_id])
        resolved.append(records)
    return resolved


# --- fixed, non-factual templates (spec section 15-19) ---------------------

_SALUTATION = "Sehr geehrte Damen und Herren,"

_OPENING_TEMPLATES: dict[str, str] = {
    "ROLE_INTEREST": (
        'mit großem Interesse habe ich Ihre Stellenanzeige für die Position "{title}" '
        "bei {company} gelesen."
    ),
    "MATCH_FOCUS": (
        'Die ausgeschriebene Position "{title}" bei {company} interessiert mich besonders, '
        "da sie gut zu meinen nachgewiesenen Kenntnissen passt."
    ),
}

_CLOSING_TEMPLATES: dict[str, str] = {
    "INTERVIEW_INTEREST": (
        "Über die Möglichkeit eines persönlichen Gesprächs würde ich mich sehr freuen."
    ),
    "SHORT_PROFESSIONAL": "Für Rückfragen stehe ich Ihnen gerne zur Verfügung.",
}

_GENERIC_PARAGRAPH_TEXT = (
    "Gerne möchte ich Ihnen meine bisherigen Erfahrungen in einem persönlichen Gespräch vorstellen."
)


def _experience_fragment(record: ExperienceEvidenceRecord) -> str:
    if record.technologies:
        technologies = ", ".join(record.technologies)
        return f"meine Erfahrung als {record.role} bei {record.company} (u. a. mit {technologies})"
    return f"meine Erfahrung als {record.role} bei {record.company}"


def _project_fragment(record: ProjectEvidenceRecord) -> str:
    return f"das Projekt {record.name}"


def _language_fragment(record: LanguageEvidenceRecord) -> str:
    # NATIVE is the only level that may render native/muttersprachlich
    # wording (spec section 9) — every other level (including UNKNOWN,
    # already excluded upstream) renders as an explicit CEFR statement.
    if record.level == "NATIVE":
        return f"{record.language} als Muttersprache"
    return f"{record.language}kenntnisse auf {record.level}-Niveau"


def _render_evidence_paragraph(
    records: list[EvidenceRecord], professional_title: str | None
) -> str:
    skills = [r for r in records if isinstance(r, SkillEvidenceRecord)]
    experiences = [r for r in records if isinstance(r, ExperienceEvidenceRecord)]
    projects = [r for r in records if isinstance(r, ProjectEvidenceRecord)]
    languages = [r for r in records if isinstance(r, LanguageEvidenceRecord)]

    sentences: list[str] = []
    if skills:
        names = ", ".join(skill.name for skill in skills)
        role = professional_title or "meiner bisherigen Tätigkeit"
        sentences.append(
            f"Im Rahmen von {role} bringe ich Kenntnisse in {names} mit, die für diese "
            "Position relevant sind."
        )
    for experience in experiences:
        sentences.append(f"Besonders relevant ist {_experience_fragment(experience)}.")
    for project in projects:
        sentences.append(f"Ebenfalls relevant ist {_project_fragment(project)}.")
    for language in languages:
        sentences.append(f"Zudem verfüge ich über {_language_fragment(language)}.")
    return " ".join(sentences)


def render_draft(
    plan: BewerbungProviderPlan,
    resolved_paragraphs: list[list[EvidenceRecord]],
    job_title: str,
    job_company: str,
    professional_title: str | None,
) -> RenderedBewerbung:
    """Build the entire final draft content from trusted inputs only:
    `plan`'s bounded enum/structure choices (already schema- and
    claim-id-validated by `parse_plan`/`resolve_plan`), `resolved_paragraphs`
    (evidence records looked up from the registry, never provider text),
    and `job_title`/`job_company`/`professional_title` (already-trusted
    fields from the persisted Job / pinned CV draft header). No parameter
    here can carry provider-authored prose.
    """
    subject = f"Bewerbung als {job_title}"
    opening = _OPENING_TEMPLATES[plan.opening_style].format(title=job_title, company=job_company)
    closing = _CLOSING_TEMPLATES[plan.closing_style]

    body_paragraphs: list[BewerbungParagraph] = []
    for paragraph, records in zip(plan.paragraphs, resolved_paragraphs, strict=True):
        if paragraph.kind == "GENERIC":
            text = _GENERIC_PARAGRAPH_TEXT
        else:
            text = _render_evidence_paragraph(records, professional_title)
        body_paragraphs.append(
            BewerbungParagraph(text=text, source_claim_ids=list(paragraph.claim_ids))
        )

    return RenderedBewerbung(
        subject=subject,
        salutation=_SALUTATION,
        opening=opening,
        body_paragraphs=body_paragraphs,
        closing=closing,
    )
