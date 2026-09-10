"""Codex gate follow-up (Astra R4A, XING starvation MEDIUM) regression:
`app.services.collector_runner.run_xing` persists a durable IMAP UID scan
watermark (`app.db.xing_scan_progress_repository`) so a large
already-processed prefix at the front of the mailbox's search window
gets skipped ENTIRELY on later runs -- not re-examined message by
message -- letting a bounded per-run scan budget eventually reach a
genuinely new message that a fixed-budget scan starting from the very
beginning every single cycle would starve forever.

`_FakeXingCollector` below deliberately reimplements the real
`XingEmailCollector`'s externally-observable contract (accepts
`scan_from_uid`/`expected_uid_validity`, exposes `.uid_validity`/
`.confirmed_uids`, returns `XingEmailBatch`es carrying `.uid`) rather
than driving the real IMAP fake -- the real collector's OWN
"skip a confirmed UID without any IMAP call" mechanism is already proven
directly in tests/test_collectors_xing_email.py. This test proves the
OTHER half: that `run_xing` reads the persisted watermark, passes it
through correctly, and advances it correctly once persistence succeeds
-- the actual cross-cycle "does this end starvation" story.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.collectors.xing_email import XingEmailBatch
from app.core.config import Settings
from app.db.base import Base
from app.db.models import JobRecord
from app.db.xing_scan_progress_repository import get_xing_scan_progress
from app.models.job import Job, JobScore
from app.services.collector_runner import run_xing

FAKE_UID_VALIDITY = 42
# A prefix long enough that no reasonable per-run scan budget would
# finish it in one cycle -- the actual starvation Codex flagged.
OLD_UIDS = list(range(1, 6))  # 5 already-processed messages
NEW_UID = 6
PER_RUN_SCAN_BUDGET = 2


class FakeJobScorer:
    def __init__(self, profile_skills):
        pass

    def score(self, job: Job) -> JobScore:
        return JobScore(score=90, recommendation="APPLY", data_confidence=0.9)


class _NoOpNotifier:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def send_job(self, job: Job, score: JobScore) -> bool:
        return True


class _NoOpResearchService:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def get_or_run(self, db, job, settings, *, force_refresh=False):
        raise AssertionError("company research must not run in this test")


class _FakeXingCollector:
    """Mimics XingEmailCollector's externally-observable scan-progress
    contract with a bounded PER-CALL scan budget -- simulates "a session
    deadline / IMAP round-trip budget only allows examining
    PER_RUN_SCAN_BUDGET candidates before this run must stop", the exact
    condition that makes an unbounded from-the-start rescan starve a
    message parked after a long already-processed prefix.
    """

    def __init__(self, **kwargs) -> None:
        self.skipped_invalid_count = 0
        self.deadline_exceeded = False
        self._is_message_processed = kwargs["is_message_processed"]
        self._scan_from_uid = kwargs.get("scan_from_uid")
        self._expected_uid_validity = kwargs.get("expected_uid_validity")
        self.uid_validity: int | None = None
        self.confirmed_uids: list[int] = []

    async def fetch_message_batches(self, since=None) -> list[XingEmailBatch]:
        self.uid_validity = FAKE_UID_VALIDITY
        self.confirmed_uids = []
        effective_from = (
            self._scan_from_uid if self._expected_uid_validity == self.uid_validity else None
        )
        all_uids = [*OLD_UIDS, NEW_UID]
        candidates = [u for u in all_uids if effective_from is None or u > effective_from]
        examined = candidates[:PER_RUN_SCAN_BUDGET]
        self.deadline_exceeded = len(examined) < len(candidates)

        batches: list[XingEmailBatch] = []
        for uid in examined:
            message_id = f"<uid-{uid}@mail.xing.com>"
            if self._is_message_processed(message_id):
                self.confirmed_uids.append(uid)
                continue
            job = Job(
                source="xing",
                title=f"Job {uid}",
                company=f"Company {uid}",
                url=f"https://example.com/jobs/{uid}",
                description="",
                skills=[],
            )
            batches.append(XingEmailBatch(message_id=message_id, jobs=(job,), uid=uid))
        return batches


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test_xing_scan_progress.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield session
    session.close()


def _settings() -> Settings:
    return Settings(
        xing_mailbox_username="xing-user@example.com",
        xing_mailbox_app_password="app-password",
        company_research_auto_enabled=False,
        min_job_score_to_notify=50,
    )


def _seed_already_processed(db, uids: list[int]) -> None:
    """Pre-seeds ProcessedEmailMessage rows for `uids` -- simulates a
    mailbox with a real prior history of successfully processed digests,
    independent of anything this test run does.
    """
    from app.db.repositories import mark_message_processed

    for uid in uids:
        mark_message_processed(db, "xing", f"<uid-{uid}@mail.xing.com>")


@pytest.mark.asyncio
async def test_bounded_scan_progress_eventually_reaches_message_after_large_prefix(db, monkeypatch):
    """The core regression: with a per-run scan budget far smaller than
    the already-processed prefix, a naive "always rescan from the start"
    collector would NEVER reach the new message -- every cycle re-spends
    its whole budget on the front of the prefix. The persisted watermark
    must make each cycle skip the ENTIRE already-confirmed prefix in one
    step (not incrementally), so the new message is reached within a
    small, bounded number of cycles.
    """
    _seed_already_processed(db, OLD_UIDS)
    monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _FakeXingCollector)
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _NoOpNotifier)
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", _NoOpResearchService
    )
    settings = _settings()

    # Cycle 1: budget only reaches uid 1-2 (both already-processed) --
    # nothing new created, but the watermark must still persist as far as
    # this cycle actually confirmed.
    result_1 = await run_xing(db, settings)
    assert result_1["created"] == 0
    progress_1 = get_xing_scan_progress(db)
    assert progress_1 is not None
    assert progress_1.uid_validity == FAKE_UID_VALIDITY
    assert progress_1.confirmed_upto_uid == 2

    # Cycle 2: the watermark (confirmed_upto_uid=2) must make THIS cycle
    # start from uid 3, not uid 1 again -- reaching uid 3-4.
    result_2 = await run_xing(db, settings)
    assert result_2["created"] == 0
    progress_2 = get_xing_scan_progress(db)
    assert progress_2.confirmed_upto_uid == 4

    # Cycle 3: watermark now skips straight to uid 5-6 -- the new
    # message (uid 6) is finally reached and persisted.
    result_3 = await run_xing(db, settings)
    assert result_3["created"] == 1
    progress_3 = get_xing_scan_progress(db)
    assert progress_3.confirmed_upto_uid == 6

    db.expire_all()
    titles = {job.title for job in db.query(JobRecord).all()}
    assert titles == {"Job 6"}


@pytest.mark.asyncio
async def test_without_watermark_the_same_budget_would_starve_forever(db, monkeypatch):
    """Negative control proving the scenario above is real: calling the
    FAKE collector directly (bypassing run_xing's watermark plumbing
    entirely, exactly like the pre-fix collector_runner always did by
    never passing scan_from_uid) with the SAME budget never reaches the
    new message no matter how many times it's called.
    """
    _seed_already_processed(db, OLD_UIDS)
    from app.db.repositories import is_message_processed as is_message_processed_fn

    collector = _FakeXingCollector(
        is_message_processed=lambda message_id: is_message_processed_fn(db, "xing", message_id),
        scan_from_uid=None,
        expected_uid_validity=None,
    )

    for _ in range(10):
        batches = await collector.fetch_message_batches()
        assert batches == [], "no watermark -- every call re-scans the same stuck prefix"


@pytest.mark.asyncio
async def test_uid_validity_mismatch_resets_watermark_instead_of_skipping_blindly(db, monkeypatch):
    """A stored watermark from a DIFFERENT UIDVALIDITY epoch (e.g. the
    mailbox was recreated) must never be trusted to skip messages -- the
    fake's own `effective_from` gate models exactly what the real
    XingEmailCollector does in `_fetch_sync_body`. Seed a stale progress
    row with a different uid_validity and prove uid 1 is examined again
    rather than silently skipped.
    """
    from app.db.xing_scan_progress_repository import advance_xing_scan_progress

    advance_xing_scan_progress(db, uid_validity=999, confirmed_upto_uid=6)
    _seed_already_processed(db, OLD_UIDS[:1])  # only uid 1 actually processed already
    monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _FakeXingCollector)
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _NoOpNotifier)
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", _NoOpResearchService
    )

    result = await run_xing(db, _settings())

    # uid_validity mismatch (999 stored vs FAKE_UID_VALIDITY observed) --
    # scan starts fresh from uid 1, not skipping straight past it: uid 1
    # is confirmed already-processed (seeded above), but uid 2 -- which
    # the stale watermark would have silently skipped as "<= 6, already
    # handled" -- is examined and correctly found to be a genuinely new
    # message.
    progress = get_xing_scan_progress(db)
    assert progress.uid_validity == FAKE_UID_VALIDITY
    assert progress.confirmed_upto_uid == 2
    assert result["created"] == 1
