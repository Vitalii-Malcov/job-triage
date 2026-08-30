import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import CompanyResearchRecord, JobRecord
from app.db.repositories import (
    get_company_research_by_identity,
    normalize_company_name,
    upsert_company_research,
)
from app.models.company_research import CompanyResearchData, Evidence
from app.providers.base import CompanyResearchProvider, ProviderNotConfiguredError
from app.services.company_research import (
    AmbiguousCompanyIdentityError,
    CompanyResearchService,
    InvalidCompanyIdentityError,
)


class _FakeProvider(CompanyResearchProvider):
    name = "fake"

    def __init__(self, result: CompanyResearchData | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.call_count = 0

    async def research(self, job, *, domain_hint=None):
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.result


def _data(**overrides) -> CompanyResearchData:
    fields = {
        "company_name": "Acme GmbH",
        "provider_name": "fake",
        "research_status": "PARTIAL",
        "confidence": 0.4,
        "evidence": [Evidence(type="FACT", claim="test", source_url="https://acme.example/")],
    }
    fields.update(overrides)
    return CompanyResearchData(**fields)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _settings(**overrides) -> Settings:
    fields = {"company_research_ttl_hours": 720}
    fields.update(overrides)
    return Settings(**fields)


def _seed_job(db: Session, **overrides) -> JobRecord:
    now = datetime.now(UTC)
    fields = {
        "fingerprint": "fp-1",
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "Acme GmbH",
        "location": "Berlin",
        "url": "https://careers.acme.com/postings/1",
        "description": "",
        "skills_json": "[]",
        "data_confidence": 0.5,
        "must_have_skills_json": "[]",
        "nice_to_have_skills_json": "[]",
        "score": 80,
        "recommendation": "APPLY",
        "status": "NEW",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    fields.update(overrides)
    record = JobRecord(**fields)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# --- cache/TTL/force_refresh -------------------------------------------------


@pytest.mark.asyncio
async def test_cache_hit_reports_no_refresh_attempted():
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)
    settings = _settings()

    first = await service.get_or_run(db, job, settings)
    second = await service.get_or_run(db, job, settings)

    assert provider.call_count == 1
    assert first.refresh_attempted is True
    assert first.refresh_succeeded is True
    assert first.served_stale is False
    assert second.refresh_attempted is False
    assert second.refresh_succeeded is True
    assert second.served_stale is False
    assert second.research.id == first.research.id


@pytest.mark.asyncio
async def test_stale_record_triggers_refresh():
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)
    settings = _settings(company_research_ttl_hours=1)

    await service.get_or_run(db, job, settings)

    stale_at = datetime.now(UTC) - timedelta(hours=2)
    normalized_name = normalize_company_name(job.company)
    record = get_company_research_by_identity(db, None, normalized_name)
    record.researched_at = stale_at
    db.commit()

    provider.result = _data(short_summary="refreshed")
    refreshed = await service.get_or_run(db, job, settings)

    assert provider.call_count == 2
    assert refreshed.refresh_attempted is True
    assert refreshed.refresh_succeeded is True
    assert refreshed.research.short_summary == "refreshed"


@pytest.mark.asyncio
async def test_force_refresh_bypasses_fresh_cache():
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)
    settings = _settings()

    await service.get_or_run(db, job, settings)
    provider.result = _data(short_summary="forced")
    result = await service.get_or_run(db, job, settings, force_refresh=True)

    assert provider.call_count == 2
    assert result.refresh_attempted is True
    assert result.research.short_summary == "forced"


# --- failure isolation / RunResponse semantics ------------------------------


@pytest.mark.asyncio
async def test_provider_failure_with_existing_record_serves_stale():
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(result=_data(short_summary="original"))
    service = CompanyResearchService(provider=provider)
    settings = _settings()

    original = await service.get_or_run(db, job, settings)

    provider.error = RuntimeError("provider exploded")
    result = await service.get_or_run(db, job, settings, force_refresh=True)

    assert result.refresh_attempted is True
    assert result.refresh_succeeded is False
    assert result.served_stale is True
    assert result.error is not None
    assert result.research.short_summary == "original"
    assert result.research.id == original.research.id
    assert result.research.research_status == "PARTIAL"


