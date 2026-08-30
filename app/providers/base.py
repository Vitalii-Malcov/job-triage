from abc import ABC, abstractmethod

from app.db.models import JobRecord
from app.models.company_research import CompanyResearchData


class ProviderError(Exception):
    """Base exception for company-research provider failures (network, SSRF
    rejection, timeout, malformed response).

    Concrete providers should subclass this so callers (app/services/
    company_research.py, app/api/routes.py) can catch a single type
    regardless of which underlying provider failed — mirrors
    app.collectors.base.CollectorError.
    """


class ProviderNotConfiguredError(ProviderError):
    """Raised when a provider requires configuration (e.g. a paid API key)
    that isn't set. The default JobDataCompanyResearchProvider never raises
    this — it has no required external configuration — but future providers
    (e.g. a search-API or LLM-backed one) can, and callers already know how
    to translate it (503, same as app.collectors.base.CollectorNotConfiguredError).
    """


class CompanyResearchProvider(ABC):
    """Interface every company-research data source implements.

    A provider's only responsibility is producing a CompanyResearchData for
    one job's company — it must never write to the database and must never
    decide caching/freshness (that's app/services/company_research.py's
    job), mirroring app.collectors.base.JobCollector's separation of
    fetching from persistence/scoring.

    Evidence-first contract: every non-empty field in the returned
    CompanyResearchData must be backed by at least one Evidence entry with
    type "FACT" (directly sourced) or "INFERENCE" (a stated logical
    deduction) — never invented. Fields with no evidence must be left
    None/[] rather than guessed.
    """

    #: Copied into CompanyResearchData.provider_name so persisted/returned
    #: results are traceable to the provider that produced them.
    name: str

    @abstractmethod
    async def research(
        self, job: JobRecord, *, domain_hint: str | None = None
    ) -> CompanyResearchData:
        """Research the company behind `job`.

        `domain_hint` is reserved for a future network-fetching provider —
        Company Research v1's only provider (JobDataCompanyResearchProvider)
        makes zero outbound network requests and ignores this parameter
        entirely (see that module's docstring for why a website-fetch
        provider was deliberately removed rather than hardened). The
        service layer (app/services/company_research.py) currently always
        passes None; `Job.url` must never be treated as the company's own
        website domain.
        """
        raise NotImplementedError
