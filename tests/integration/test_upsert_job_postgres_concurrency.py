"""HARD-007 (Codex master review): proves `app.db.repositories.upsert_job`'s
fix for the concurrent-new-fingerprint race against REAL PostgreSQL, not
just the SQLite reproduction
(tests/test_repository.py::test_concurrent_insert_of_the_same_new_job_converges_on_one_row),
per the explicit instruction that a SQLite-only concurrency claim is not
sufficient production evidence.

**The scenario.** Two independent Sessions/connections race to
`upsert_job()` the exact same brand-new (never-before-seen) fingerprint.
A single outer `threading.Barrier` alone was tried first and found
UNRELIABLE against real PostgreSQL round-trip timing: on some runs one
thread's full SELECT-INSERT-COMMIT completed before the other thread's
own SELECT even ran, so the second thread correctly saw the first's
already-committed row and took the UPDATE branch instead of racing at
all -- a real result, just not the race this module exists to prove. A
SECOND barrier is therefore injected at the exact
`get_job_by_fingerprint` read point (via monkeypatching the bare
module-level name `upsert_job` itself calls, not by touching
`upsert_job`'s own code) so both threads are GUARANTEED to have already
observed `existing is None` before either is allowed to proceed to its
INSERT -- deterministically reproducing the true race every run.

**What this proves (HARD-007 fix, real PostgreSQL):**
- `JobRecord.fingerprint`'s DB-level UNIQUE constraint
  (`uq_jobs_fingerprint`) really does hold under genuine PostgreSQL
  concurrency -- exactly one row is ever created, never two.
- The losing call no longer raises `IntegrityError` out of `upsert_job`
  at all -- it converges on the winner's row (`created=False`) with the
  normal existing-row update semantics applied, exactly like a
  sequential second call would.
- The Session `upsert_job` was called with remains fully usable
  immediately afterward with NO extra caller-side `rollback()` required
  -- PostgreSQL, unlike SQLite, poisons the entire transaction after any
  error until an explicit ROLLBACK, and `upsert_job` now performs that
  rollback itself as part of the race-recovery path (see that
  function's own docstring).
- That a subsequent, unrelated, legitimate `upsert_job()` call on the
  SAME (former loser) Session succeeds normally right after.

**Local execution:** skipped automatically unless `TEST_POSTGRES_URL` is
set. Run `alembic upgrade head` against that same database first --
this module has no `Base.metadata.create_all` fallback, matching every
other PostgreSQL integration test in this directory.

**CI:** wired into `.github/workflows/ci.yml`'s existing
`scheduler-postgres` job.
"""

import json
import os
import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.candidate_profile_repository import (
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
)
from app.db.models import CandidateProfileRecord, JobRecord, JobReferenceTokenRecord, UserProfile
from app.db.repositories import _fingerprint, _is_fingerprint_unique_violation, upsert_job
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.models.job import Job, JobScore
from app.services.collector_runner import score_and_persist

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set -- PostgreSQL integration test skipped locally "
    "(GitHub Actions' scheduler-postgres job sets it).",
)

FINGERPRINT_MARKER = "hard-007-pg-concurrent-insert"


