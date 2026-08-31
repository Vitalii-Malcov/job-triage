"""Bewerbung draft generation orchestration (Stage 6D): pin/staleness
verification, evidence-packet construction, provider plan invocation,
trusted rendering, and persistence.

**No provider-authored prose reaches persistence (blocker fix).** The
provider's raw plan payload is parsed/validated
(`app.agents.bewerbung_renderer.parse_plan`), its claim ids are resolved
against the evidence registry (`resolve_plan`), and only then is the
actual letter text produced (`render_draft`) — entirely from trusted
templates and trusted CV draft/job fields. See
`app.agents.bewerbung_renderer`'s module docstring for the full
architecture this enforces.

Every successful call creates a NEW immutable `BewerbungDraftRecord` (spec
section 35) — unlike `CompanyResearchService`/Stage 6B/6C, there is no
cache-identity reuse here: a provider's plan can legitimately vary between
calls with identical pinned inputs, and regeneration is intentional, so a
repeated POST is never short-circuited into returning a prior row.

Mirrors `app.services.company_research.CompanyResearchService`'s shape
(constructor-injectable provider, `None` means "build the default"), the
only other async-provider-calling service in this project.
"""

import logging

from sqlalchemy.orm import Session

from app.agents.bewerbung_generator import (
    BEWERBUNG_GENERATOR_VERSION,
    BewerbungCVDraftJobMismatchError,
    BewerbungCVDraftNotFoundError,
    BewerbungJobChangedError,
    BewerbungMatchInconsistentError,
    BewerbungMatchNotFoundError,
    BewerbungProfileChangedError,
)
from app.agents.bewerbung_renderer import (
    BewerbungPlanRejectedError,
    build_evidence,
    parse_plan,
    render_draft,
    resolve_plan,
)
from app.db.bewerbung_repository import create_bewerbung_draft, to_bewerbung_draft
from app.db.candidate_cv_draft_repository import get_draft_by_id, to_tailored_cv_draft
from app.db.candidate_job_match_repository import (
    compute_job_snapshot_fingerprint,
    get_match_by_id,
    to_candidate_job_match,
)
from app.db.candidate_profile_repository import get_or_create_candidate_profile
from app.db.models import JobRecord
from app.models.bewerbung import BewerbungDraft, BewerbungDraftData
from app.providers.bewerbung.base import BewerbungProvider
from app.providers.bewerbung.deterministic import DeterministicBewerbungProvider

logger = logging.getLogger(__name__)


class BewerbungService:
    def __init__(self, provider: BewerbungProvider | None = None) -> None:
        # None means "build the default provider" at call time, not at
        # construction — mirrors CompanyResearchService exactly; tests
        # inject a fake/deterministic provider directly via this
        # constructor instead.
        self._injected_provider = provider

    def _provider_for(self) -> BewerbungProvider:
        return self._injected_provider or DeterministicBewerbungProvider()

    async def generate(self, db: Session, job: JobRecord, cv_draft_id: int) -> BewerbungDraft:
        cv_draft_record = get_draft_by_id(db, cv_draft_id)
        if cv_draft_record is None:
            raise BewerbungCVDraftNotFoundError(cv_draft_id)
        if cv_draft_record.job_id != job.id:
            raise BewerbungCVDraftJobMismatchError(cv_draft_id=cv_draft_id, job_id=job.id)

        profile_record = get_or_create_candidate_profile(db)
        if profile_record.profile_version != cv_draft_record.candidate_profile_version:
            raise BewerbungProfileChangedError(
                cv_draft_profile_version=cv_draft_record.candidate_profile_version,
                current_profile_version=profile_record.profile_version,
            )

        current_fingerprint = compute_job_snapshot_fingerprint(job)
        if current_fingerprint != cv_draft_record.job_snapshot_fingerprint:
            raise BewerbungJobChangedError()

        match_record = get_match_by_id(db, cv_draft_record.match_id)
        if match_record is None:
            raise BewerbungMatchNotFoundError(cv_draft_record.match_id)
        # Spec section 46 (hardened defensively, low cost): the pinned CV
        # draft's own traceability copies must still agree with the match
        # record they were copied from. Should be unreachable — nothing
        # mutates a persisted match/CV draft — but checked before ever
        # invoking a provider.
        if (
            match_record.job_id != cv_draft_record.job_id
            or match_record.candidate_profile_version != cv_draft_record.candidate_profile_version
            or match_record.job_snapshot_fingerprint != cv_draft_record.job_snapshot_fingerprint
            or match_record.algorithm_version != cv_draft_record.match_algorithm_version
        ):
            raise BewerbungMatchInconsistentError(cv_draft_record.match_id)

        cv_draft = to_tailored_cv_draft(cv_draft_record)
        match = to_candidate_job_match(match_record)

        evidence, registry = build_evidence(
            cv_draft, match, job.title, job.company, job.description
        )

        provider = self._provider_for()
        raw_plan = await provider.generate_plan(evidence)

        try:
            plan = parse_plan(raw_plan)
            resolved_paragraphs = resolve_plan(plan, registry)
        except BewerbungPlanRejectedError as exc:
            # Privacy-safe (spec section 47): fixed violation codes only,
            # never the provider's raw payload.
            logger.warning(
                "bewerbung_plan_rejected job_id=%s cv_draft_id=%s provider=%s codes=%s",
                job.id,
                cv_draft_id,
                provider.name,
                exc.codes,
            )
            raise

        professional_title = (
            cv_draft.header.professional_title.value if cv_draft.header.professional_title else None
        )
        rendered = render_draft(
            plan, resolved_paragraphs, job.title, job.company, professional_title
        )

        signature_name = None
        if cv_draft.header.first_name or cv_draft.header.last_name:
            signature_name = " ".join(
                part.value
                for part in (cv_draft.header.first_name, cv_draft.header.last_name)
                if part
            )

        warnings: list[str] = []
        if signature_name is None:
            warnings.append("NO_TRUSTED_NAME")

        used_claim_ids = {
            claim_id for paragraph in plan.paragraphs for claim_id in paragraph.claim_ids
        }
        allowed_by_id = {claim.id: claim for claim in evidence.allowed_claims}
        used_claims = [allowed_by_id[claim_id] for claim_id in used_claim_ids]

        data = BewerbungDraftData(
            job_id=job.id,
            cv_draft_id=cv_draft_id,
            match_id=cv_draft_record.match_id,
            candidate_profile_version=cv_draft_record.candidate_profile_version,
            job_snapshot_fingerprint=cv_draft_record.job_snapshot_fingerprint,
            match_algorithm_version=cv_draft_record.match_algorithm_version,
            cv_adapter_version=cv_draft_record.cv_adapter_version,
            bewerbung_generator_version=BEWERBUNG_GENERATOR_VERSION,
            provider=provider.name,
            subject=rendered.subject,
            salutation=rendered.salutation,
            opening=rendered.opening,
            body_paragraphs=rendered.body_paragraphs,
            closing=rendered.closing,
            signature_name=signature_name,
            plan=plan,
            claims=used_claims,
            warnings=warnings,
        )

        record = create_bewerbung_draft(db, data)
        # Privacy-safe (spec section 42): technical metadata only, never
        # candidate name/summary/skills/experience/subject/body content.
        logger.info(
            "bewerbung_draft_created job_id=%s cv_draft_id=%s bewerbung_draft_id=%s "
            "provider=%s generator_version=%s status=%s",
            job.id,
            cv_draft_id,
            record.id,
            provider.name,
            BEWERBUNG_GENERATOR_VERSION,
            record.status,
        )
        return to_bewerbung_draft(record)
