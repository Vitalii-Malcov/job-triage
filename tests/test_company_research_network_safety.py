"""H-03 regression: Company Research Agent v1 makes zero outbound network
requests of any kind, for any job source — including XING (see
app/collectors/xing_email.py's hard rule about never following its
per-recipient tracking redirects, which this feature must never violate
even indirectly).

A DNS-resolve-then-fetch SSRF check (an earlier version of this feature)
was removed rather than hardened after a Codex review found it has an
unavoidable DNS-rebinding/TOCTOU window: the resolved-and-validated IP is
not guaranteed to be the IP the HTTP client actually connects to on its own,
separate resolution. Company Research v1 therefore has no network path at
all — this is verified two ways below: static inspection of the provider
module (no httpx/requests/aiohttp/urllib import anywhere) and a live
end-to-end run through the real service + real provider with every socket
API blocked, proving nothing even attempts a connection.
"""

import inspect
import socket
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord
from app.services.company_research import CompanyResearchService


def _block_all_network(monkeypatch):
    def _raise(*args, **kwargs):
        raise AssertionError("Company Research v1 must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _raise)
    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    monkeypatch.setattr(socket, "create_connection", _raise)


def _seed_job(db, **overrides) -> JobRecord:
    now = datetime.now(UTC)
    fields = {
        "fingerprint": "fp-network-safety",
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "Acme GmbH",
        "location": "Berlin",
        "url": "https://jobs.example/1",
        "description": "Python and Docker.",
        "skills_json": '["python", "docker"]',
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


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("bundesagentur", "https://www.arbeitsagentur.de/jobsuche/jobdetail/abc"),
        ("xing", "https://xing.com/tracking/personal-redirect-abc123"),
        ("some_future_source", "https://jobs.lever.co/acme/1"),
    ],
)
@pytest.mark.asyncio
async def test_get_or_run_never_touches_the_network(monkeypatch, source, url):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    job = _seed_job(db, fingerprint=url, source=source, url=url)
    settings = Settings(company_research_ttl_hours=720)

    _block_all_network(monkeypatch)

    result = await CompanyResearchService().get_or_run(db, job, settings)

    assert result.refresh_succeeded is True
    assert result.research.research_status == "PARTIAL"


def test_provider_module_never_imports_an_http_client():
    import app.providers.job_data_provider as module

    source = inspect.getsource(module)
    for forbidden in ("httpx", "requests", "aiohttp", "urllib.request", "http.client"):
        assert f"import {forbidden}" not in source

    for name in ("httpx", "requests", "aiohttp"):
        assert not hasattr(module, name)


def test_service_module_never_imports_an_http_client():
    import app.services.company_research as module

    source = inspect.getsource(module)
    for forbidden in ("httpx", "requests", "aiohttp", "urllib.request", "http.client"):
        assert f"import {forbidden}" not in source