@pytest.fixture()
def pg_session_factory():
    engine = create_engine(TEST_POSTGRES_URL, pool_pre_ping=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    cleanup = session_factory()
    try:
        # NOTE: filter on `title` (or `url`), NOT `fingerprint` --
        # `fingerprint` is a SHA-256 hex digest and can never contain the
        # literal marker substring. Filtering on the hash was a bug
        # caught while developing this test: a leftover row from a prior
        # run was never actually cleaned up, silently turning the
        # "brand-new fingerprint" race into a "both see the same
        # already-existing row" non-race that always passed both threads
        # through the UPDATE branch -- a false negative, not a false
        # positive, but still worth flagging as exactly the kind of
        # test-quality issue this hardening pass's own Phase 18 looks for.
        cleanup.execute(delete(JobRecord).where(JobRecord.title.like(f"%{FINGERPRINT_MARKER}%")))
        cleanup.commit()
    finally:
        cleanup.close()
    yield session_factory
    engine.dispose()


def _make_job(uid: int) -> Job:
    return Job(
        source="bundesagentur",
        title=f"Race Engineer {FINGERPRINT_MARKER}",
        company="RaceCo",
        location="Berlin",
        url=f"https://example.com/jobs/{FINGERPRINT_MARKER}-{uid}",
    )


def _synchronized_get_job_by_fingerprint(read_barrier: threading.Barrier):
    """Wraps the REAL `get_job_by_fingerprint` with a second barrier so
    both racing threads are guaranteed to have already completed their
    read (and observed `existing is None`) before either is released to
    proceed to `upsert_job`'s INSERT branch -- makes the true race
    deterministic instead of depending on real PostgreSQL round-trip
    timing (see module docstring).
    """
    from app.db.repositories import get_job_by_fingerprint as real_get_job_by_fingerprint

    thread_local = threading.local()

    def _wrapped(db, job):
        result = real_get_job_by_fingerprint(db, job)
        # HARD-007 fix note: upsert_job's own race-recovery path now
        # calls get_job_by_fingerprint a SECOND time (the post-rollback
        # re-read to find the winner) -- only on the LOSING thread, and
        # only after the INSERT has already failed. That second call
        # must never touch this 2-party barrier again (only one thread
        # would ever reach it a second time, so a second .wait() call
        # would time out waiting for a partner that will never arrive).
        # Only each thread's FIRST call -- the actual pre-INSERT
        # existence check this barrier exists to synchronize -- waits.
        if not getattr(thread_local, "used_barrier", False):
            thread_local.used_barrier = True
            read_barrier.wait(timeout=5)
        return result

    return _wrapped


class TestRealConcurrentNewJobInsert:
    def test_only_one_row_created_loser_converges_without_raising(
        self, pg_session_factory, monkeypatch
    ):
        same_new_job = _make_job(1)
        score = JobScore(score=80, recommendation="APPLY")
        fingerprint = _fingerprint(same_new_job)

        read_barrier = threading.Barrier(2)
        monkeypatch.setattr(
            "app.db.repositories.get_job_by_fingerprint",
            _synchronized_get_job_by_fingerprint(read_barrier),
        )

        results: dict[int, object] = {}

        def worker(index: int) -> None:
            session = pg_session_factory()
            try:
                results[index] = upsert_job(session, same_new_job, score)
            except BaseException as exc:  # noqa: BLE001
                results[index] = exc
            finally:
                session.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        outcomes = list(results.values())
        successes = [o for o in outcomes if isinstance(o, tuple)]
        failures = [o for o in outcomes if isinstance(o, BaseException)]

        # HARD-007 fix: no unhandled IntegrityError escapes either thread
        # -- both logical calls succeed and converge on the SAME row.
        assert not failures, f"upsert_job raised for the losing call: {failures}"
        assert len(successes) == 2
        created_flags = sorted(created for _record, created in successes)
        assert created_flags == [False, True]
        record_ids = {record.id for record, _created in successes}
        assert len(record_ids) == 1

        verify = pg_session_factory()
        try:
            rows = verify.scalars(
                select(JobRecord).where(JobRecord.fingerprint == fingerprint)
            ).all()
            assert len(rows) == 1
        finally:
            verify.close()

    def test_losing_session_remains_usable_immediately_with_no_caller_side_rollback(
        self, pg_session_factory, monkeypatch
    ):
        """PostgreSQL-specific behavior this guards against (does NOT
        reproduce on SQLite): after ANY statement fails inside a
        transaction, PostgreSQL refuses every further statement in that
        same transaction ("current transaction is aborted, commands
        ignored until end of transaction block") until an explicit
        ROLLBACK. `upsert_job` now performs that rollback itself, as part
        of its own race-recovery path (see its docstring) -- this proves
        the CALLER needs to do nothing extra: the Session is immediately
        usable the instant `upsert_job` returns normally, for both a
        trivial read and a genuinely new, unrelated `upsert_job` call.
        """
        same_new_job = _make_job(2)
        score = JobScore(score=80, recommendation="APPLY")

        read_barrier = threading.Barrier(2)
        monkeypatch.setattr(
            "app.db.repositories.get_job_by_fingerprint",
            _synchronized_get_job_by_fingerprint(read_barrier),
        )

        sessions: dict[int, object] = {}
        results: dict[int, object] = {}

        def worker(index: int) -> None:
            session = pg_session_factory()
            sessions[index] = session
            try:
                results[index] = upsert_job(session, same_new_job, score)
            except BaseException as exc:  # noqa: BLE001
                results[index] = exc
            # Deliberately NOT closing the session here -- the main
            # thread needs to inspect the loser's session state below,
            # from outside this thread, only after this thread has
            # already finished touching it.

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert all(isinstance(r, tuple) for r in results.values()), (
            f"upsert_job raised instead of converging: {results}"
        )
        winner_index = next(i for i, r in results.items() if r[1] is True)
        loser_index = next(i for i, r in results.items() if r[1] is False)
        loser_session = sessions[loser_index]

        # Restore the REAL (unwrapped) get_job_by_fingerprint before the
        # follow-up "subsequent normal work succeeds" check below -- the
        # 2-party read_barrier only has meaning for the deliberately
        # racing pair above; reusing the still-patched version for a
        # solo call would block for the full 5s timeout waiting for a
        # second party that will never arrive, then raise
        # BrokenBarrierError (confirmed empirically while developing
        # this test -- not a hypothetical concern).
        monkeypatch.undo()

        try:
            # No caller-side rollback anywhere in this test -- upsert_job
            # already performed it internally. A trivial, unrelated read
            # must succeed immediately.
            loser_session.execute(select(func.count()).select_from(JobRecord))

            unrelated_job = _make_job(3)
            record, created = upsert_job(loser_session, unrelated_job, score)
            assert created is True
        finally:
            sessions[winner_index].close()
            loser_session.close()


class TestFingerprintViolationDetectionAgainstRealPostgresDiagnostics:
    """HARD-007-RR1 (Codex targeted re-review): `upsert_job`'s
    race-recovery must key off the SPECIFIC `uq_jobs_fingerprint`
    constraint, using the real driver's structured diagnostics
    (`IntegrityError.orig.diag.constraint_name`) rather than a broad
    "unique" substring match. The SQLite unit tests
    (tests/test_repository.py) exercise `_is_fingerprint_unique_violation`
    against a hand-built fake object that MIMICS psycopg's `.diag`
    interface -- these two tests instead prove the assumption that mimic
    rests on: that a REAL psycopg `IntegrityError` raised by REAL
    PostgreSQL actually carries a populated, correctly-discriminating
    `.diag.constraint_name` for both a genuine `uq_jobs_fingerprint`
    violation and an unrelated one (`uq_job_reference_tokens_job_token`).
    """

    def test_real_fingerprint_unique_violation_is_detected(self, pg_session_factory):
        job = _make_job(10)
        fingerprint = _fingerprint(job)
        now = datetime.now(UTC)

        session = pg_session_factory()
        try:
            session.add(
                JobRecord(
                    fingerprint=fingerprint,
                    source="bundesagentur",
                    title=job.title,
                    company=job.company,
                    location=job.location,
                    url=str(job.url),
                    description="",
                    status="NEW",
                    first_seen_at=now,
                    last_seen_at=now,
                    score=80,
                    recommendation="APPLY",
                )
            )
            session.commit()

            # A second row with the SAME fingerprint -- a real
            # PostgreSQL-raised uq_jobs_fingerprint violation, not a
            # fabricated stand-in.
            session.add(
                JobRecord(
                    fingerprint=fingerprint,
                    source="bundesagentur",
                    title=job.title,
                    company=job.company,
                    location=job.location,
                    url=str(job.url),
                    description="",
                    status="NEW",
                    first_seen_at=now,
                    last_seen_at=now,
                    score=80,
                    recommendation="APPLY",
                )
            )
            with pytest.raises(IntegrityError) as excinfo:
                session.commit()
            session.rollback()

            assert _is_fingerprint_unique_violation(excinfo.value) is True
        finally:
            session.close()

    def test_real_unrelated_unique_violation_is_not_detected(self, pg_session_factory):
        job = _make_job(11)
        fingerprint = _fingerprint(job)
        now = datetime.now(UTC)

        session = pg_session_factory()
        try:
            record = JobRecord(
                fingerprint=fingerprint,
                source="bundesagentur",
                title=job.title,
                company=job.company,
                location=job.location,
                url=str(job.url),
                description="",
                status="NEW",
                first_seen_at=now,
                last_seen_at=now,
                score=80,
                recommendation="APPLY",
            )
            session.add(record)
            session.flush()

            session.add(JobReferenceTokenRecord(job_id=record.id, token="DUPTOKEN"))
            session.flush()
            # A real, unrelated uq_job_reference_tokens_job_token
            # violation -- must NOT be mistaken for a fingerprint race.
            session.add(JobReferenceTokenRecord(job_id=record.id, token="DUPTOKEN"))
            with pytest.raises(IntegrityError) as excinfo:
                session.flush()
            session.rollback()

            assert _is_fingerprint_unique_violation(excinfo.value) is False
        finally:
            session.close()


class TestPostingTypeConcurrencyReconciliation:
    """S10-RR-001 (Codex Stage 10 re-review, BLOCKING): proves
    `score_and_persist`'s CAS-based posting_type reconciliation
    (`app.db.repositories.update_job_score_if_posting_type_unchanged`)
    against REAL PostgreSQL -- two independent Sessions race
    `score_and_persist()` for the SAME brand-new fingerprint, one
    submitting an UNTYPED Job, the other a SELBSTAENDIGKEIT-typed one
    with no FREELANCE preference (so it must be excluded).

    Uses the same two-barrier technique as
    `TestRealConcurrentNewJobInsert` above, but patched at
    `app.services.collector_runner`'s own `get_job_by_fingerprint`
    reference (`score_and_persist`'s OWN first-line read, not
    `upsert_job`'s separate internal one) so BOTH threads are guaranteed
    to have observed `existing is None` before either proceeds --
    forcing the genuine INSERT collision inside `upsert_job`'s own
    HARD-007 race-recovery path, which is exactly where S10-001's
    original bug (a losing writer's stale precomputed recommendation
    silently overwriting a winner's correct one) lived. `upsert_job`'s
    OWN internal reads are left completely real/unpatched -- this proves
    the fix WITHOUT touching or weakening HARD-007 itself.
    """

    @pytest.fixture(autouse=True)
    def _reset_candidate_profile_singleton(self, pg_session_factory):
        # A clean, default (no employment_types preference) Stage 6A
        # Candidate Profile before each test here -- the SELBSTAENDIGKEIT
        # scenario below depends on FREELANCE NOT being in the
        # candidate's preferences, matching the SQLite unit tests' own
        # default assumption
        # (tests/test_collector_runner_posting_classification.py).
        session = pg_session_factory()
        try:
            session.execute(delete(CandidateProfileRecord).where(CandidateProfileRecord.id == 1))
            session.commit()
        finally:
            session.close()

    def test_concurrent_typed_and_untyped_score_and_persist_never_pairs_apply_with_excluded_type(
        self, pg_session_factory, monkeypatch
    ):
        marker = f"{FINGERPRINT_MARKER}-posting-type-cas"
        job_a = Job(
            source="bundesagentur",
            title=f"Backend Engineer {marker}",
            company="RaceCo",
            location="Berlin",
            url=f"https://example.com/jobs/{marker}",
            must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
            posting_type=None,
        )
        job_b = Job(
            source="bundesagentur",
            title=f"Backend Engineer {marker}",
            company="RaceCo",
            location="Berlin",
            url=f"https://example.com/jobs/{marker}",
            posting_type="SELBSTAENDIGKEIT",
        )
        profile = UserProfile(
            name="default", skills_json=json.dumps(["python", "fastapi", "sqlalchemy"])
        )

        read_barrier = threading.Barrier(2)
        monkeypatch.setattr(
            "app.services.collector_runner.get_job_by_fingerprint",
            _synchronized_get_job_by_fingerprint(read_barrier),
        )

        results: dict[int, object] = {}

        def worker(index: int, job: Job) -> None:
            session = pg_session_factory()
            try:
                results[index] = score_and_persist(session, profile, job)
            except BaseException as exc:  # noqa: BLE001
                results[index] = exc
            finally:
                session.close()

        threads = [
            threading.Thread(target=worker, args=(0, job_a)),
            threading.Thread(target=worker, args=(1, job_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        outcomes = list(results.values())
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        assert not failures, f"score_and_persist raised: {failures}"
        assert len(outcomes) == 2

        verify = pg_session_factory()
        try:
            rows = verify.scalars(
                select(JobRecord).where(JobRecord.fingerprint == _fingerprint(job_b))
            ).all()
            assert len(rows) == 1, "exactly one row must exist -- HARD-007 fingerprint uniqueness"
            row = rows[0]
            # The reported bug, made explicit: an excluded posting type
            # must never be paired with recommendation=APPLY.
            assert not (row.posting_type == "SELBSTAENDIGKEIT" and row.recommendation == "APPLY")
            # job_b's explicit SELBSTAENDIGKEIT always wins posting_type
            # (job_a's None never overwrites it -- preserve-on-omit in
            # _apply_job_update_fields), so the row must always converge
            # to excluded/SKIP regardless of which thread won the
            # physical INSERT race.
            assert row.posting_type == "SELBSTAENDIGKEIT"
            assert row.recommendation == "SKIP"
            assert row.score == 0
        finally:
            verify.close()

    def test_blank_posting_type_cannot_erase_a_stored_selbstaendigkeit_real_postgres(
        self, pg_session_factory
    ):
        marker = f"{FINGERPRINT_MARKER}-blank-erase"
        profile = UserProfile(
            name="default", skills_json=json.dumps(["python", "fastapi", "sqlalchemy"])
        )
        original = Job(
            source="bundesagentur",
            title=f"Python Advanced {marker}",
            company="alfatraining Bildungszentrum GmbH",
            url=f"https://example.com/jobs/{marker}",
            posting_type="SELBSTAENDIGKEIT",
        )

        session1 = pg_session_factory()
        try:
            record1, result1, created1 = score_and_persist(session1, profile, original)
            assert created1 is True
            assert record1.posting_type == "SELBSTAENDIGKEIT"
            assert result1.recommendation == "SKIP"
        finally:
            session1.close()

        # A raw "" from an upstream API response (S10-RR-001 normalizes
        # this to None at the Job model boundary) for the SAME
        # fingerprint, on a SEPARATE session/connection.
        blank_resubmit = Job(
            source="bundesagentur",
            title=f"Python Advanced {marker}",
            company="alfatraining Bildungszentrum GmbH",
            url=f"https://example.com/jobs/{marker}",
            posting_type="",
        )
        assert blank_resubmit.posting_type is None

        session2 = pg_session_factory()
        try:
            record2, result2, created2 = score_and_persist(session2, profile, blank_resubmit)
            assert created2 is False
            assert record2.id == record1.id
            assert record2.posting_type == "SELBSTAENDIGKEIT"
            assert result2.recommendation == "SKIP"
        finally:
            session2.close()


class TestS10RR002StaleDirtyScoreFieldsNotReplayed:
    """S10-RR-002 (Codex Stage 10 re-re-review, BLOCKING): proves that a
    successful CAS reconciliation inside `score_and_persist` does NOT
    leave stale score/recommendation/data_confidence values pending on
    the Session, ready to be silently replayed (with no posting_type
    guard at all) by a LATER, wholly unrelated commit on that SAME
    Session against REAL PostgreSQL.

    **The scenario (sequential, not a true concurrent race -- see
    below):**
    1. Session A reconciles a SELBSTAENDIGKEIT posting via CAS. FREELANCE
       is granted up front so this legitimately converges on APPLY --
       needed so a stale replay of A's OLD values would reproduce the
       exact literal bad pairing this test guards against
       (SELBSTAENDIGKEIT + APPLY), not some other mismatched pairing.
    2. Session B revokes the FREELANCE preference and re-scores the SAME
       posting -- now legitimately SKIP. B's own write is a plain,
       non-reconciling update (its own observed posting_type already
       matches what it computes against), so B is unaffected by this bug
       either way.
    3. Session A performs another, LATER, UNRELATED `commit()`.

    Pre-fix, `record_a` (from step 1) still carries dirty score=X/
    recommendation=APPLY/data_confidence=Y from step 1's manual
    reassignment (see `score_and_persist`'s S10-RR-002 docstring section)
    -- step 3's commit flushes those via a plain `UPDATE ... WHERE id =
    :id`, no posting_type predicate at all, silently resurrecting APPLY
    over B's legitimate SKIP. Post-fix, `db.refresh()` left `record_a`
    clean after step 1, so step 3 is a true no-op.

    Deliberately uses the SAME monkeypatched-read-point technique as
    `TestPostingTypeConcurrencyReconciliation` above (not real threading)
    -- this scenario is fundamentally SEQUENTIAL (A reconciles, THEN B
    acts, THEN A commits again), so a real race isn't what's being
    proven; the monkeypatch only exists to force A into the
    CAS-reconciliation branch deterministically, exactly like a genuine
    concurrent writer would.

    **Critical test-methodology note:** this test deliberately never
    touches any attribute of `record_a` between step 1 and step 3.
    Reading an EXPIRED attribute (e.g. `record_a.posting_type`, never
    manually re-set) triggers SQLAlchemy's own lazy-refresh-on-access,
    which would reload the row fresh from the database and incidentally
    launder away the exact dirty state this test exists to catch --
    discovered while developing the companion SQLite unit test
    (tests/test_collector_runner_posting_classification.py), which
    originally asserted `record not in db.dirty` AFTER already asserting
    `record.posting_type == ...` and so never actually caught the
    pre-fix bug until the assertion order was corrected.
    """

    @pytest.fixture(autouse=True)
    def _reset_candidate_profile_singleton(self, pg_session_factory):
        session = pg_session_factory()
        try:
            session.execute(delete(CandidateProfileRecord).where(CandidateProfileRecord.id == 1))
            session.commit()
        finally:
            session.close()

    def test_later_unrelated_commit_does_not_resurrect_a_stale_apply_over_a_legitimate_skip(
        self, pg_session_factory, monkeypatch
    ):
        marker = f"{FINGERPRINT_MARKER}-rr002-stale-replay"

        setup_session = pg_session_factory()
        try:
            get_or_create_candidate_profile(setup_session)
            apply_candidate_profile_patch(
                setup_session,
                CandidateProfilePatchRequest(
                    expected_profile_version=1,
                    job_preferences={"employment_types": ["FREELANCE"]},
                ),
            )
        finally:
            setup_session.close()

        profile = UserProfile(
            name="default", skills_json=json.dumps(["python", "fastapi", "sqlalchemy"])
        )
        # A real (non-empty) description is required for both -- with no
        # description, data_confidence is too low to ever reach APPLY
        # (it lands on NEEDS_ENRICHMENT instead), which would still prove
        # a stale replay corrupted B's row but not the EXACT literal
        # SELBSTAENDIGKEIT+APPLY pairing this test targets.
        rich_description = "We build REST APIs with Python, FastAPI and SQLAlchemy. " * 15
        job_a = Job(
            source="bundesagentur",
            title=f"Backend Engineer {marker}",
            company="RaceCo",
            url=f"https://example.com/jobs/{marker}",
            description=rich_description,
            must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
            posting_type=None,
        )
        job_seed = Job(
            source="bundesagentur",
            title=f"Backend Engineer {marker}",
            company="RaceCo",
            url=f"https://example.com/jobs/{marker}",
            description=rich_description,
            must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
            posting_type="SELBSTAENDIGKEIT",
        )

        from app.db.repositories import get_job_by_fingerprint as real_get_job_by_fingerprint

        call_count = {"n": 0}

        def interleaving(db_, job_):
            call_count["n"] += 1
            result = real_get_job_by_fingerprint(db_, job_)
            if call_count["n"] == 1:
                monkeypatch.setattr(
                    "app.services.collector_runner.get_job_by_fingerprint",
                    real_get_job_by_fingerprint,
                )
                meanwhile = pg_session_factory()
                try:
                    score_and_persist(meanwhile, profile, job_seed)
                finally:
                    meanwhile.close()
                monkeypatch.setattr(
                    "app.services.collector_runner.get_job_by_fingerprint", interleaving
                )
            return result

        monkeypatch.setattr("app.services.collector_runner.get_job_by_fingerprint", interleaving)

        session_a = pg_session_factory()
        try:
            # --- Step 1: Session A reconciles via CAS (legitimately to
            # APPLY, freelance granted + good skills). No assertions on
            # record_a's attributes here -- see the class docstring.
            record_a, _result_a, _created_a = score_and_persist(session_a, profile, job_a)

            # --- Step 2: Session B revokes the freelance preference and
            # re-scores the SAME posting -- now legitimately SKIP.
            session_b = pg_session_factory()
            try:
                profile_record_b = get_or_create_candidate_profile(session_b)
                apply_candidate_profile_patch(
                    session_b,
                    CandidateProfilePatchRequest(
                        expected_profile_version=profile_record_b.profile_version,
                        job_preferences={"employment_types": []},
                    ),
                )
                job_b = Job(
                    source="bundesagentur",
                    title=f"Backend Engineer {marker}",
                    company="RaceCo",
                    url=f"https://example.com/jobs/{marker}",
                    must_have_skills=["Python", "FastAPI", "SQLAlchemy"],
                    posting_type="SELBSTAENDIGKEIT",
                )
                record_b, result_b, _created_b = score_and_persist(session_b, profile, job_b)
                assert record_b.posting_type == "SELBSTAENDIGKEIT"
                assert result_b.recommendation == "SKIP"
            finally:
                session_b.close()

            # --- Step 3: Session A performs another, LATER, UNRELATED
            # commit. Pre-fix, this flushes record_a's stale dirty APPLY
            # values with no posting_type guard, resurrecting APPLY over
            # B's legitimate SKIP.
            session_a.commit()
        finally:
            session_a.close()

        verify = pg_session_factory()
        try:
            row = verify.scalar(
                select(JobRecord).where(JobRecord.fingerprint == _fingerprint(job_seed))
            )
            assert row is not None
            # The literal bad pairing this whole Stage 10 effort exists
            # to prevent must never reappear via this mechanism either.
            assert not (row.posting_type == "SELBSTAENDIGKEIT" and row.recommendation == "APPLY")
            # The precise, positive claim: Session A's later commit
            # preserved Session B's consistent values exactly.
            assert row.posting_type == "SELBSTAENDIGKEIT"
            assert row.recommendation == "SKIP"
        finally:
            verify.close()
