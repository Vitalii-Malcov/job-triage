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
from app.db.xing_scan_progress_repository import (
    _advance_existing,
    advance_xing_scan_progress,
    compute_mailbox_scope,
    get_xing_scan_progress,
)
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
        # Codex gate follow-up (Astra R4A HIGH, watermark gap): mirrors
        # the real XingEmailCollector.candidate_uids contract -- see its
        # own docstring. `run_xing` walks this list (not merely the keys
        # of its own handled-UID map) to compute the new watermark.
        self.candidate_uids: list[int] = []

    async def fetch_message_batches(self, since=None) -> list[XingEmailBatch]:
        self.uid_validity = FAKE_UID_VALIDITY
        self.confirmed_uids = []
        effective_from = (
            self._scan_from_uid if self._expected_uid_validity == self.uid_validity else None
        )
        all_uids = [*OLD_UIDS, NEW_UID]
        candidates = [u for u in all_uids if effective_from is None or u > effective_from]
        self.candidate_uids = candidates
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


def _mailbox_scope(settings: Settings) -> str:
    return compute_mailbox_scope(
        settings.xing_mailbox_imap_host,
        settings.xing_mailbox_imap_port,
        settings.xing_mailbox_username,
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
    progress_1 = get_xing_scan_progress(db, mailbox_scope=_mailbox_scope(settings))
    assert progress_1 is not None
    assert progress_1.uid_validity == FAKE_UID_VALIDITY
    assert progress_1.confirmed_upto_uid == 2

    # Cycle 2: the watermark (confirmed_upto_uid=2) must make THIS cycle
    # start from uid 3, not uid 1 again -- reaching uid 3-4.
    result_2 = await run_xing(db, settings)
    assert result_2["created"] == 0
    progress_2 = get_xing_scan_progress(db, mailbox_scope=_mailbox_scope(settings))
    assert progress_2.confirmed_upto_uid == 4

    # Cycle 3: watermark now skips straight to uid 5-6 -- the new
    # message (uid 6) is finally reached and persisted.
    result_3 = await run_xing(db, settings)
    assert result_3["created"] == 1
    progress_3 = get_xing_scan_progress(db, mailbox_scope=_mailbox_scope(settings))
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
    settings = _settings()
    advance_xing_scan_progress(
        db,
        uid_validity=999,
        confirmed_upto_uid=6,
        mailbox_scope=_mailbox_scope(settings),
        observed_uid_validity=None,  # seeding a brand-new row
    )
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

    result = await run_xing(db, settings)

    # uid_validity mismatch (999 stored vs FAKE_UID_VALIDITY observed) --
    # scan starts fresh from uid 1, not skipping straight past it: uid 1
    # is confirmed already-processed (seeded above), but uid 2 -- which
    # the stale watermark would have silently skipped as "<= 6, already
    # handled" -- is examined and correctly found to be a genuinely new
    # message.
    progress = get_xing_scan_progress(db, mailbox_scope=_mailbox_scope(settings))
    assert progress.uid_validity == FAKE_UID_VALIDITY
    assert progress.confirmed_upto_uid == 2
    assert result["created"] == 1


class _FakeXingCollectorUnresolvedUid:
    """Codex gate follow-up (Astra R4A HIGH, watermark gap) regression
    fixture: UID 2 is neither confirmed-skippable nor does it yield a
    batch -- mirrors the real `_fetch_and_process_message`'s `(None,
    False)` return for a non-OK FETCH response (or, equivalently, a UID
    the session deadline never reached). UIDs 1 and 3 resolve normally
    (new, unprocessed messages).
    """

    def __init__(self, **kwargs) -> None:
        self.skipped_invalid_count = 0
        self.deadline_exceeded = False
        self._is_message_processed = kwargs["is_message_processed"]
        self._scan_from_uid = kwargs.get("scan_from_uid")
        self._expected_uid_validity = kwargs.get("expected_uid_validity")
        self.uid_validity: int | None = None
        self.confirmed_uids: list[int] = []
        self.candidate_uids: list[int] = []

    async def fetch_message_batches(self, since=None) -> list[XingEmailBatch]:
        self.uid_validity = FAKE_UID_VALIDITY
        self.confirmed_uids = []
        effective_from = (
            self._scan_from_uid if self._expected_uid_validity == self.uid_validity else None
        )
        all_uids = [1, 2, 3]
        candidates = [u for u in all_uids if effective_from is None or u > effective_from]
        self.candidate_uids = candidates

        batches: list[XingEmailBatch] = []
        for uid in candidates:
            if uid == 2:
                # Simulates a non-OK FETCH: neither added to
                # confirmed_uids nor turned into a batch.
                continue
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


@pytest.mark.asyncio
async def test_watermark_never_advances_past_unresolved_uid(db, monkeypatch):
    """Codex gate follow-up (Astra R4A HIGH) regression: UID 1 handled,
    UID 2's FETCH fails/unresolved, UID 3 handled. The persisted
    watermark must NOT advance past UID 2 -- a prior version walked
    `sorted(handled_uids)`, and since an unresolved UID is simply ABSENT
    from that dict (not merely False), sorting silently deleted the gap
    and let UID 3's success advance the watermark straight past UID 2,
    permanently losing it (never retried again, since the watermark
    already skips past it).
    """
    monkeypatch.setattr(
        "app.services.collector_runner.XingEmailCollector", _FakeXingCollectorUnresolvedUid
    )
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _NoOpNotifier)
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", _NoOpResearchService
    )
    settings = _settings()

    result_1 = await run_xing(db, settings)
    assert result_1["created"] == 2  # UID 1 and UID 3's jobs both persisted

    progress_1 = get_xing_scan_progress(db, mailbox_scope=_mailbox_scope(settings))
    assert progress_1 is not None
    assert progress_1.confirmed_upto_uid == 1  # must NOT advance past UID 2

    # Next cycle must retry UID 2: scan_from_uid=1 means candidates=[2, 3].
    # UID 2 is still unresolved, so the watermark must still not advance.
    result_2 = await run_xing(db, settings)
    assert result_2["created"] == 0  # UID 3's job already persisted (updated, not created)

    progress_2 = get_xing_scan_progress(db, mailbox_scope=_mailbox_scope(settings))
    assert progress_2.confirmed_upto_uid == 1


