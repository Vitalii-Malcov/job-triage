"""Company Research orchestration: identity resolution, cache/TTL, provider
invocation, and failure-isolated persistence.

Shared by POST /jobs/{id}/research and the Telegram /research command via
app.api.routes._run_company_research — the same "one orchestration function,
reused by both surfaces" pattern as _run_bundesagentur/_run_xing in that
module. The provider itself never touches the database or decides caching;
this class owns that separation (mirrors JobCollector vs. the routes.py
orchestration functions for job collectors).

**Job.url is never treated as the company's own website domain.** A job
posting's URL is the URL of the *posting* (often on a job board or ATS —
Lever, Greenhouse, the source's own site, ...), not the hiring company's
site. An earlier version of this module derived a "domain hint" from
Job.url and blacklisted a handful of known job-board hostnames; Codex's
review correctly flagged this as an unreliable identity signal (two
different companies both posting via the same ATS would otherwise risk
being merged, or a domain wrongly attributed) and it has been removed
entirely rather than patched with a longer blacklist. Company Research v1
therefore only ever has a `normalized_company_name` identity — domain-based
identity (`app.db.repositories.normalize_domain` /
`get_company_research_by_identity`'s domain-preferred lookup) remains
supported at the persistence layer for a future provider with a genuine,
trusted domain source, but nothing in this module produces one today.
"""

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import CompanyResearchRecord, JobRecord
from app.db.repositories import (
    AmbiguousCompanyIdentityError,
    CompanyResearchWriteOutcome,
    is_usable_company_research,
    normalize_company_name,
    record_failed_attempt,
    resolve_name_only_company_research,
    upsert_company_research,
)
from app.models.company_research import (
    CompanyResearchResponse,
    CompanyResearchRunResponse,
    Evidence,
)
from app.providers.base import CompanyResearchProvider, ProviderNotConfiguredError
from app.providers.job_data_provider import JobDataCompanyResearchProvider

logger = logging.getLogger(__name__)

# AmbiguousCompanyIdentityError is defined in app.db.repositories (it's
# raised directly by resolve_name_only_company_research, and
# record_failed_attempt needs to be able to hit the same guard — see that
# class's docstring) but re-exported here: app/api/routes.py and
# app/services/telegram_bot.py both import it from this module, alongside
# InvalidCompanyIdentityError, as "the identity errors CompanyResearchService
# can raise" — keeping both importable from one place.
__all__ = [
    "AmbiguousCompanyIdentityError",
    "CompanyResearchService",
    "InvalidCompanyIdentityError",
    "to_company_research_response",
]


class InvalidCompanyIdentityError(Exception):
    """Raised when a job has no usable company name to research (blank, or
    blank after Unicode normalization — see
    app.db.repositories.normalize_company_name).

    A blank identity must never silently fall into a shared "" bucket that
    would merge every such job's research into one record — this is a
    controlled, typed failure instead. app/api/routes.py maps it to 422.
    """


