"""Bewerbung / Anschreiben (cover letter) draft generation DTOs (Stage 6D).

**Builds on 6C, not 6A directly (spec section 2).** Candidate facts come
only from the pinned `TailoredCVDraft` (Stage 6C) — already evidence-filtered
against Stage 6A's trust rule (see
`app.models.candidate_profile.is_usable_for_generation`). This module and
`app.agents.bewerbung_generator`/`app.agents.bewerbung_renderer` never
re-derive candidate facts from CandidateProfile directly; they only read
what 6C already decided was safe to show.

**The provider selects STRUCTURE, never PROSE (blocker fix).** An earlier
version of this module let a provider return arbitrary opening/body/closing
text plus a self-reported `used_claim_ids` list that the validator merely
cross-checked with regexes — this let a misbehaving provider render
unsupported facts (e.g. "Ich verfüge über AWS-Erfahrung.") that were never
in `allowed_claims` at all, entirely bypassing the claim allowlist. The
fixed contract: a provider returns only a `BewerbungProviderPlan` — a
bounded choice of `opening_style`/`closing_style` enums plus, per paragraph,
which `allowed_claims` ids to reference. `app.agents.bewerbung_renderer`
resolves every claim id against the evidence registry and renders the
*actual* factual sentences from trusted, record-specific templates — the
provider's own text never reaches `subject`/`salutation`/`opening`/
`body_paragraphs`/`closing`/`signature_name` at all. See that module's
docstring for the full rendering contract.

**Immutable, one-row-per-generation (spec section 35, unlike 6B/6C).**
Every successful generation call creates a NEW `BewerbungDraft` row, even
if the inputs are identical to a previous call — a provider's plan choice
can legitimately vary between calls, and callers regenerate intentionally.
There is no cache-identity / reuse-on-POST here, unlike
`CandidateJobMatchRecord`/`CandidateCVDraftRecord`'s
UNIQUE-constraint-based caching.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

BewerbungDraftStatus = Literal["DRAFT"]
BewerbungLanguage = Literal["de"]

AllowedClaimSourceEntity = Literal[
    "candidate_skill",
    "candidate_project",
    "candidate_experience",
    "candidate_language",
]

# Bounded, non-factual choices a provider may pick between — mapped to
# fixed German sentences by app.agents.bewerbung_renderer. A provider
# cannot supply its own opening/closing text (spec section 15/18).
OpeningStyle = Literal["ROLE_INTEREST", "MATCH_FOCUS"]
ClosingStyle = Literal["INTERVIEW_INTEREST", "SHORT_PROFESSIONAL"]

# "EVIDENCE" paragraphs reference one or more allowed_claims ids, rendered
# via record-specific templates. "GENERIC" paragraphs carry no claim ids at
# all and render a fixed, non-factual connective sentence — the only legal
# choice when there is no candidate evidence to reference at all (spec
# section 55's sparse-profile case).
ParagraphKind = Literal["EVIDENCE", "GENERIC"]

_MAX_PARAGRAPHS = 4
_MAX_CLAIM_IDS_PER_PARAGRAPH = 4
_MAX_TOTAL_CLAIM_IDS = 10
_MAX_CLAIM_ID_LENGTH = 100

_ClaimId = Annotated[str, Field(min_length=1, max_length=_MAX_CLAIM_ID_LENGTH)]


class AllowedClaim(BaseModel):
    """One factual candidate claim a provider is allowed to *reference by
    id* (spec section 5/12) — traced back to exactly one item of the pinned
    6C CV draft (never the raw Candidate Profile — see module docstring).
    `claim` is a human-readable label for the provider's own reasoning
    about which ids to pick; the provider's plan never echoes `claim` text
    back, and no code path ever copies `claim` into a persisted draft
    field verbatim — the renderer re-derives the actual sentence from its
    own structured evidence registry (see
    `app.agents.bewerbung_renderer.EvidenceRecord`), keyed by the same
    `id`, independently of this label.
    """

    id: str
    claim: str
    source_entity: AllowedClaimSourceEntity
    source_id: int


class BewerbungEvidenceCandidate(BaseModel):
    """The candidate-fact side of the evidence packet sent to a provider
    (spec section 10), limited to header-style fields not already carried
    as a structured `AllowedClaim` — built entirely from the pinned 6C CV
    draft, never from the live Candidate Profile (section 2). Advisory
    context only: no template ever inserts `summary` verbatim into
    rendered prose.
    """

    professional_title: str | None = None
    summary: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class BewerbungEvidenceJob(BaseModel):
    """The job side of the evidence packet. `description` is untrusted
    external text (spec section 43) — it is carried as a plain data field,
    never concatenated into a provider's system/instruction text, and
    never read by the trusted renderer either (only `title`/`company`,
    already-trusted job identity fields, ever reach rendered prose).
    """

    title: str
    company: str
    description: str
    matched_requirements: list[str] = Field(default_factory=list)
    partial_requirements: list[str] = Field(default_factory=list)


class BewerbungEvidencePacket(BaseModel):
    """The complete, minimal input sent to a `BewerbungProvider` (spec
    section 10-13). `allowed_claims` is the exhaustive claim allowlist a
    plan's paragraphs may reference by id; `forbidden_claims` is
    human-readable negative guidance built from MISSING/UNKNOWN
    requirements — advisory to the provider only. The actual safety
    boundary is structural: a plan can only reference `allowed_claims`
    ids, and the renderer resolves those ids against its own registry
    independently of anything the provider says about them.
    """

    candidate: BewerbungEvidenceCandidate
    job: BewerbungEvidenceJob
    allowed_claims: list[AllowedClaim] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)


class PlanParagraph(BaseModel):
    """One paragraph's worth of *structure*, never text (spec section 3/4).
    `extra="forbid"` (spec section 24): a provider payload carrying an
    unexpected field (e.g. `"free_text": "..."`) fails schema validation
    before anything is rendered, rather than being silently ignored.
    """

    model_config = ConfigDict(extra="forbid")

    kind: ParagraphKind
    claim_ids: list[_ClaimId] = Field(default_factory=list, max_length=_MAX_CLAIM_IDS_PER_PARAGRAPH)

    @model_validator(mode="after")
    def _validate_claim_ids_match_kind(self) -> "PlanParagraph":
        if self.kind == "EVIDENCE" and not self.claim_ids:
            raise ValueError("An EVIDENCE paragraph must reference at least one claim id.")
        if self.kind == "GENERIC" and self.claim_ids:
            raise ValueError("A GENERIC paragraph must not reference any claim id.")
        return self


class BewerbungProviderPlan(BaseModel):
    """The entire structured contract a provider may return (spec section
    3). No field here can ever hold arbitrary candidate/company prose —
    every field is either a bounded enum or a list of ids resolved,
    post-validation, against `BewerbungEvidencePacket.allowed_claims` by
    `app.agents.bewerbung_renderer.resolve_plan`. `extra="forbid"` closes
    the schema against any additional attacker-controlled field (spec
    section 24).
    """

    model_config = ConfigDict(extra="forbid")

    opening_style: OpeningStyle
    paragraphs: list[PlanParagraph] = Field(min_length=1, max_length=_MAX_PARAGRAPHS)
    closing_style: ClosingStyle

    @model_validator(mode="after")
    def _validate_total_claim_ids(self) -> "BewerbungProviderPlan":
        total = sum(len(paragraph.claim_ids) for paragraph in self.paragraphs)
        if total > _MAX_TOTAL_CLAIM_IDS:
            raise ValueError(f"Plan references too many claim ids (max {_MAX_TOTAL_CLAIM_IDS}).")
        return self


class BewerbungParagraph(BaseModel):
    """One rendered paragraph, persisted with the exact evidence ids that
    produced it (spec section 27) — answers "which facts produced this
    paragraph?" mechanically, without re-deriving it from the plan.
    """

    text: str
    source_claim_ids: list[str] = Field(default_factory=list)


class BewerbungDraftData(BaseModel):
    """The computed content of a Bewerbung draft — everything
    `app.services.bewerbung.BewerbungService.generate` produces, before
    persistence assigns an id/created_at. Mirrors
    `TailoredCVDraftData -> TailoredCVDraft` (Stage 6C).

    `subject`/`salutation`/`opening`/`body_paragraphs`/`closing`/
    `signature_name` are all produced exclusively by the trusted renderer
    (`app.agents.bewerbung_renderer.render_draft`) from `plan` +
    trusted CV draft/job fields — never copied from anything a provider
    returned as free text (spec section 28's central acceptance criterion).
    `plan` is persisted for traceability (spec section 26): it is already
    schema-bounded and contains zero free text, so it is safe to keep as-is
    rather than discarding it after use.
    """

    job_id: int
    cv_draft_id: int
    match_id: int
    # Snapshot pins (section 4), copied verbatim from the pinned 6C draft —
    # never recomputed independently, exactly like 6C copies its own pins
    # from its pinned match.
    candidate_profile_version: int
    job_snapshot_fingerprint: str
    match_algorithm_version: str
    cv_adapter_version: str
    bewerbung_generator_version: str
    provider: str
    status: BewerbungDraftStatus = "DRAFT"

    language: BewerbungLanguage = "de"
    subject: str
    salutation: str
    opening: str
    body_paragraphs: list[BewerbungParagraph] = Field(default_factory=list)
    closing: str
    signature_name: str | None = None
    plan: BewerbungProviderPlan

    claims: list[AllowedClaim] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class BewerbungDraft(BewerbungDraftData):
    """GET/POST /api/v1/jobs/{job_id}/bewerbung-draft and GET
    /api/v1/bewerbung-drafts/{draft_id} response shape. Immutable once
    created — see app/db/bewerbung_repository.py.
    """

    id: int
    created_at: datetime


class BewerbungDraftRequest(BaseModel):
    """POST /api/v1/jobs/{job_id}/bewerbung-draft body. `cv_draft_id` is
    required and pins to one specific, immutable, already-persisted 6C
    draft — never an implicit "latest CV draft" (spec section 3).
    """

    cv_draft_id: int