@pytest.mark.asyncio
async def test_mailbox_scope_isolates_watermark_between_mailboxes(db, monkeypatch):
    """Codex gate follow-up (Astra R4A MEDIUM, mailbox scope) regression:
    mailbox A's watermark must never leak into mailbox B, even when B's
    mailbox happens to report the SAME UIDVALIDITY as A (a real-world
    coincidence the mailbox_scope key exists to guard against -- see
    `compute_mailbox_scope`'s own docstring). B must start a fresh scan.
    """
    monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _FakeXingCollector)
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _NoOpNotifier)
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", _NoOpResearchService
    )

    settings_a = Settings(
        xing_mailbox_imap_host="imap.mailbox-a.example.com",
        xing_mailbox_username="user-a@example.com",
        xing_mailbox_app_password="app-password",
        company_research_auto_enabled=False,
        min_job_score_to_notify=50,
    )
    settings_b = Settings(
        xing_mailbox_imap_host="imap.mailbox-b.example.com",
        xing_mailbox_username="user-b@example.com",
        xing_mailbox_app_password="app-password",
        company_research_auto_enabled=False,
        min_job_score_to_notify=50,
    )
    scope_a = _mailbox_scope(settings_a)
    scope_b = _mailbox_scope(settings_b)
    assert scope_a != scope_b

    # Seed mailbox A's watermark directly -- simulates a prior successful
    # run that scanned all the way through uid 6, at the SAME
    # FAKE_UID_VALIDITY mailbox B's fake collector will also report.
    advance_xing_scan_progress(
        db,
        uid_validity=FAKE_UID_VALIDITY,
        confirmed_upto_uid=6,
        mailbox_scope=scope_a,
        observed_uid_validity=None,  # seeding a brand-new row
    )

    captured: dict[str, object] = {}

    class _CapturingFakeCollector(_FakeXingCollector):
        def __init__(self, **kwargs):
            captured["scan_from_uid"] = kwargs.get("scan_from_uid")
            captured["expected_uid_validity"] = kwargs.get("expected_uid_validity")
            super().__init__(**kwargs)

    monkeypatch.setattr("app.services.collector_runner.XingEmailCollector", _CapturingFakeCollector)

    result_b = await run_xing(db, settings_b)

    # run_xing must have passed scan_from_uid=None for mailbox B -- it
    # never saw mailbox A's confirmed_upto_uid=6, despite the matching
    # UIDVALIDITY.
    assert captured["scan_from_uid"] is None
    assert captured["expected_uid_validity"] is None

    progress_b = get_xing_scan_progress(db, mailbox_scope=scope_b)
    assert progress_b is not None
    assert progress_b.confirmed_upto_uid == 2  # fresh scan: budget reaches only uid 1-2
    assert result_b["created"] == 2  # uid 1 and 2 both new, since B never saw A's history

    # Mailbox A's own row must be untouched by mailbox B's run.
    progress_a = get_xing_scan_progress(db, mailbox_scope=scope_a)
    assert progress_a is not None
    assert progress_a.confirmed_upto_uid == 6