@pytest.mark.asyncio
async def test_provider_failure_with_no_prior_record_is_not_a_success():
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(error=RuntimeError("boom"))
    service = CompanyResearchService(provider=provider)
    settings = _settings()

    result = await service.get_or_run(db, job, settings)

    assert result.refresh_attempted is True
    assert result.refresh_succeeded is False
    assert result.served_stale is False
    assert result.research is None
    assert result.error is not None

    # A diagnostic FAILED row is still persisted so the next call retries
    # automatically and the failure is visible for troubleshooting.
    normalized_name = normalize_company_name(job.company)
    persisted = get_company_research_by_identity(db, None, normalized_name)
    assert persisted is not None
    assert persisted.research_status == "FAILED"
    assert persisted.last_attempt_status == "FAILED"


@pytest.mark.asyncio
async def test_repeated_failure_never_reports_served_stale_or_success():
    """RR-M-02 (Codex second re-review): a first failed attempt with no
    prior good record persists a minimal FAILED diagnostic row (see
    record_failed_attempt). A *second* consecutive failure must not treat
    that diagnostic row as "prior good research" — it is not usable content
    (research_status never reached PARTIAL) — so it must keep reporting
    research=None/served_stale=False, exactly like the first failure, not
    silently become a served_stale=True/HTTP-200-eligible response.
    """
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(error=RuntimeError("boom"))
    service = CompanyResearchService(provider=provider)
    settings = _settings()

    first = await service.get_or_run(db, job, settings)
    second = await service.get_or_run(db, job, settings)

    for result in (first, second):
        assert result.research is None
        assert result.refresh_attempted is True
        assert result.refresh_succeeded is False
        assert result.refresh_superseded is False
        assert result.served_stale is False
        assert result.error is not None

    normalized_name = normalize_company_name(job.company)
    persisted = get_company_research_by_identity(db, None, normalized_name)
    assert persisted is not None
    assert persisted.research_status == "FAILED"
    assert persisted.last_attempt_status == "FAILED"


@pytest.mark.asyncio
async def test_not_configured_error_propagates_instead_of_being_swallowed():
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(error=ProviderNotConfiguredError("needs an API key"))
    service = CompanyResearchService(provider=provider)
    settings = _settings()

    with pytest.raises(ProviderNotConfiguredError):
        await service.get_or_run(db, job, settings)


# --- H-02: Job.url must never become company identity -----------------------


@pytest.mark.asyncio
async def test_domain_hint_is_always_none_regardless_of_job_url():
    """Company Research v1 has no trusted domain source — Job.url is a job
    posting/job-board URL, never the company's own website (see
    app/services/company_research.py module docstring).
    """
    db = _db()
    captured = []

    class _CapturingProvider(CompanyResearchProvider):
        name = "capturing"

        async def research(self, job, *, domain_hint=None):
            captured.append(domain_hint)
            return _data()

    service = CompanyResearchService(provider=_CapturingProvider())

    for i, url in enumerate(
        (
            "https://jobs.lever.co/acme/1",
            "https://boards.greenhouse.io/acme/jobs/2",
            "https://xing.com/tracking/abc123",
            "https://careers.acme.com/postings/1",
        )
    ):
        job = _seed_job(
            db,
            fingerprint=url,
            url=url,
            company=f"Company {i}",
            source="xing" if i == 2 else "bundesagentur",
        )
        await service.get_or_run(db, job, _settings())

    assert captured == [None, None, None, None]


