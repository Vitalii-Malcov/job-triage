"""Codex gate follow-up (Astra R4A, lease-loss MEDIUM) regression tests:
`app.services.collector_runner.run_bundesagentur`/`run_xing` accept an
OPTIONAL `is_lease_lost` callback. `app.services.automation
.run_automation_cycle`'s own between-STEP checks (`_raise_if_lease_lost`)
cannot see a lease lost mid-way through ONE step's own per-job loop -- a
single collector run can process many jobs, each potentially making a
real external Company Research/Telegram call. These tests prove:

* once `is_lease_lost()` starts returning True, NO new Company
  Research/Telegram call is launched for any LATER job in the same run;
* a job already fully scored/persisted before that point stays durable
  (never rolled back/undone);
* every STANDALONE caller (the manual endpoint, the Telegram bot
  command) omits `is_lease_lost` entirely and sees zero behavior change
  -- verified here by calling `run_bundesagentur`/`run_xing` directly
  with no `is_lease_lost` argument at all.

Mirrors tests/test_company_research_auto_budget.py's fake-service
approach (no real network I/O anywhere).
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord
from app.models.company_research import CompanyResearchResponse, CompanyResearchRunResponse
from app.models.job import Job, JobScore
from app.services.collector_runner import run_bundesagentur, run_xing

ACCOUNT = "me@example.com"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test_collector_runner_lease_guard.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


class FakeJobScorer:
    """Every job scores APPLY at a fixed score, high enough to clear
    `min_job_score_to_notify` -- so both auto-research and Telegram
    notification would normally fire for every job, making a suppressed
    call unambiguous evidence of the lease guard actually working.
    """

    def __init__(self, profile_skills):
        pass

    def score(self, job: Job) -> JobScore:
        return JobScore(score=90, recommendation="APPLY", data_confidence=0.9)


class FakeBundesagenturCollector:
    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs
        self.skipped_invalid_count = 0

    async def fetch(self, since=None) -> list[Job]:
        return self._jobs

    async def fetch_detail(self, referenznummer: str) -> str | None:
        return None


class CountingResearchService:
    """Stand-in for CompanyResearchService that just counts+records which
    jobs it was called for -- mirrors
    tests/test_company_research_auto_budget.py's own fake.
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def get_or_run(self, db, job, settings, *, force_refresh=False):
        CountingResearchService.calls.append(job.company)
        now = datetime.now(UTC)
        research = CompanyResearchResponse(
            id=len(CountingResearchService.calls),
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

    calls: list[str] = []


class CountingNotifier:
    """Stand-in for TelegramNotifier that just counts+records which jobs
    it was called for -- no real HTTP client anywhere.
    """

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def send_job(self, job: Job, score: JobScore) -> bool:
        CountingNotifier.calls.append(job.company)
        return True

    calls: list[str] = []


def _make_is_lease_lost_after_n_calls(threshold: int):
    """Returns False for the first `threshold` calls, True forever after
    -- lets a test place the transition to "lease lost" at an exact call
    number instead of racing real timing.
    """
    state = {"count": 0}

    def _check() -> bool:
        state["count"] += 1
        return state["count"] > threshold

    return _check


def _ba_job(**overrides) -> Job:
    data = {
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "First Company",
        "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/first",
        "description": "",
        "skills": ["python"],
    }
    data.update(overrides)
    return Job(**data)


def _settings(**overrides) -> Settings:
    data = dict(
        bundesagentur_api_key="upstream-key",
        xing_mailbox_username="xing-user@example.com",
        xing_mailbox_app_password="app-password",
        company_research_auto_enabled=True,
        company_research_auto_max_per_run=20,
        min_job_score_to_notify=50,
    )
    data.update(overrides)
    return Settings(**data)


class TestBundesagenturLeaseGuard:
    async def _run(self, db, monkeypatch, jobs, *, is_lease_lost=None):
        CountingResearchService.calls = []
        CountingNotifier.calls = []
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector",
            lambda **kwargs: FakeBundesagenturCollector(jobs),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.JobScorer",
            lambda profile_skills: FakeJobScorer(profile_skills),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.CompanyResearchService", CountingResearchService
        )
        monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", CountingNotifier)
        kwargs = {} if is_lease_lost is None else {"is_lease_lost": is_lease_lost}
        return await run_bundesagentur(db, _settings(), **kwargs)

    @pytest.mark.asyncio
    async def test_lease_lost_mid_run_stops_new_research_and_telegram_calls(self, db, monkeypatch):
        jobs = [
            _ba_job(company="First Company", url="https://example.com/jobs/first"),
            _ba_job(company="Second Company", url="https://example.com/jobs/second"),
        ]
        # False for job 1's research check, True from job 1's own
        # notify check onward -- so job 1 still gets its research call
        # (lease not yet confirmed lost), but NEITHER job 1's own
        # Telegram notification NOR ANY of job 2's calls fire.
        is_lease_lost = _make_is_lease_lost_after_n_calls(1)

        result = await self._run(db, monkeypatch, jobs, is_lease_lost=is_lease_lost)

        # Core job persistence is completely unaffected by lease loss --
        # both jobs scored/persisted durably.
        assert result["created"] == 2
        assert result["failed"] == 0

        # Company Research: only job 1 (before loss was confirmed).
        assert CountingResearchService.calls == ["First Company"]
        # Telegram: zero -- loss was already confirmed by job 1's own
        # notify check, let alone job 2's.
        assert CountingNotifier.calls == []

        # Already-committed core job data is genuinely durable -- reload
        # via a fresh query.
        db.expire_all()
        titles = {job.title for job in db.query(JobRecord).all()}
        assert titles == {"Python Developer"}
        assert db.query(JobRecord).count() == 2

    @pytest.mark.asyncio
    async def test_lease_never_lost_makes_no_difference(self, db, monkeypatch):
        """Sanity counterpart: an `is_lease_lost` that always reports
        `False` must not change behavior at all versus not passing one.
        """
        jobs = [_ba_job(company="Solo Company", url="https://example.com/jobs/solo")]

        result = await self._run(db, monkeypatch, jobs, is_lease_lost=lambda: False)

        assert result["created"] == 1
        assert CountingResearchService.calls == ["Solo Company"]
        assert CountingNotifier.calls == ["Solo Company"]

    @pytest.mark.asyncio
    async def test_standalone_caller_without_lease_guard_is_unaffected(self, db, monkeypatch):
        """Every standalone caller (the manual endpoint, the Telegram bot
        command) omits `is_lease_lost` entirely -- calling
        `run_bundesagentur` with no such argument at all must work
        exactly as before this fix.
        """
        jobs = [_ba_job(company="Solo Company", url="https://example.com/jobs/solo")]

        result = await self._run(db, monkeypatch, jobs, is_lease_lost=None)

        assert result["created"] == 1
        assert CountingResearchService.calls == ["Solo Company"]
        assert CountingNotifier.calls == ["Solo Company"]