def test_mailbox_scope_serialization_does_not_collide_across_field_boundaries():
    """Codex gate follow-up (Astra R4A MEDIUM take 2, scope serialization)
    regression: a naive `":"`-joined fingerprint is ambiguous whenever a
    field can itself contain `":"`.  `username="ab:cd", mailbox="ef"` and
    `username="ab", mailbox="cd:ef"` both concatenate to the identical
    `"...:ab:cd:ef"` tail under plain string interpolation -- two
    genuinely different mailbox configurations must never hash to the
    same scope.
    """
    scope_1 = compute_mailbox_scope("imap.example.com", 993, "ab:cd", mailbox="ef")
    scope_2 = compute_mailbox_scope("imap.example.com", 993, "ab", mailbox="cd:ef")

    assert scope_1 != scope_2


def test_mailbox_scope_username_case_is_not_folded():
    """Codex gate follow-up (Astra R4A MEDIUM take 2, scope serialization)
    regression: unlike `imap_host`, `username` must NOT be lowercased --
    an IMAP username's local-part is not guaranteed case-insensitive by
    every server, so folding case here could silently collapse two
    distinct configured mailboxes into one scope.
    """
    scope_upper = compute_mailbox_scope("imap.example.com", 993, "User@Example.com")
    scope_lower = compute_mailbox_scope("imap.example.com", 993, "user@example.com")

    assert scope_upper != scope_lower


@pytest.mark.asyncio
async def test_advance_xing_scan_progress_never_regresses_under_concurrent_stale_write(db):
    """Codex gate follow-up (Astra R4A MEDIUM, concurrency) regression:
    a writer holding a STALE snapshot of the progress row (as would occur
    under real concurrent access -- two workers both read the row before
    either commits) must never be able to regress `confirmed_upto_uid`,
    even though the naive prior implementation branched on exactly that
    stale snapshot rather than the database's live state.

    Exercises `_advance_existing` directly (not
    `advance_xing_scan_progress`, which always re-reads a fresh row and
    would trivially avoid this regression regardless of the underlying
    bug) with a deliberately stale `existing` object, proving the
    monotonic guard lives in the SQL `WHERE` clause itself.
    """
    scope = "concurrency-scope"
    advance_xing_scan_progress(
        db, uid_validity=1, confirmed_upto_uid=5, mailbox_scope=scope, observed_uid_validity=None
    )

    # Simulates "worker B" observing the row while it still read
    # confirmed_upto_uid=5 -- a stale snapshot from before "worker A"
    # (below) advances it further.
    stale_existing = get_xing_scan_progress(db, mailbox_scope=scope)
    assert stale_existing.confirmed_upto_uid == 5

    # Worker A advances first and commits a higher watermark.
    advance_xing_scan_progress(
        db, uid_validity=1, confirmed_upto_uid=10, mailbox_scope=scope, observed_uid_validity=1
    )

    # Worker B now persists its own (smaller, computed from the stale
    # read) value using the snapshot it captured before A's commit.
    _advance_existing(db, stale_existing, uid_validity=1, new=8, observed_uid_validity=1)

    progress = get_xing_scan_progress(db, mailbox_scope=scope)
    assert progress.confirmed_upto_uid == 10  # must NOT regress to 8