@pytest.mark.asyncio
async def test_two_companies_sharing_the_same_ats_host_stay_separate_records():
    db = _db()
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)
    settings = _settings()

    acme_job = _seed_job(
        db, fingerprint="acme", company="Acme GmbH", url="https://jobs.lever.co/acme/1"
    )
    provider.result = _data(company_name="Acme GmbH")
    acme_result = await service.get_or_run(db, acme_job, settings)

    globex_job = _seed_job(
        db, fingerprint="globex", company="Globex AG", url="https://jobs.lever.co/globex/2"
    )
    provider.result = _data(company_name="Globex AG")
    globex_result = await service.get_or_run(db, globex_job, settings)

    assert acme_result.research.id != globex_result.research.id
    assert acme_result.research.company_name == "Acme GmbH"
    assert globex_result.research.company_name == "Globex AG"


# --- blank/invalid company identity ------------------------------------------


@pytest.mark.asyncio
async def test_blank_company_name_raises_invalid_identity_error():
    db = _db()
    job = _seed_job(db, company="   ")
    service = CompanyResearchService(provider=_FakeProvider(result=_data()))

    with pytest.raises(InvalidCompanyIdentityError):
        await service.get_or_run(db, job, _settings())


def test_get_cached_returns_none_for_blank_company_name():
    db = _db()
    job = _seed_job(db, company="   ")
    service = CompanyResearchService(provider=_FakeProvider(result=_data()))

    assert service.get_cached(db, job) is None


# --- FR-M-01: ambiguous name-only identity -----------------------------------


def _seed_two_known_domain_companies(db: Session) -> None:
    upsert_company_research(
        db,
        _data(short_summary="german"),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )
    upsert_company_research(
        db,
        _data(short_summary="international"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )


@pytest.mark.asyncio
async def test_get_or_run_raises_ambiguous_identity_when_two_known_domains_share_a_name():
    """FR-M-01: Company Research v1 always calls with domain_hint=None (see
    H-02) — without a domain of its own to disambiguate, a name shared by
    two distinct known-domain companies on file must never be silently
    resolved to whichever one happens to hold the coordination alias.
    """
    db = _db()
    _seed_two_known_domain_companies(db)
    job = _seed_job(db, company="Acme GmbH")
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)

    with pytest.raises(AmbiguousCompanyIdentityError):
        await service.get_or_run(db, job, _settings())

    # No provider run against an arbitrarily-picked identity.
    assert provider.call_count == 0

    # Neither company's stored research was disturbed.
    acme_de = get_company_research_by_identity(db, "acme.de", "acme gmbh")
    acme_com = get_company_research_by_identity(db, "acme.com", "acme gmbh")
    assert acme_de.short_summary == "german"
    assert acme_com.short_summary == "international"


def test_get_cached_raises_ambiguous_identity_when_two_known_domains_share_a_name():
    db = _db()
    _seed_two_known_domain_companies(db)
    job = _seed_job(db, company="Acme GmbH")
    service = CompanyResearchService(provider=_FakeProvider(result=_data()))

    with pytest.raises(AmbiguousCompanyIdentityError):
        service.get_cached(db, job)


@pytest.mark.asyncio
async def test_get_or_run_with_exactly_one_known_domain_is_not_ambiguous():
    """Zero or exactly one known domain for a name is unambiguous — only
    2+ triggers the FR-M-01 guard."""
    db = _db()
    upsert_company_research(
        db,
        _data(short_summary="only-one"),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )
    job = _seed_job(db, company="Acme GmbH")
    service = CompanyResearchService(provider=_FakeProvider(result=_data()))

    result = await service.get_or_run(db, job, _settings())

    assert result.research is not None


# --- FR-M-03: sole known-domain identity resolution --------------------------


def _seed_sole_known_domain_company(db: Session) -> CompanyResearchRecord:
    record, outcome = upsert_company_research(
        db,
        _data(short_summary="original"),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )
    assert outcome.value == "CREATED"
    return record