def to_company_research_response(record: CompanyResearchRecord) -> CompanyResearchResponse:
    """Convert a persisted CompanyResearchRecord into its typed API shape.

    Used both by CompanyResearchService (after a run) and directly by
    GET /jobs/{id}/research (a pure cache read with no provider/network
    call) — see app/api/routes.py.
    """
    return CompanyResearchResponse(
        id=record.id,
        company_name=record.company_name,
        company_domain=record.company_domain,
        industry=record.industry,
        headquarters=record.headquarters,
        company_size=record.company_size,
        short_summary=record.short_summary,
        products_or_services=json.loads(record.products_or_services_json),
        technologies=json.loads(record.technologies_json),
        hiring_signals=json.loads(record.hiring_signals_json),
        relevant_facts=json.loads(record.relevant_facts_json),
        positive_signals=json.loads(record.positive_signals_json),
        risk_signals=json.loads(record.risk_signals_json),
        source_urls=json.loads(record.source_urls_json),
        evidence=[Evidence(**item) for item in json.loads(record.evidence_json)],
        confidence=record.confidence,
        research_status=record.research_status,
        provider_name=record.provider_name,
        researched_at=record.researched_at,
        last_attempt_at=record.last_attempt_at,
        last_attempt_status=record.last_attempt_status,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class CompanyResearchService:
    def __init__(self, provider: CompanyResearchProvider | None = None) -> None:
        # None means "build the default provider" (JobDataCompanyResearchProvider
        # takes no settings-derived configuration in v1 — it has no network
        # path to configure) rather than at construction — tests inject a
        # fake/mock provider directly via this constructor instead.
        self._injected_provider = provider

    def _provider_for(self) -> CompanyResearchProvider:
        return self._injected_provider or JobDataCompanyResearchProvider()

    def _is_fresh(self, record: CompanyResearchRecord, settings: Settings) -> bool:
        if record.research_status != "PARTIAL":
            # A FAILED (or still-PENDING) record is never treated as fresh,
            # so the very next call always retries automatically regardless
            # of TTL — self-healing after a transient failure.
            return False
        if record.researched_at is None:
            return False
        researched_at = record.researched_at
        if researched_at.tzinfo is None:
            # SQLite (unlike Postgres) doesn't preserve tzinfo through a
            # DateTime(timezone=True) round-trip — a value stored as UTC
            # comes back naive. Every value this project ever writes to
            # researched_at is UTC (see upsert_company_research), so a
            # naive read is always safe to reattach as UTC rather than a
            # sign of a genuinely ambiguous timestamp.
            researched_at = researched_at.replace(tzinfo=UTC)
        age = datetime.now(UTC) - researched_at
        return age < timedelta(hours=settings.company_research_ttl_hours)

    def _is_usable_research(self, record: CompanyResearchRecord) -> bool:
        """True if `record` holds genuine, previously-successful research
        content that's safe to serve as "stale but usable" after a failed
        refresh (RR-M-02). Delegates to the repository-layer
        is_usable_company_research so this project has exactly one
        definition of "usable" shared by both the service (this method) and
        the repository (FR-M-02's collision-resolution helpers).

        A record can exist purely as a FAILED/PENDING diagnostic row (e.g.
        record_failed_attempt's minimal row when no prior research ever
        succeeded) — that row is real and persisted, but it is not usable
        content. Without this check, a *second* consecutive failure would
        incorrectly treat the FAILED row left by the *first* failure as
        "prior good research" and report served_stale=True / an eventual
        HTTP 200, instead of the 502 a repeated total failure must keep
        returning.
        """
        return is_usable_company_research(record)

    def get_cached(self, db: Session, job: JobRecord) -> CompanyResearchResponse | None:
        """Pure cache read: resolve identity and return the stored record if
        one exists, regardless of freshness. Never calls the provider or
        touches the network — used by GET /jobs/{id}/research, which must
        not trigger an expensive/external call (spec requirement).

        Uses resolve_name_only_company_research (FR-M-03) — the same
        routine get_or_run uses — so GET and POST can never disagree about
        which record a given company name resolves to. Raises
        AmbiguousCompanyIdentityError (FR-M-01) rather than arbitrarily
        returning one of several known-domain companies sharing this job's
        normalized company name. app/api/routes.py maps it to 409.
        """
        normalized_company_name = normalize_company_name(job.company)
        if not normalized_company_name:
            return None
        record = resolve_name_only_company_research(db, normalized_company_name)
        if record is None:
            return None
        return to_company_research_response(record)

    async def get_or_run(
        self,
        db: Session,
        job: JobRecord,
        settings: Settings,
        *,
        force_refresh: bool = False,
    ) -> CompanyResearchRunResponse:
        normalized_company_name = normalize_company_name(job.company)
        if not normalized_company_name:
            raise InvalidCompanyIdentityError(
                f"Job {job.id} has no usable company name to research."
            )

        # v1 has no trusted domain source — see module docstring (H-02).
        domain_hint = None

        # FR-M-03: resolve_name_only_company_research is the single
        # identity-resolution routine for a name-only request (also used by
        # get_cached) — it transparently resolves to the sole known-domain
        # record when exactly one exists (previously a raw exact "name:<x>"
        # lookup would miss it entirely, causing a wasted provider call and
        # a spurious "superseded" result below) and still raises
        # AmbiguousCompanyIdentityError (FR-M-01) for 2+ known domains.
        existing = resolve_name_only_company_research(db, normalized_company_name)
        if existing is not None and not force_refresh and self._is_fresh(existing, settings):
            return CompanyResearchRunResponse(
                research=to_company_research_response(existing),
                refresh_attempted=False,
                refresh_succeeded=True,
                refresh_superseded=False,
                served_stale=False,
                error=None,
            )

        # FR-M-03: an already-resolved record's own identity must be
        # retained across a refresh — never silently downgrade a sole
        # known-domain record (e.g. "domain:acme.de") back to a domain-less
        # "name:<x>" identity merely because v1's own domain_hint is always
        # None. A brand-new identity (existing is None) still has no domain
        # to persist under, per H-02 — Job.url is never a domain source.
        effective_domain = existing.normalized_domain if existing is not None else domain_hint

        provider = self._provider_for()
        expected_version = existing.version if existing is not None else None
        try:
            data = await provider.research(job, domain_hint=domain_hint)
        except ProviderNotConfiguredError:
            # Not configured is a deliberate, non-retryable signal (e.g. a
            # future provider that needs an unset API key) — propagate it
            # instead of swallowing it into a FAILED record below, so the
            # caller (app/api/routes.py) can surface it distinctly as 503,
            # mirroring CollectorNotConfiguredError's treatment for job
            # collectors.
            raise
        except Exception as exc:
            # All other provider failures are isolated from persistence: a
            # bad refresh must not destroy previously-good research
            # (README/spec requirement) — only attempt metadata is updated,
            # never the research content itself. Logged here (not re-raised)
            # since this is a handled, expected failure mode for an external
            # data source.
            logger.warning(
                "company_research_provider_failed provider=%s company=%s",
                provider.name,
                job.company,
                exc_info=True,
            )
            error_message = str(exc) or exc.__class__.__name__
            record = record_failed_attempt(
                db,
                normalized_domain=effective_domain,
                normalized_company_name=normalized_company_name,
                company_name=job.company,
                provider_name=getattr(provider, "name", "unknown"),
                error_message=error_message,
            )
            # RR-M-02: `existing` may itself be nothing more than a
            # FAILED/PENDING diagnostic row from an earlier failed attempt
            # (record_failed_attempt's minimal row) — that is not usable
            # prior research, and must not be reported as served_stale=True
            # / surfaced as an HTTP 200. Only genuine previously-successful
            # content (_is_usable_research) counts as "stale but usable".
            if existing is not None and self._is_usable_research(existing):
                return CompanyResearchRunResponse(
                    research=to_company_research_response(record),
                    refresh_attempted=True,
                    refresh_succeeded=False,
                    refresh_superseded=False,
                    served_stale=True,
                    error=record.last_error,
                )
            return CompanyResearchRunResponse(
                research=None,
                refresh_attempted=True,
                refresh_succeeded=False,
                refresh_superseded=False,
                served_stale=False,
                error=record.last_error,
            )

        record, outcome = upsert_company_research(
            db,
            data,
            normalized_domain=effective_domain,
            normalized_company_name=normalized_company_name,
            expected_version=expected_version,
        )
        if outcome == CompanyResearchWriteOutcome.SUPERSEDED:
            # RR-M-03: the provider call above succeeded, but a concurrent
            # refresh already committed a newer result first — our own
            # result was discarded, never persisted. This is neither a
            # provider failure nor stale cache: `record` is genuinely
            # fresher than what we produced, just not *our* data.
            return CompanyResearchRunResponse(
                research=to_company_research_response(record),
                refresh_attempted=True,
                refresh_succeeded=False,
                refresh_superseded=True,
                served_stale=False,
                error="Refresh result was superseded by a newer concurrent refresh.",
            )
        return CompanyResearchRunResponse(
            research=to_company_research_response(record),
            refresh_attempted=True,
            refresh_succeeded=True,
            refresh_superseded=False,
            served_stale=False,
            error=None,
        )