@pytest.mark.asyncio
async def test_advance_xing_scan_progress_stale_writer_can_still_advance_further(db):
    """Companion to the regression above: a stale-snapshot writer whose
    computed value is still GENUINELY ahead of the current stored value
    must still be able to advance it -- the SQL guard must compare
    against live state, not simply reject every write from a stale
    snapshot.
    """
    scope = "concurrency-scope-advance"
    advance_xing_scan_progress(
        db, uid_validity=1, confirmed_upto_uid=5, mailbox_scope=scope, observed_uid_validity=None
    )

    stale_existing = get_xing_scan_progress(db, mailbox_scope=scope)

    _advance_existing(db, stale_existing, uid_validity=1, new=12, observed_uid_validity=1)

    progress = get_xing_scan_progress(db, mailbox_scope=scope)
    assert progress.confirmed_upto_uid == 12


@pytest.mark.asyncio
async def test_stale_epoch_1_writer_never_clobbers_committed_epoch_2(db):
    """Codex gate follow-up (Astra R4A MEDIUM take 3, UIDVALIDITY CAS)
    regression: a prior fix made the epoch-reset condition "stored
    uid_validity differs from the NEW value" -- too broad, because it
    ALSO matches a row that has already moved to a newer epoch than the
    writer knows about.

    Scenario: worker B takes a snapshot while the row is still at epoch
    1. Before B writes anything, worker A observes that same epoch-1
    row, decides the mailbox was recreated, and CAS-transitions it to
    epoch 2 with watermark 10 -- and commits. B, still only aware of
    epoch 1, now tries to persist its own (stale) belief: epoch 1,
    watermark 8. The old broad condition would see "stored (2) != B's
    target (1)" and let B's UPDATE overwrite A's committed epoch-2 row
    right back down to epoch 1 -- resurrecting a stale epoch and losing
    A's already-confirmed progress. The fixed CAS in `_advance_existing`
    only matches when the row is STILL at `observed_uid_validity` (the
    epoch B actually read, 1) -- since the live row is now epoch 2, B's
    write must no-op, and the final state must remain exactly what A
    committed: epoch 2, watermark 10.
    """
    scope = "uidvalidity-cas-scope"
    # Seed the row at epoch 1 -- what both A and B will have observed.
    advance_xing_scan_progress(
        db, uid_validity=1, confirmed_upto_uid=3, mailbox_scope=scope, observed_uid_validity=None
    )

    # Worker B's stale snapshot: taken while the row is still epoch 1.
    stale_existing = get_xing_scan_progress(db, mailbox_scope=scope)
    assert stale_existing.uid_validity == 1

    # Worker A observes epoch 1 too, but is the one that actually acts
    # first: mailbox was recreated, CAS-transitions epoch 1 -> 2 with a
    # fresh watermark, and commits.
    advance_xing_scan_progress(
        db,
        uid_validity=2,
        confirmed_upto_uid=10,
        mailbox_scope=scope,
        observed_uid_validity=1,
    )
    committed = get_xing_scan_progress(db, mailbox_scope=scope)
    assert committed.uid_validity == 2
    assert committed.confirmed_upto_uid == 10

    # Worker B, unaware of A's transition, now persists its own stale
    # belief: still epoch 1, watermark 8. This must no-op, not restore
    # epoch 1 or regress the watermark.
    _advance_existing(db, stale_existing, uid_validity=1, new=8, observed_uid_validity=1)

    final = get_xing_scan_progress(db, mailbox_scope=scope)
    assert final.uid_validity == 2
    assert final.confirmed_upto_uid == 10


