"""Audit FLOW-01: a collector announces a vacancy once, not on every run."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord
from app.models.job import Job, JobScore
from app.services.collector_runner import run_bundesagentur


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'flow_notifications.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _no_fit_barrier(monkeypatch):
    # The shortlist-safety gate has its own tests; here the synthetic job is not a real fit.
    monkeypatch.setattr("app.services.collector_runner.confirmed_barrier", lambda db, job_id: None)


class _Scorer:
    def __init__(self, profile_skills):
        pass

    def score(self, job):
        return JobScore(
            score=90,
            recommendation="APPLY",
            data_confidence=0.9,
            matched_must_have=["python", "fastapi"],
        )


class _Collector:
    def __init__(self, jobs):
        self._jobs = jobs
        self.skipped_invalid_count = 0

    async def fetch(self, since=None):
        return self._jobs

    async def fetch_detail(self, referenznummer):
        return None


class _Notifier:
    sent: list[tuple[str, bool]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def send_job(self, job, score):
        _Notifier.sent.append((job.title, score.is_duplicate))
        return True


def _job(**overrides):
    data = {
        "source": "bundesagentur",
        "title": "Python Developer",
        "company": "Muster GmbH",
        "url": "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1",
        "description": "",
        "skills": ["python"],
        "source_reference": "10000-1",
    }
    data.update(overrides)
    return Job(**data)


def _settings():
    return Settings(
        bundesagentur_api_key="upstream-key",
        company_research_auto_enabled=False,
        telegram_vacancy_feed_enabled=False,
        min_job_score_to_notify=50,
    )


async def _run(db, monkeypatch, jobs):
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector",
        lambda **kwargs: _Collector(jobs),
    )
    monkeypatch.setattr("app.agents.job_score_evaluator.JobScorer", lambda skills: _Scorer(skills))
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _Notifier)
    return await run_bundesagentur(db, _settings())


@pytest.mark.asyncio
async def test_a_known_vacancy_is_not_announced_again_on_the_next_run(db, monkeypatch):
    _Notifier.sent = []
    await _run(db, monkeypatch, [_job()])
    await _run(db, monkeypatch, [_job()])
    await _run(db, monkeypatch, [_job()])
    assert _Notifier.sent == [("Python Developer", False)]
    assert db.query(JobRecord).count() == 1


@pytest.mark.asyncio
async def test_a_new_vacancy_in_a_later_run_is_still_announced(db, monkeypatch):
    _Notifier.sent = []
    await _run(db, monkeypatch, [_job()])
    second = _job(
        title="Backend Developer",
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-2",
        source_reference="10000-2",
    )
    await _run(db, monkeypatch, [_job(), second])
    assert [title for title, _ in _Notifier.sent] == ["Python Developer", "Backend Developer"]


@pytest.mark.asyncio
async def test_an_applied_vacancy_is_never_announced_again(db, monkeypatch):
    from app.db.repositories import update_job_status
    from app.models.application_status import ApplicationStatus

    _Notifier.sent = []
    await _run(db, monkeypatch, [_job()])
    job_id = db.query(JobRecord).one().id
    update_job_status(db, job_id, ApplicationStatus.APPLIED)
    await _run(db, monkeypatch, [_job()])
    assert len(_Notifier.sent) == 1


# ---- FLOW-05: a BA detail-API outage must not drop new vacancies ------------------------


class _DetailOutageCollector(_Collector):
    def __init__(self, jobs, *, fail_with):
        super().__init__(jobs)
        self.fail_with = fail_with
        self.detail_calls = 0

    async def fetch_detail(self, referenznummer):
        self.detail_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return "Python FastAPI Entwickler. Wir bieten Remote-Arbeit."


def _three_jobs():
    return [
        _job(
            title=f"Python Developer {i}",
            url=f"https://www.arbeitsagentur.de/jobsuche/jobdetail/2000{i}-1",
            source_reference=f"2000{i}-1",
        )
        for i in range(3)
    ]


async def _run_with(db, monkeypatch, collector):
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector", lambda **kwargs: collector
    )
    monkeypatch.setattr("app.agents.job_score_evaluator.JobScorer", lambda skills: _Scorer(skills))
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _Notifier)
    return await run_bundesagentur(db, _settings())


@pytest.mark.asyncio
async def test_detail_outage_saves_the_vacancies_without_description(db, monkeypatch):
    from app.collectors.bundesagentur import BundesagenturAPIError

    _Notifier.sent = []
    collector = _DetailOutageCollector(_three_jobs(), fail_with=BundesagenturAPIError("503"))
    result = await _run_with(db, monkeypatch, collector)
    assert db.query(JobRecord).count() == 3
    assert result["created"] == 3 and result["failed"] == 0


@pytest.mark.asyncio
async def test_breaker_stops_detail_calls_after_the_first_exhausted_failure(db, monkeypatch):
    from app.collectors.bundesagentur import BundesagenturAPIError

    collector = _DetailOutageCollector(_three_jobs(), fail_with=BundesagenturAPIError("503"))
    await _run_with(db, monkeypatch, collector)
    assert collector.detail_calls == 1  # one ~33 s attempt per run, not one per vacancy


@pytest.mark.asyncio
async def test_next_run_after_recovery_enriches_the_stored_vacancies(db, monkeypatch):
    from app.collectors.bundesagentur import BundesagenturAPIError

    await _run_with(
        db,
        monkeypatch,
        _DetailOutageCollector(_three_jobs(), fail_with=BundesagenturAPIError("503")),
    )
    assert all(not row.description.strip() for row in db.query(JobRecord).all())
    healthy = _DetailOutageCollector(_three_jobs(), fail_with=None)
    result = await _run_with(db, monkeypatch, healthy)
    db.expire_all()
    assert healthy.detail_calls == 3
    assert result["created"] == 0 and result["failed"] == 0
    assert all("FastAPI" in row.description for row in db.query(JobRecord).all())
    assert db.query(JobRecord).count() == 3


@pytest.mark.asyncio
async def test_a_rejected_api_key_still_fails_the_job(db, monkeypatch):
    from app.collectors.bundesagentur import BundesagenturAuthError

    collector = _DetailOutageCollector(_three_jobs(), fail_with=BundesagenturAuthError("401"))
    result = await _run_with(db, monkeypatch, collector)
    assert db.query(JobRecord).count() == 0
    assert result["failed"] == 3


# ---- FLOW-01 follow-up: a vacancy that first BECOMES APPLY is still announced once ------


class _DescriptionAwareScorer:
    """MAYBE without a description (as after a detail outage), APPLY once it has one."""

    def __init__(self, profile_skills):
        pass

    def score(self, job):
        if not job.description.strip():
            return JobScore(score=40, recommendation="MAYBE", data_confidence=0.3)
        return JobScore(
            score=90,
            recommendation="APPLY",
            data_confidence=0.9,
            matched_must_have=["python", "fastapi"],
        )


@pytest.mark.asyncio
async def test_vacancy_that_becomes_apply_after_enrichment_is_announced_exactly_once(
    db, monkeypatch
):
    from app.collectors.bundesagentur import BundesagenturAPIError

    _Notifier.sent = []
    jobs = _three_jobs()[:1]
    monkeypatch.setattr(
        "app.agents.job_score_evaluator.JobScorer", lambda skills: _DescriptionAwareScorer(skills)
    )

    async def run(collector):
        monkeypatch.setattr(
            "app.services.collector_runner.BundesagenturCollector", lambda **kw: collector
        )
        monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _Notifier)
        return await run_bundesagentur(db, _settings())

    await run(_DetailOutageCollector(jobs, fail_with=BundesagenturAPIError("503")))
    assert _Notifier.sent == []  # saved as MAYBE without a description: nothing to announce yet
    await run(_DetailOutageCollector(jobs, fail_with=None))
    assert len(_Notifier.sent) == 1  # became APPLY on enrichment: announced
    await run(_DetailOutageCollector(jobs, fail_with=None))
    assert len(_Notifier.sent) == 1  # and not again


class _ScoreSequenceScorer:
    """Returns the scores of `_ScoreSequenceScorer.scores` in order, one per run."""

    scores: list[int] = []
    calls = 0

    def __init__(self, profile_skills):
        pass

    def score(self, job):
        value = _ScoreSequenceScorer.scores[min(_ScoreSequenceScorer.calls, len(self.scores) - 1)]
        _ScoreSequenceScorer.calls += 1
        return JobScore(
            score=value,
            recommendation="APPLY",
            data_confidence=0.9,
            matched_must_have=["python", "fastapi"],
        )


@pytest.mark.asyncio
async def test_a_score_crossing_the_notify_threshold_is_announced_once(db, monkeypatch):
    _Notifier.sent = []
    _ScoreSequenceScorer.scores, _ScoreSequenceScorer.calls = [40, 95, 95], 0
    monkeypatch.setattr(
        "app.agents.job_score_evaluator.JobScorer", lambda skills: _ScoreSequenceScorer(skills)
    )
    monkeypatch.setattr(
        "app.services.collector_runner.BundesagenturCollector", lambda **kw: _Collector([_job()])
    )
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _Notifier)
    settings = Settings(
        bundesagentur_api_key="upstream-key",
        company_research_auto_enabled=False,
        telegram_vacancy_feed_enabled=False,
        min_job_score_to_notify=90,
    )
    await run_bundesagentur(db, settings)  # APPLY but 40 < 90: not announce-eligible
    assert _Notifier.sent == []
    await run_bundesagentur(db, settings)  # 95 >= 90: crosses the threshold -> announced
    assert len(_Notifier.sent) == 1
    await run_bundesagentur(db, settings)  # already announce-eligible -> not again
    assert len(_Notifier.sent) == 1