class TestXingLeaseGuard:
    async def _run(self, db, monkeypatch, jobs, *, is_lease_lost=None):
        CountingResearchService.calls = []
        CountingNotifier.calls = []

        class _FakeXingCollector:
            def __init__(self, **kwargs) -> None:
                self.skipped_invalid_count = 0
                self.deadline_exceeded = False

            async def fetch_message_batches(self, since=None):
                from app.collectors.xing_email import XingEmailBatch

                return [XingEmailBatch(message_id="<digest@mail.xing.com>", jobs=tuple(jobs))]

        monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _FakeXingCollector)
        monkeypatch.setattr(
            "app.services.collector_runner.JobScorer",
            lambda profile_skills: FakeJobScorer(profile_skills),
        )
        monkeypatch.setattr(
            "app.services.collector_runner.CompanyResearchService", CountingResearchService
        )
        monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", CountingNotifier)
        kwargs = {} if is_lease_lost is None else {"is_lease_lost": is_lease_lost}
        return await run_xing(db, _settings(), **kwargs)

    @pytest.mark.asyncio
    async def test_lease_lost_mid_run_stops_new_research_and_telegram_calls(self, db, monkeypatch):
        jobs = [
            Job(
                source="xing",
                title="Backend Engineer",
                company="First Company",
                url="https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1",
                description="",
                skills=[],
            ),
            Job(
                source="xing",
                title="Frontend Engineer",
                company="Second Company",
                url="https://www.xing.com/m/BBBBBBBBBBBBBBBBBBBB2",
                description="",
                skills=[],
            ),
        ]
        is_lease_lost = _make_is_lease_lost_after_n_calls(1)

        result = await self._run(db, monkeypatch, jobs, is_lease_lost=is_lease_lost)

        assert result["created"] == 2
        assert result["failed"] == 0
        assert CountingResearchService.calls == ["First Company"]
        assert CountingNotifier.calls == []

        db.expire_all()
        assert db.query(JobRecord).count() == 2

    @pytest.mark.asyncio
    async def test_standalone_caller_without_lease_guard_is_unaffected(self, db, monkeypatch):
        jobs = [
            Job(
                source="xing",
                title="Backend Engineer",
                company="Solo Company",
                url="https://www.xing.com/m/CCCCCCCCCCCCCCCCCCCC3",
                description="",
                skills=[],
            )
        ]

        result = await self._run(db, monkeypatch, jobs, is_lease_lost=None)

        assert result["created"] == 1
        assert CountingResearchService.calls == ["Solo Company"]
        assert CountingNotifier.calls == ["Solo Company"]