@pytest.mark.asyncio
async def test_a_fresh_cache_resolves_sole_known_domain_without_calling_provider():
    """Test A: a job whose company resolves (name-only, domain_hint=None,
    the only kind v1 ever produces) to a single already-known-domain record
    must hit the fresh-cache path exactly like a name-only record would —
    not treat the domain-bearing identity_key mismatch as a cache miss and
    call the provider needlessly.
    """
    db = _db()
    seeded = _seed_sole_known_domain_company(db)
    job = _seed_job(db, company="Acme GmbH")
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)

    result = await service.get_or_run(db, job, _settings())

    assert provider.call_count == 0
    assert result.refresh_attempted is False
    assert result.refresh_succeeded is True
    assert result.refresh_superseded is False
    assert result.served_stale is False
    assert result.error is None
    assert result.research.id == seeded.id
    assert result.research.short_summary == "original"

    total = db.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1


@pytest.mark.asyncio
async def test_b_force_refresh_preserves_sole_known_domain_identity():
    """Test B: force_refresh on the sole known-domain record must run the
    provider exactly once, persist under the *same* domain identity (never
    downgrade to a domain-less "name:<x>" row just because domain_hint is
    None), keep the same row id, and bump the version — no new row.
    """
    db = _db()
    seeded = _seed_sole_known_domain_company(db)
    seeded_id = seeded.id
    seeded_version = seeded.version
    job = _seed_job(db, company="Acme GmbH")
    provider = _FakeProvider(result=_data(short_summary="refreshed"))
    service = CompanyResearchService(provider=provider)

    result = await service.get_or_run(db, job, _settings(), force_refresh=True)

    assert provider.call_count == 1
    assert result.refresh_attempted is True
    assert result.refresh_succeeded is True
    assert result.refresh_superseded is False
    assert result.served_stale is False
    assert result.research.id == seeded_id
    assert result.research.short_summary == "refreshed"

    canonical = get_company_research_by_identity(db, "acme.de", "acme gmbh")
    assert canonical is not None
    assert canonical.id == seeded_id
    assert canonical.identity_key == "domain:acme.de"
    assert canonical.normalized_domain == "acme.de"
    assert canonical.version == seeded_version + 1

    total = db.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    # No stray domain-less duplicate was created alongside it.
    assert get_company_research_by_identity(db, None, "acme gmbh") is None


@pytest.mark.asyncio
async def test_d_post_style_fresh_call_does_not_invoke_provider_twice():
    """Test D: two consecutive get_or_run calls (no force_refresh) against
    a fresh sole known-domain record must both hit cache — mirrors the
    behavior a repeat POST /jobs/{id}/research would see.
    """
    db = _db()
    _seed_sole_known_domain_company(db)
    job = _seed_job(db, company="Acme GmbH")
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)

    first = await service.get_or_run(db, job, _settings())
    second = await service.get_or_run(db, job, _settings())

    assert provider.call_count == 0
    assert first.refresh_attempted is False
    assert second.refresh_attempted is False
    assert first.research.id == second.research.id


def test_get_cached_resolves_sole_known_domain():
    """Test C (service-level companion to the endpoint-level GET test):
    get_cached must resolve the sole known-domain record too, using the
    same resolution routine as get_or_run.
    """
    db = _db()
    seeded = _seed_sole_known_domain_company(db)
    job = _seed_job(db, company="Acme GmbH")
    service = CompanyResearchService(provider=_FakeProvider(result=_data()))

    cached = service.get_cached(db, job)

    assert cached is not None
    assert cached.id == seeded.id


# --- GET / get_cached ---------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cached_returns_none_when_nothing_persisted():
    db = _db()
    job = _seed_job(db)
    service = CompanyResearchService(provider=_FakeProvider(result=_data()))

    assert service.get_cached(db, job) is None