@pytest.mark.asyncio
async def test_run_xing_uses_early_captured_epoch_not_late_reread_after_commits(db, monkeypatch):
    """Codex gate follow-up (Astra R4A MEDIUM take 4, early scalar
    capture) regression.

    `run_xing` must capture `observed_uid_validity` as a plain scalar
    IMMEDIATELY after `get_xing_scan_progress`, before any of this run's
    own persistence commits -- not read `scan_progress.uid_validity`
    again afterward. Under the default `expire_on_commit=True` session
    behavior, this run's OWN commits (job persistence,
    `mark_message_processed`) expire `scan_progress`'s ORM attributes;
    a LATE `scan_progress.uid_validity` access re-SELECTs the row's
    CURRENT state -- which, if another writer changed the row mid-run,
    is no longer the epoch this run actually made its decisions against.

    Simulates that concurrent change via a genuinely SEPARATE
    session/commit against the same SQLite file (not a Python-side
    mutation `run_xing`'s own session would already see) while the fake
    collector's `fetch_message_batches` runs -- i.e. strictly BETWEEN
    `run_xing`'s initial `get_xing_scan_progress` read and its own later
    commits.

    Before the fix: the late re-read would observe the OTHER worker's
    epoch 2 and treat this run's own epoch-1 write as an "epoch
    transition" FROM 2, whose CAS then matches the (also epoch-2) row
    and incorrectly overwrites it back down to epoch 1 -- clobbering the
    other worker's already-committed epoch 2 / watermark 10.

    After the fix: `observed_uid_validity` stays pinned at the TRUE
    baseline (epoch 1, captured before any commit), so this run's own
    write is a same-epoch advance whose CAS requires the row to still be
    epoch 1 -- it no longer is, so the write correctly no-ops and the
    other worker's epoch 2 / watermark 10 survives untouched.
    """
    settings = _settings()
    scope = _mailbox_scope(settings)

    # Seed the row at epoch 1 -- what run_xing's own get_xing_scan_progress
    # will observe at the very top of the function.
    advance_xing_scan_progress(
        db, uid_validity=1, confirmed_upto_uid=3, mailbox_scope=scope, observed_uid_validity=None
    )

    other_session_factory = sessionmaker(bind=db.get_bind())

    class _FakeCollectorRowChangesMidRun:
        def __init__(self, **kwargs) -> None:
            self.skipped_invalid_count = 0
            self.deadline_exceeded = False
            self._is_message_processed = kwargs["is_message_processed"]
            self.uid_validity: int | None = None
            self.confirmed_uids: list[int] = []
            self.candidate_uids: list[int] = []

        async def fetch_message_batches(self, since=None) -> list[XingEmailBatch]:
            # This worker's own live IMAP observation: still epoch 1 --
            # it has no way to know another writer is about to move the
            # row to epoch 2 while this call is in flight.
            self.uid_validity = 1

            # Simulates a genuinely concurrent writer moving the row to a
            # newer epoch mid-run, via a SEPARATE session/commit -- this
            # is what makes `db`'s own cached `scan_progress` object
            # SQLAlchemy-expired-and-stale, not merely logically stale.
            other_session = other_session_factory()
            try:
                advance_xing_scan_progress(
                    other_session,
                    uid_validity=2,
                    confirmed_upto_uid=10,
                    mailbox_scope=scope,
                    observed_uid_validity=1,
                )
            finally:
                other_session.close()

            uid = 7
            self.candidate_uids = [uid]
            message_id = f"<uid-{uid}@mail.xing.com>"
            job = Job(
                source="xing",
                title="Job 7",
                company="Company 7",
                url="https://example.com/jobs/7",
                description="",
                skills=[],
            )
            return [XingEmailBatch(message_id=message_id, jobs=(job,), uid=uid)]

    monkeypatch.setattr(
        "app.services.collector_runner.XingEmailCollector", _FakeCollectorRowChangesMidRun
    )
    monkeypatch.setattr(
        "app.services.collector_runner.JobScorer",
        lambda profile_skills: FakeJobScorer(profile_skills),
    )
    monkeypatch.setattr("app.services.collector_runner.TelegramNotifier", _NoOpNotifier)
    monkeypatch.setattr(
        "app.services.collector_runner.CompanyResearchService", _NoOpResearchService
    )

    result = await run_xing(db, settings)

    assert result["created"] == 1  # uid 7's job still persisted regardless

    # The row must remain exactly what the OTHER worker committed -- this
    # run's own watermark write must have no-opped rather than clobbering
    # it back down to epoch 1.
    progress = get_xing_scan_progress(db, mailbox_scope=scope)
    assert progress.uid_validity == 2
    assert progress.confirmed_upto_uid == 10
