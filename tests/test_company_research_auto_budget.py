"""M-05 regression: the opt-in auto-research hook must not fan out into an
unbounded number of research runs for a single collector run, regardless of
how many APPLY-recommended jobs it produces.
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.company_research import CompanyResearchResponse, CompanyResearchRunResponse
from app.models.job import Job, JobScore
from app.security import rate_limit as rate_limit_module

API_KEY = "test-api-key"


class FakeJobScorer:
    def __init__(self, profile_skills):
        pass

    def score(self, job: Job) -> JobScore:
        return JobScore(score=90, recommendation="APPLY", data_confidence=0.9)


class FakeCollector:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self.skipped_invalid_count = 0

    async def fetch(self, since=None) -> list[Job]:
        return self._jobs

    async def fetch_detail(self, referenznummer: str) -> str | None:
        return None


class CountingResearchService:
    """Stand-in for CompanyResearchService that just counts calls — the
    real research/persistence logic is already covered by
    tests/test_company_research_service.py; this test only cares about how
    many times it gets invoked per collector run.
    """

    call_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def get_or_run(self, db, job, settings, *, force_refresh=False):
        CountingResearchService.call_count += 1
        now = datetime.now(UTC)
        research = CompanyResearchResponse(
            id=CountingResearchService.call_count,
            company_name=job.company,
            provider_name="fake",
            research_status="PARTIAL",
            confidence=0.3,
            researched_at=now,
            last_attempt_at=now,
            last_attempt_status="SUCCESS",
            last_error=None,
            created_at=now,
            updated_at=now,
        )
        return CompanyResearchRunResponse(
            research=research,
            refresh_attempted=True,
            refresh_succeeded=True,
            served_stale=False,
            error=None,
        )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test_auto_budget.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    fake_settings = Settings(
        api_key=API_KEY,
        rate_limit_requests=1000,
        rate_limit_window_seconds=60,
        bundesagentur_api_key="upstream-key",
        company_research_auto_enabled=True,
        company_research_auto_max_per_run=20,
        # Impossibly high on purpose: this test is about the auto-research
        # budget, not Telegram notifications — a real notification attempt
        # per APPLY job would also serialize behind _run_bundesagentur's
        # 1-second inter-notification pacing sleep for no reason here.
        min_job_score_to_notify=101,
    )
    monkeypatch.setattr("app.security.auth.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.security.rate_limit.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.api.routes.get_settings", lambda: fake_settings)
    rate_limit_module._requests.clear()
    rate_limit_module._collector_requests.clear()

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
    rate_limit_module._requests.clear()
    rate_limit_module._collector_requests.clear()


def _auth_headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


def test_auto_research_is_bounded_by_budget_per_collector_run(client, monkeypatch):
    jobs = [
        Job(
            source="bundesagentur",
            title="Python Developer",
            company=f"Company {i}",
            url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{i}",
            description="",
        )
        for i in range(100)
    ]
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector", lambda **kwargs: FakeCollector(jobs)
    )
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    CountingResearchService.call_count = 0
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", CountingResearchService
    )

    response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

    assert response.status_code == 200
    assert response.json() == {
        "fetched": 100,
        "created": 100,
        "updated": 0,
        "skipped_invalid": 0,
        "failed": 0,
    }
    assert CountingResearchService.call_count <= 20


def test_auto_research_disabled_by_default_makes_zero_calls(client, monkeypatch):
    jobs = [
        Job(
            source="bundesagentur",
            title="Python Developer",
            company="Solo Company",
            url="https://www.arbeitsagentur.de/jobsuche/jobdetail/solo",
            description="",
        )
    ]
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector", lambda **kwargs: FakeCollector(jobs)
    )
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    disabled_settings = Settings(
        api_key=API_KEY,
        rate_limit_requests=1000,
        bundesagentur_api_key="upstream-key",
        company_research_auto_enabled=False,
        min_job_score_to_notify=101,
    )
    monkeypatch.setattr("app.api.routes.get_settings", lambda: disabled_settings)
    CountingResearchService.call_count = 0
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", CountingResearchService
    )

    response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

    assert response.status_code == 200
    assert CountingResearchService.call_count == 0


SECRET_TEXT = "secret-upstream-detail-must-not-leak"


class _FailingResearchService:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def get_or_run(self, db, job, settings, *, force_refresh=False):
        raise RuntimeError(SECRET_TEXT)


def test_auto_research_failure_does_not_leak_exception_text(client, monkeypatch, caplog):
    """S8A-004R (Codex re-review, MEDIUM): an unexpected exception from
    the auto-research hook must never surface raw exception text in
    logs, and must remain best-effort — it must not affect the
    collector run's counts/response.
    """
    job = Job(
        source="bundesagentur",
        title="Python Developer",
        company="Solo Company",
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/solo",
        description="",
    )
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector",
        lambda **kwargs: FakeCollector([job]),
    )
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", _FailingResearchService
    )

    with caplog.at_level("DEBUG"):
        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

    assert response.status_code == 200
    assert response.json() == {
        "fetched": 1,
        "created": 1,
        "updated": 0,
        "skipped_invalid": 0,
        "failed": 0,
    }
    assert SECRET_TEXT not in caplog.text
    assert SECRET_TEXT not in response.text
    assert "RuntimeError" in caplog.text


class _SessionPoisoningResearchService:
    """NEW-002 (Astra R4A): simulates a research failure that leaves the
    SHARED SQLAlchemy session needing an explicit rollback before any
    further use — e.g. a real DB-level error mid-FLUSH inside
    `CompanyResearchService.get_or_run`'s own persistence calls
    (`app.db.repositories.upsert_company_research`/
    `record_failed_attempt`, both of which `db.add(...)` then
    `db.commit()`). A plain `raise RuntimeError(...)` (as
    `_FailingResearchService` above does) never actually touches `db`,
    and a plain failing `db.execute(text(...))` (confirmed empirically)
    does NOT put SQLAlchemy's Session into "pending rollback" either --
    only a genuine ORM FLUSH failure does. This fake reproduces that
    precisely: it stages an ORM object that violates a real DB
    constraint and commits it, so the session enters SQLAlchemy's
    "pending rollback" state exactly like a real persistence failure
    inside `get_or_run` would.
    """

    call_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def get_or_run(self, db, job, settings, *, force_refresh=False):
        _SessionPoisoningResearchService.call_count += 1
        from app.db.models import GmailMessageRecord

        # ck_gmail_messages_uid_positive requires uid > 0 -- this insert
        # fails at flush/commit time with a real IntegrityError, exactly
        # the class of failure a genuine research-persistence bug would
        # produce, and leaves `db` needing an explicit rollback.
        db.add(
            GmailMessageRecord(
                thread_id=999999,
                account_key="me@example.com",
                mailbox="INBOX",
                uid_validity=100,
                uid=-1,
                references_json="[]",
                to_addresses_json="[]",
                cc_addresses_json="[]",
                subject="x",
                direction="INBOUND",
                body_plain="",
                body_truncated=False,
                has_html=False,
                attachments_json="[]",
            )
        )
        db.commit()


def test_research_session_poisoning_does_not_abort_later_job_persistence(
    client, monkeypatch, caplog
):
    """NEW-002 (Astra R4A): a research failure that leaves the shared
    session needing rollback must never invalidate LATER, unrelated core
    job scoring/persistence on that same session. Two jobs are fetched;
    the auto-research call for job 1 poisons the session. Without an
    explicit `db.rollback()` in `_maybe_auto_research`'s except block,
    job 2's own `score_and_persist` call would raise
    `PendingRollbackError` (caught one level up by `run_bundesagentur`'s
    own per-job try/except, which itself calls `db.rollback()` and
    counts job 2 as `failed`) — wrongly reporting a real, scorable job as
    failed purely because of the earlier, unrelated best-effort research
    failure. Both jobs must persist cleanly, and the response/logs must
    never surface a raw PendingRollbackError.
    """
    jobs = [
        Job(
            source="bundesagentur",
            title="Python Developer",
            company="First Company",
            url="https://www.arbeitsagentur.de/jobsuche/jobdetail/first",
            description="",
        ),
        Job(
            source="bundesagentur",
            title="Backend Engineer",
            company="Second Company",
            url="https://www.arbeitsagentur.de/jobsuche/jobdetail/second",
            description="",
        ),
    ]
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector", lambda **kwargs: FakeCollector(jobs)
    )
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    _SessionPoisoningResearchService.call_count = 0
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", _SessionPoisoningResearchService
    )

    with caplog.at_level("DEBUG"):
        response = client.post("/api/v1/collectors/bundesagentur/run", headers=_auth_headers())

    assert response.status_code == 200
    body = response.json()
    # Both jobs scored/persisted successfully -- the poisoned research
    # attempt for job 1 must never abort job 2's own persistence.
    assert body["created"] == 2
    assert body["failed"] == 0
    assert _SessionPoisoningResearchService.call_count == 2
    assert "PendingRollbackError" not in caplog.text
    assert "PendingRollbackError" not in response.text
    assert "IntegrityError" in caplog.text  # sanitized type-name-only logging

    # Core job persistence is genuinely durable -- re-query via a brand
    # new request, which gets a completely fresh session from the SAME
    # overridden `get_db` dependency (see the `client` fixture above),
    # proving the rows survived beyond the original request's own session
    # rather than merely appearing durable via in-session object state.
    list_response = client.get("/api/v1/jobs", headers=_auth_headers())
    assert list_response.status_code == 200
    titles = {job["title"] for job in list_response.json()}
    assert {"Python Developer", "Backend Engineer"} <= titles