@pytest.mark.asyncio
async def test_get_cached_never_calls_provider_even_when_stale():
    db = _db()
    job = _seed_job(db)
    provider = _FakeProvider(result=_data())
    service = CompanyResearchService(provider=provider)
    settings = _settings(company_research_ttl_hours=1)

    await service.get_or_run(db, job, settings)
    normalized_name = normalize_company_name(job.company)
    record = get_company_research_by_identity(db, None, normalized_name)
    record.researched_at = datetime.now(UTC) - timedelta(hours=100)
    db.commit()

    call_count_before = provider.call_count
    cached = service.get_cached(db, job)

    assert cached is not None
    assert provider.call_count == call_count_before


# --- RR-M-03: superseded concurrent force-refresh (Codex second re-review) --


class _BlockingProvider(CompanyResearchProvider):
    """A provider whose research() call blocks on an asyncio.Event until
    released, and signals a second event the instant it starts — lets a
    test deterministically interleave two concurrent get_or_run calls on
    one asyncio event loop without real threads or timing-based sleeps.
    """

    name = "blocking"

    def __init__(
        self, result: CompanyResearchData, *, started: asyncio.Event, release: asyncio.Event
    ):
        self.result = result
        self.started = started
        self.release = release

    async def research(self, job, *, domain_hint=None):
        self.started.set()
        await self.release.wait()
        return self.result


@pytest.mark.asyncio
async def test_concurrent_force_refresh_reports_superseded_for_the_loser(tmp_path):
    """Service-level version of the RR-M-03 scenario (a repository-level
    test alone isn't enough per Codex's second re-review): two concurrent
    force_refresh calls for the same job, driven through the real
    CompanyResearchService.get_or_run — not just upsert_company_research
    directly — on two independent Sessions sharing one file-backed SQLite
    DB (so writes from one are actually visible to the other, unlike
    :memory:).

    Sequence: DB starts at version 1. Caller A's provider call is made to
    block; caller B's provider call completes immediately and persists
    version 2. Caller A is then released, its provider succeeds, but its
    persist attempt is stale (expected_version=1, actual=2) and must come
    back SUPERSEDED — never silently reported as a successful refresh, and
    never clobbering B's already-committed result.
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent_service.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    from app.db.repositories import upsert_company_research

    seed_db = factory()
    job = _seed_job(seed_db)
    job_id = job.id
    normalized_name = normalize_company_name(job.company)
    seeded, _ = upsert_company_research(
        seed_db,
        _data(short_summary="v1"),
        normalized_domain=None,
        normalized_company_name=normalized_name,
    )
    assert seeded.version == 1
    seed_db.close()

    db_a = factory()
    db_b = factory()
    job_a = db_a.get(JobRecord, job_id)
    job_b = db_b.get(JobRecord, job_id)

    a_started = asyncio.Event()
    a_release = asyncio.Event()
    service_a = CompanyResearchService(
        provider=_BlockingProvider(
            _data(short_summary="payload-A"), started=a_started, release=a_release
        )
    )
    service_b = CompanyResearchService(
        provider=_FakeProvider(result=_data(short_summary="payload-B"))
    )
    settings = _settings()

    task_a = asyncio.create_task(service_a.get_or_run(db_a, job_a, settings, force_refresh=True))
    await a_started.wait()

    result_b = await service_b.get_or_run(db_b, job_b, settings, force_refresh=True)
    assert result_b.refresh_succeeded is True
    assert result_b.refresh_superseded is False
    assert result_b.served_stale is False
    assert result_b.research.short_summary == "payload-B"

    a_release.set()
    result_a = await task_a

    assert result_a.refresh_succeeded is False
    assert result_a.refresh_superseded is True
    assert result_a.served_stale is False
    assert result_a.error is not None
    assert result_a.research is not None
    assert result_a.research.short_summary == "payload-B"

    final_db = factory()
    canonical = get_company_research_by_identity(final_db, None, normalized_name)
    assert canonical.short_summary == "payload-B"
    assert canonical.version == 2
    db_a.close()
    db_b.close()
    final_db.close()
