import inspect
from datetime import UTC, datetime

import pytest

import app.providers.job_data_provider as job_data_provider_module
from app.db.models import JobRecord
from app.providers.job_data_provider import JobDataCompanyResearchProvider


def _job(**overrides) -> JobRecord:
    now = datetime.now(UTC)
    fields = {
        "id": 1,
        "fingerprint": "fp-1",
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "Acme GmbH",
        "location": "Berlin",
        "url": "https://jobs.example/1",
        "description": "We use Python and Docker.",
        "skills_json": '["python", "docker"]',
        "data_confidence": 0.5,
        "skill_source": None,
        "must_have_skills_json": "[]",
        "nice_to_have_skills_json": "[]",
        "score": 80,
        "recommendation": "APPLY",
        "status": "NEW",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    fields.update(overrides)
    return JobRecord(**fields)


def test_module_never_imports_an_http_client():
    """Company Research v1 makes zero outbound network requests — see the
    module docstring for why a website-fetch provider was removed rather
    than hardened (unavoidable DNS-rebinding window in a
    resolve-then-fetch SSRF check). This provider must not have the means
    to make an HTTP request at all.
    """
    source = inspect.getsource(job_data_provider_module)
    forbidden_imports = ["httpx", "requests", "aiohttp", "urllib.request", "http.client"]
    for name in forbidden_imports:
        assert f"import {name}" not in source, f"module must not import {name}"

    for name in ("httpx", "requests", "aiohttp"):
        assert not hasattr(job_data_provider_module, name)


@pytest.mark.asyncio
async def test_research_status_is_always_partial():
    provider = JobDataCompanyResearchProvider()
    data = await provider.research(_job())
    assert data.research_status == "PARTIAL"


@pytest.mark.asyncio
async def test_ignores_domain_hint_entirely():
    """domain_hint is reserved for a future network-fetching provider; this
    provider must produce identical output regardless of what's passed.
    """
    provider = JobDataCompanyResearchProvider()
    without_hint = await provider.research(_job(), domain_hint=None)
    with_hint = await provider.research(_job(), domain_hint="evil.example")
    assert without_hint.company_domain is None
    assert with_hint.company_domain is None
    assert without_hint.model_dump(exclude={"evidence"}) == with_hint.model_dump(
        exclude={"evidence"}
    )


@pytest.mark.asyncio
async def test_frankfurt_vacancy_does_not_become_company_headquarters():
    provider = JobDataCompanyResearchProvider()
    data = await provider.research(_job(location="Frankfurt"))

    assert data.headquarters is None
    assert any("Frankfurt" in fact for fact in data.relevant_facts)
    fact_claims = [e.claim for e in data.evidence if e.type == "FACT"]
    assert any("This vacancy is located in Frankfurt" in claim for claim in fact_claims)
    assert not any("headquarters" in claim.lower() for claim in fact_claims)


@pytest.mark.asyncio
async def test_python_vacancy_does_not_become_company_technologies():
    provider = JobDataCompanyResearchProvider()
    data = await provider.research(_job(skills_json='["python"]'))

    assert data.technologies == []
    assert any("python" in fact.lower() for fact in data.relevant_facts)
    fact_claims = [e.claim for e in data.evidence if e.type == "FACT"]
    assert any("this vacancy mentions" in claim.lower() for claim in fact_claims)
    assert not any("technology stack" in claim.lower() for claim in fact_claims)


@pytest.mark.asyncio
async def test_no_hallucination_company_level_fields_stay_empty_with_unknown_evidence():
    provider = JobDataCompanyResearchProvider()
    data = await provider.research(_job(location="", skills_json="[]"))

    assert data.company_domain is None
    assert data.industry is None
    assert data.company_size is None
    assert data.headquarters is None
    assert data.products_or_services == []
    assert data.technologies == []
    assert data.hiring_signals == []
    assert data.positive_signals == []
    assert data.risk_signals == []

    unknown_claims = [e.claim for e in data.evidence if e.type == "UNKNOWN"]
    assert any("Industry" in claim for claim in unknown_claims)
    assert any("Headquarters" in claim for claim in unknown_claims)
    assert any("Technologies" in claim for claim in unknown_claims)
    assert any("Company size" in claim for claim in unknown_claims)


@pytest.mark.asyncio
async def test_evidence_source_is_the_job_posting():
    provider = JobDataCompanyResearchProvider()
    data = await provider.research(_job(url="https://jobs.example/42"))

    fact_evidence = [e for e in data.evidence if e.type == "FACT"]
    assert fact_evidence
    for item in fact_evidence:
        assert item.source_url == "https://jobs.example/42"
