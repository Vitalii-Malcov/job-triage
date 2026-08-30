"""Company Research Agent v1's only provider: local, job-data-only, zero
network.

Codex's merge-gate review (see git history around this module) found that a
DNS-resolve-then-fetch SSRF check has an unavoidable TOCTOU/DNS-rebinding
window (the resolved-and-validated IP is not guaranteed to be the IP httpx
actually connects to on its own, separate resolution). Building a safe
pinned-IP TLS transport is real work with its own failure modes, and is
explicitly out of scope for this pass. Company Research v1 therefore makes
**zero outbound network requests of any kind** — see
tests/test_company_research_network_safety.py, which asserts this both by
static inspection (no httpx/requests/aiohttp/urllib import anywhere in this
module) and by running a full CompanyResearchService.get_or_run() with all
socket access blocked.

A second, related finding: promoting a single vacancy's own fields
(location, mentioned skills) to *company-level* facts (headquarters,
technologies) is itself a hallucination risk — a job's location is not
necessarily the company's headquarters, and skills mentioned in one posting
are not necessarily the company's whole tech stack. This provider therefore
never fills company-level fields (industry, headquarters, company_size,
products_or_services, technologies, hiring_signals, positive_signals,
risk_signals) — they stay None/[] with an explicit UNKNOWN evidence entry.
Vacancy-scoped observations are recorded instead as qualified
`relevant_facts` ("This vacancy is located in ...", not "Company
headquarters: ..."), each backed by FACT evidence sourced from the job
posting itself.

A future network-fetching provider (company website, search API, LLM
research) is a separate stage that requires a deliberately chosen safe
egress architecture — not something to bolt on here.
"""

import json

from app.db.models import JobRecord
from app.models.company_research import CompanyResearchData, Evidence
from app.providers.base import CompanyResearchProvider

PROVIDER_NAME = "job_data"

# Company-level fields this provider can never fill from a single vacancy
# without hallucinating — each gets an explicit UNKNOWN evidence entry
# instead of being silently absent. A future provider with genuine
# company-level sources (not just one job posting) can fill these in.
_ALWAYS_UNKNOWN_FIELDS: dict[str, str] = {
    "industry": "Industry",
    "headquarters": "Headquarters",
    "company_size": "Company size",
    "products_or_services": "Products or services",
    "technologies": "Technologies",
    "hiring_signals": "Hiring signals",
    "positive_signals": "Positive signals",
    "risk_signals": "Risk signals",
}

# Heuristic, deliberately conservative confidence calibration — about
# evidence *amount*, not source authority (same spirit as
# app.agents.data_confidence). A job-data-only provider can never be
# "complete company research", so the ceiling is well below 1.0.
_BASE_CONFIDENCE = 0.2
_EVIDENCE_WEIGHT = 0.08
_MAX_CONFIDENCE = 0.6


class JobDataCompanyResearchProvider(CompanyResearchProvider):
    """Builds vacancy-scoped FACT evidence from a JobRecord's own
    already-persisted data. Never makes a network call, never fails (no I/O
    to fail on), never returns COMPLETE — see module docstring for why.
    """

    name = PROVIDER_NAME

    async def research(
        self, job: JobRecord, *, domain_hint: str | None = None
    ) -> CompanyResearchData:
        # domain_hint is part of the CompanyResearchProvider interface for
        # a future network-fetching provider; this provider does no network
        # I/O at all and ignores it entirely.
        evidence: list[Evidence] = [
            Evidence(
                type="FACT",
                claim=f"Job posting company name: {job.company}",
                source_url=job.url,
                source_title=job.title,
            )
        ]

        relevant_facts: list[str] = []
        if job.location.strip():
            claim = f"This vacancy is located in {job.location}."
            relevant_facts.append(claim)
            evidence.append(
                Evidence(type="FACT", claim=claim, source_url=job.url, source_title=job.title)
            )

        skills = sorted(set(json.loads(job.skills_json or "[]")))
        if skills:
            claim = f"This vacancy mentions: {', '.join(skills)}."
            relevant_facts.append(claim)
            evidence.append(
                Evidence(type="FACT", claim=claim, source_url=job.url, source_title=job.title)
            )

        for label in _ALWAYS_UNKNOWN_FIELDS.values():
            evidence.append(
                Evidence(
                    type="UNKNOWN",
                    claim=f"{label}: could not be determined from available sources.",
                )
            )

        fact_count = sum(1 for item in evidence if item.type == "FACT")
        confidence = min(_BASE_CONFIDENCE + _EVIDENCE_WEIGHT * fact_count, _MAX_CONFIDENCE)

        return CompanyResearchData(
            company_name=job.company,
            company_domain=None,
            industry=None,
            headquarters=None,
            company_size=None,
            short_summary="",
            products_or_services=[],
            technologies=[],
            hiring_signals=[],
            relevant_facts=relevant_facts,
            positive_signals=[],
            risk_signals=[],
            source_urls=[job.url],
            evidence=evidence,
            confidence=round(confidence, 2),
            research_status="PARTIAL",
            provider_name=self.name,
        )
