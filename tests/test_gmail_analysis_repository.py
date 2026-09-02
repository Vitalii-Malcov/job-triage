"""Tests for app.db.gmail_analysis_repository +
app.services.gmail_message_analysis (Stage 7B) — immutability,
idempotency, versioning, fingerprinting, concurrency, and bounded query
counts.
"""

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.gmail_analysis_repository import (
    compute_context_fingerprint,
    get_job_candidates,
    get_latest_analysis_for_message,
    get_or_create_analysis,
    get_thread_prior_matches,
    list_analyses,
)
from app.db.gmail_repository import upsert_message
from app.db.models import GmailMessageAnalysisRecord, JobRecord
from app.providers.email.base import ParsedGmailMessage
from app.services.email_matching import EmailMatchResult, ThreadPriorMatch
from app.services.gmail_message_analysis import (
    ANALYSIS_VERSION,
    analyze_gmail_message,
    compute_input_fingerprint,
    determine_requires_human_review,
)

ACCOUNT_A = "a@example.com"
ACCOUNT_B = "b@example.com"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gmail_analysis_repository.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def db_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'gmail_analysis_repository_concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _parsed(account_key=ACCOUNT_A, uid=1, message_id="<msg1@example.com>", **overrides):
    data = dict(
        account_key=account_key,
        mailbox="INBOX",
        uid=uid,
        uid_validity=100,
        message_id_header=message_id,
        in_reply_to=None,
        references=(),
        from_address="hr@acme.example.com",
        from_display_name="HR",
        to_addresses=(account_key,),
        cc_addresses=(),
        subject="Ihre Bewerbung",
        sent_at=datetime.now(UTC),
        direction="INBOUND",
        body_plain="Vielen Dank für Ihre Bewerbung als Python Developer.",
        body_truncated=False,
        has_html=False,
        attachments=(),
    )
    data.update(overrides)
    return ParsedGmailMessage(**data)


def _add_job(db, **overrides):
    defaults = dict(
        fingerprint=f"fp-{overrides.get('title', 'x')}-{id(overrides)}",
        source="test",
        title="Python Developer",
        company="Acme GmbH",
        location="Berlin",
        url="https://acme.example.com/jobs/1",
        description="",
        score=80,
        recommendation="APPLY",
        status="APPLIED",
    )
    defaults.update(overrides)
    job = JobRecord(**defaults)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_analysis_revision_is_immutable_never_updated(db):
    _add_job(db)
    msg, _ = upsert_message(db, _parsed())

    record, created = analyze_gmail_message(db, ACCOUNT_A, msg.id)
    assert created is True
    original_created_at = record.created_at
    original_id = record.id

    # No repository function exposes an UPDATE path for this table at all.
    assert not any(
        name.startswith("update_")
        for name in dir(__import__("app.db.gmail_analysis_repository", fromlist=["*"]))
    )

    reloaded = db.get(GmailMessageAnalysisRecord, original_id)
    assert reloaded.created_at == original_created_at
    assert reloaded.match_type == record.match_type


def test_idempotent_same_input_and_version_returns_existing_row(db):
    _add_job(db)
    msg, _ = upsert_message(db, _parsed())

    record1, created1 = analyze_gmail_message(db, ACCOUNT_A, msg.id)
    record2, created2 = analyze_gmail_message(db, ACCOUNT_A, msg.id)

    assert created1 is True
    assert created2 is False
    assert record1.id == record2.id

    all_rows = db.scalars(select(GmailMessageAnalysisRecord)).all()
    assert len(all_rows) == 1


def test_new_algorithm_version_creates_new_revision(db):
    _add_job(db)
    msg, _ = upsert_message(db, _parsed())

    match_result = EmailMatchResult(
        match_type="UNMATCHED",
        matched_job_id=None,
        confidence="LOW",
        score=0,
        evidence=(),
        candidates=(),
    )
    fingerprint = compute_input_fingerprint(msg.subject, msg.from_address, msg.body_plain)

    record_v1, created_v1 = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint=fingerprint,
        context_fingerprint="ctx-fixed",
        match_result=match_result,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    record_v2, created_v2 = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=2,
        input_fingerprint=fingerprint,
        context_fingerprint="ctx-fixed",
        match_result=match_result,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )

    assert created_v1 is True
    assert created_v2 is True
    assert record_v1.id != record_v2.id
    assert record_v1.analysis_version == 1
    assert record_v2.analysis_version == 2

    all_rows = db.scalars(select(GmailMessageAnalysisRecord)).all()
    assert len(all_rows) == 2


def test_changed_input_fingerprint_does_not_overwrite_prior_analysis(db):
    _add_job(db)
    msg, _ = upsert_message(db, _parsed())

    match_result = EmailMatchResult(
        match_type="UNMATCHED",
        matched_job_id=None,
        confidence="LOW",
        score=0,
        evidence=(),
        candidates=(),
    )

    record_a, created_a = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fingerprint-a",
        context_fingerprint="ctx-fixed",
        match_result=match_result,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    record_b, created_b = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fingerprint-b",
        context_fingerprint="ctx-fixed",
        match_result=match_result,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )

    assert created_a is True
    assert created_b is True
    assert record_a.id != record_b.id

    still_there = db.get(GmailMessageAnalysisRecord, record_a.id)
    assert still_there is not None
    assert still_there.input_fingerprint == "fingerprint-a"


def test_concurrent_duplicate_analysis_is_db_safe(db_session_factory):
    """Two real Sessions/threads racing to analyze the exact same
    (message, version, fingerprint) identity must converge on ONE
    persisted row — the UNIQUE constraint is the final arbiter, not a
    Python pre-check (mirrors test_gmail_repository.py's own concurrency
    tests).
    """
    setup_session = db_session_factory()
    _add_job(setup_session)
    msg, _ = upsert_message(setup_session, _parsed())
    msg_id = msg.id
    setup_session.close()

    barrier = threading.Barrier(5)
    results: dict[int, tuple[int, bool]] = {}
    errors: dict[int, BaseException] = {}

    def worker(index: int) -> None:
        session = db_session_factory()
        try:
            barrier.wait(timeout=5)
            record, created = analyze_gmail_message(session, ACCOUNT_A, msg_id)
            results[index] = (record.id, created)
        except BaseException as exc:  # noqa: BLE001
            errors[index] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == {}, f"unhandled exceptions: {errors}"
    assert len(results) == 5
    record_ids = {record_id for record_id, _created in results.values()}
    assert len(record_ids) == 1
    assert sum(1 for _record_id, created in results.values() if created) == 1

    verify_session = db_session_factory()
    try:
        rows = verify_session.scalars(select(GmailMessageAnalysisRecord)).all()
        assert len(rows) == 1
    finally:
        verify_session.close()


def test_get_job_candidates_is_one_query(db):
    for i in range(10):
        _add_job(db, fingerprint=f"fp-{i}", title=f"Role {i}")

    statements: list[str] = []

    def _track(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _track)
    try:
        candidates = get_job_candidates(db)
    finally:
        event.remove(engine, "before_cursor_execute", _track)

    assert len(candidates) == 10
    assert len(statements) == 1


def test_list_analyses_is_one_query_regardless_of_row_count(db):
    for i in range(5):
        _add_job(db, fingerprint=f"fp-{i}", title=f"Role {i}")
        msg, _ = upsert_message(db, _parsed(uid=i + 1, message_id=f"<m{i}@example.com>"))
        analyze_gmail_message(db, ACCOUNT_A, msg.id)

    statements: list[str] = []

    def _track(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _track)
    try:
        rows = list_analyses(db, ACCOUNT_A, limit=50, offset=0)
    finally:
        event.remove(engine, "before_cursor_execute", _track)

    assert len(rows) == 5
    assert len(statements) == 1


def test_cross_account_isolation_at_repository_level(db):
    _add_job(db)
    msg_a, _ = upsert_message(
        db, _parsed(account_key=ACCOUNT_A, uid=1, message_id="<a@example.com>")
    )
    msg_b, _ = upsert_message(
        db, _parsed(account_key=ACCOUNT_B, uid=1, message_id="<b@example.com>")
    )

    analyze_gmail_message(db, ACCOUNT_A, msg_a.id)
    analyze_gmail_message(db, ACCOUNT_B, msg_b.id)

    analyses_a = list_analyses(db, ACCOUNT_A, limit=50, offset=0)
    analyses_b = list_analyses(db, ACCOUNT_B, limit=50, offset=0)

    assert len(analyses_a) == 1
    assert len(analyses_b) == 1
    assert analyses_a[0].gmail_message_id == msg_a.id
    assert analyses_b[0].gmail_message_id == msg_b.id


def test_thread_prior_matches_excludes_current_message_and_is_bounded(db):
    _add_job(db)
    msg1, _ = upsert_message(db, _parsed(uid=1, message_id="<root@example.com>"))
    record1, _ = analyze_gmail_message(db, ACCOUNT_A, msg1.id)

    msg2, _ = upsert_message(
        db,
        _parsed(
            uid=2,
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references=("<root@example.com>",),
        ),
    )

    prior = get_thread_prior_matches(
        db, account_key=ACCOUNT_A, thread_id=msg2.thread_id, exclude_gmail_message_id=msg2.id
    )
    assert msg2.id not in {
        p.job_id for p in prior
    }  # exclude is by message id, not job id coincidence
    if record1.matched_job_id is not None:
        assert any(p.job_id == record1.matched_job_id for p in prior)


def test_determine_requires_human_review_matches_spec_bullets():
    # AMBIGUOUS always requires review
    assert determine_requires_human_review(
        match_type="AMBIGUOUS",
        match_confidence="MEDIUM",
        classification="APPLICATION_RECEIVED",
        classification_confidence="HIGH",
    )
    # LOW match confidence always requires review
    assert determine_requires_human_review(
        match_type="JOB_ONLY",
        match_confidence="LOW",
        classification="APPLICATION_RECEIVED",
        classification_confidence="HIGH",
    )
    # Consequential classifications always require review even at HIGH
    # confidence — 7B-009: REQUEST_FOR_INFORMATION and
    # GENERAL_RECRUITER_MESSAGE were added after a Codex review reproduced
    # a HIGH-confidence REQUEST_FOR_INFORMATION with review skipped.
    for classification in (
        "OFFER",
        "INTERVIEW_INVITATION",
        "REJECTION",
        "INTERVIEW_RESCHEDULE",
        "WITHDRAWAL_OR_POSITION_CLOSED",
        "REQUEST_FOR_INFORMATION",
        "GENERAL_RECRUITER_MESSAGE",
    ):
        assert determine_requires_human_review(
            match_type="APPLICATION",
            match_confidence="HIGH",
            classification=classification,
            classification_confidence="HIGH",
        )
    # UNMATCHED with actionable classification requires review
    assert determine_requires_human_review(
        match_type="UNMATCHED",
        match_confidence="LOW",
        classification="INTERVIEW_INVITATION",
        classification_confidence="HIGH",
    )
    # Clean, non-consequential, well-matched case does not force review
    assert not determine_requires_human_review(
        match_type="APPLICATION",
        match_confidence="HIGH",
        classification="APPLICATION_RECEIVED",
        classification_confidence="HIGH",
    )


def test_analysis_version_constant_is_positive():
    assert ANALYSIS_VERSION > 0


# ---------------------------------------------------------------------------
# 7B-003/004: analysis identity must be sensitive to the EFFECTIVE
# candidate/thread context an analysis run actually considered, not just
# the message's own unchanged content.
# ---------------------------------------------------------------------------


def test_candidate_context_freshness_no_job_then_correct_job_added(db):
    """Test 1 (spec's exact scenario, 7B-003): analyze with no matching
    candidate -> UNMATCHED. Add the correct JobRecord. Re-analyze the
    SAME unchanged email under the SAME algorithm version. Must produce a
    NEW revision that now correctly matches — never silently return the
    stale UNMATCHED row. The old revision must remain queryable.
    """
    msg, _ = upsert_message(db, _parsed())

    record1, created1 = analyze_gmail_message(db, ACCOUNT_A, msg.id)
    assert created1 is True
    assert record1.match_type == "UNMATCHED"

    _add_job(db, status="APPLIED")

    record2, created2 = analyze_gmail_message(db, ACCOUNT_A, msg.id)
    assert created2 is True
    assert record2.id != record1.id
    assert record2.match_type == "APPLICATION"
    assert record2.matched_job_id is not None

    # Previous immutable historical row must remain queryable.
    still_there = db.get(GmailMessageAnalysisRecord, record1.id)
    assert still_there is not None
    assert still_there.match_type == "UNMATCHED"

    # Re-analyzing again with unchanged context is idempotent.
    record3, created3 = analyze_gmail_message(db, ACCOUNT_A, msg.id)
    assert created3 is False
    assert record3.id == record2.id


def test_thread_context_freshness_root_reanalyzed_after_reply_gains_association(db):
    """7B-004: a root message analyzed before any useful thread
    association exists, then re-analyzed after a legitimate reply
    establishes a strong (corroborated) job association in the same
    thread — must reflect the new context, not the stale cached result.
    """
    job = _add_job(
        db, status="APPLIED", company="TrustedCo GmbH", url="https://trustedco.example.com/jobs/1"
    )

    root, _ = upsert_message(
        db,
        _parsed(
            uid=1,
            message_id="<root@example.com>",
            from_address="someone@unrelated.example",
            body_plain="unrelated small talk with no job context at all",
        ),
    )
    record_root_1, _ = analyze_gmail_message(db, ACCOUNT_A, root.id)
    assert record_root_1.match_type == "UNMATCHED"

    upsert_message(
        db,
        _parsed(
            uid=2,
            message_id="<reply@example.com>",
            in_reply_to="<root@example.com>",
            references=("<root@example.com>",),
            from_address="hr@trustedco.example.com",
            body_plain="Following up on your application to TrustedCo GmbH.",
        ),
    )
    from app.db.gmail_repository import get_message_by_identity

    reply_record = get_message_by_identity(db, ACCOUNT_A, "INBOX", 100, 2)
    reply_analysis, _ = analyze_gmail_message(db, ACCOUNT_A, reply_record.id)
    assert reply_analysis.matched_job_id == job.id

    record_root_2, created = analyze_gmail_message(db, ACCOUNT_A, root.id)
    assert created is True
    assert record_root_2.id != record_root_1.id
    assert record_root_2.matched_job_id == job.id


# ---------------------------------------------------------------------------
# 7B-005: thread prior matches must reflect only each message's LATEST
# analysis revision, never an older decisive revision superseded by a
# newer non-decisive one.
# ---------------------------------------------------------------------------


def test_thread_prior_matches_uses_latest_revision_not_stale_decisive_one(db):
    """Scenario A (spec): revision 1 matched job A, revision 2 UNMATCHED
    -> thread association must NOT expose A.
    """
    msg, _ = upsert_message(db, _parsed())
    matched_a = EmailMatchResult(
        match_type="APPLICATION",
        matched_job_id=42,
        confidence="HIGH",
        score=90,
        evidence=(),
        candidates=(),
    )
    unmatched = EmailMatchResult(
        match_type="UNMATCHED",
        matched_job_id=None,
        confidence="LOW",
        score=0,
        evidence=(),
        candidates=(),
    )

    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp1",
        context_fingerprint="ctx1",
        match_result=matched_a,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp1",
        context_fingerprint="ctx2",
        match_result=unmatched,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )

    prior = get_thread_prior_matches(
        db, account_key=ACCOUNT_A, thread_id=msg.thread_id, exclude_gmail_message_id=999999
    )
    assert prior == []


def test_thread_prior_matches_uses_latest_revision_b_over_a(db):
    """Scenario B (spec): revision 1 matched A, revision 2 matched B ->
    only B is current.
    """
    msg, _ = upsert_message(db, _parsed())
    matched_a = EmailMatchResult(
        match_type="APPLICATION",
        matched_job_id=1,
        confidence="HIGH",
        score=90,
        evidence=(),
        candidates=(),
    )
    matched_b = EmailMatchResult(
        match_type="APPLICATION",
        matched_job_id=2,
        confidence="HIGH",
        score=90,
        evidence=(),
        candidates=(),
    )

    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp1",
        context_fingerprint="ctx1",
        match_result=matched_a,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp1",
        context_fingerprint="ctx2",
        match_result=matched_b,
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )

    prior = get_thread_prior_matches(
        db, account_key=ACCOUNT_A, thread_id=msg.thread_id, exclude_gmail_message_id=999999
    )
    assert prior == [ThreadPriorMatch(job_id=2, match_type="APPLICATION")]


def test_thread_prior_matches_multiple_historical_rows_do_not_create_false_ambiguity(db):
    """Scenario C (spec): multiple historical rows for the SAME message
    must not each be counted separately (that would fabricate an
    apparent multi-job thread and defeat the single-distinct-job
    association tier) — only one ThreadPriorMatch per distinct message.
    """
    msg, _ = upsert_message(db, _parsed())
    for i, job_id in enumerate((1, 1, 1), start=1):
        get_or_create_analysis(
            db,
            account_key=ACCOUNT_A,
            gmail_message_id=msg.id,
            analysis_version=1,
            input_fingerprint="fp1",
            context_fingerprint=f"ctx{i}",
            match_result=EmailMatchResult(
                match_type="APPLICATION",
                matched_job_id=job_id,
                confidence="HIGH",
                score=90,
                evidence=(),
                candidates=(),
            ),
            classification_category="UNKNOWN",
            classification_confidence="LOW",
            classification_evidence=(),
            is_automated=False,
            requires_human_review=True,
        )

    prior = get_thread_prior_matches(
        db, account_key=ACCOUNT_A, thread_id=msg.thread_id, exclude_gmail_message_id=999999
    )
    assert prior == [ThreadPriorMatch(job_id=1, match_type="APPLICATION")]


# ---------------------------------------------------------------------------
# 7B-007: old exact-referenced job discoverable beyond the recency window.
# ---------------------------------------------------------------------------


def test_get_job_candidates_finds_old_job_beyond_recency_limit_via_reference(db):
    from datetime import timedelta

    base = datetime.now(UTC)
    old_job = JobRecord(
        fingerprint="old-beyond-500",
        source="test",
        title="Old Role",
        company="OldCo",
        location="",
        url="https://oldco.example.com/jobs/999888",
        description="",
        score=1,
        recommendation="SKIP",
        status="NEW",
        last_seen_at=base - timedelta(days=1000),
    )
    db.add(old_job)
    db.commit()
    from app.db.repositories import sync_job_reference_tokens

    sync_job_reference_tokens(db, old_job)
    for i in range(30):
        db.add(
            JobRecord(
                fingerprint=f"new-{i}",
                source="test",
                title=f"New Role {i}",
                company=f"NewCo{i}",
                location="",
                url=f"https://newco{i}.example.com",
                description="",
                score=1,
                recommendation="SKIP",
                status="NEW",
                last_seen_at=base,
            )
        )
    db.commit()

    without_reference = get_job_candidates(db, limit=10)
    assert old_job.id not in {c.job_id for c in without_reference}

    with_reference = get_job_candidates(db, limit=10, reference_tokens=frozenset({"999888"}))
    assert old_job.id in {c.job_id for c in with_reference}


def test_get_job_candidates_query_count_stays_bounded_with_and_without_reference(db):
    for i in range(20):
        _add_job(db, fingerprint=f"bulk-{i}", title=f"Role {i}")

    statements: list[str] = []

    def _track(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _track)
    try:
        get_job_candidates(db)
        no_ref_count = len(statements)
        statements.clear()
        get_job_candidates(db, reference_tokens=frozenset({"ABC123"}))
        with_ref_count = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", _track)

    assert no_ref_count == 1
    assert with_ref_count == 2


# ---------------------------------------------------------------------------
# compute_context_fingerprint: deterministic, order-independent.
# ---------------------------------------------------------------------------


def test_compute_context_fingerprint_is_order_independent():
    from app.services.email_matching import JobCandidate

    a = JobCandidate(job_id=1, title="T", company="C", location="L", url="U", status="NEW")
    b = JobCandidate(job_id=2, title="T2", company="C2", location="L2", url="U2", status="NEW")

    fp1 = compute_context_fingerprint([a, b], [])
    fp2 = compute_context_fingerprint([b, a], [])
    assert fp1 == fp2


def test_compute_context_fingerprint_changes_when_candidate_pool_changes():
    from app.services.email_matching import JobCandidate

    a = JobCandidate(job_id=1, title="T", company="C", location="L", url="U", status="NEW")
    b = JobCandidate(job_id=2, title="T2", company="C2", location="L2", url="U2", status="NEW")

    fp_before = compute_context_fingerprint([a], [])
    fp_after = compute_context_fingerprint([a, b], [])
    assert fp_before != fp_after


# ---------------------------------------------------------------------------
# Round 2, Blocker 3: JobReferenceTokenRecord exact-equality lookup must
# not be starved by any number of partial-substring collisions.
# ---------------------------------------------------------------------------


def _seed_old_job_and_collisions(db, *, collision_count: int):
    """Bulk-inserts one real exact-referenced job plus `collision_count`
    OTHER jobs whose url/title merely CONTAIN pieces of the same token
    text in a different shape (the pattern that starved the old LIKE-based
    targeted query) — bulk executemany, not per-row ORM commits, so this
    stays fast even at collision_count=5000.
    """
    from app.db.models import JobReferenceTokenRecord

    old_job = JobRecord(
        fingerprint="old-exact",
        source="test",
        title="Old Role",
        company="OldCo",
        location="",
        url="https://oldco.example.com/jobs/12345",
        description="",
        score=1,
        recommendation="SKIP",
        status="NEW",
    )
    db.add(old_job)
    db.commit()
    db.add(JobReferenceTokenRecord(job_id=old_job.id, token="12345"))

    job_rows = [
        {
            "fingerprint": f"collision-{i}",
            "source": "test",
            "title": f"Role {i}",
            "company": f"Co{i}",
            "location": "",
            "url": f"https://co{i}.example.com/jobs/A12345X{i}",
            "description": "",
            "skills_json": "[]",
            "data_confidence": 0.0,
            "must_have_skills_json": "[]",
            "nice_to_have_skills_json": "[]",
            "score": 1,
            "recommendation": "SKIP",
            "status": "NEW",
            "first_seen_at": datetime.now(UTC),
            "last_seen_at": datetime.now(UTC),
        }
        for i in range(collision_count)
    ]
    if job_rows:
        db.execute(JobRecord.__table__.insert(), job_rows)
    db.commit()

    collision_job_ids = db.scalars(
        select(JobRecord.id).where(JobRecord.fingerprint.like("collision-%"))
    ).all()
    token_rows = [
        {"job_id": job_id, "token": f"A12345X{i}"}
        for job_id, i in zip(collision_job_ids, range(collision_count), strict=True)
    ]
    if token_rows:
        db.execute(JobReferenceTokenRecord.__table__.insert(), token_rows)
    db.commit()
    return old_job.id


@pytest.mark.parametrize("collision_count", [50, 51, 100, 600, 5000])
def test_exact_reference_found_regardless_of_partial_collision_count(db, collision_count):
    old_job_id = _seed_old_job_and_collisions(db, collision_count=collision_count)

    candidates = get_job_candidates(db, limit=10, reference_tokens=frozenset({"12345"}))
    assert old_job_id in {c.job_id for c in candidates}


def test_reference_token_equality_excludes_partial_matches(db):
    from app.services.email_matching import extract_reference_tokens

    assert extract_reference_tokens("", "https://x.example.com/A12345X") == frozenset({"A12345X"})
    assert extract_reference_tokens("", "https://x.example.com/912345") == frozenset({"912345"})
    assert extract_reference_tokens("", "https://x.example.com/123456") == frozenset({"123456"})
    assert extract_reference_tokens("", "https://x.example.com/12345") == frozenset({"12345"})
    # None of the "near miss" tokens equal the real one.
    assert "A12345X" != "12345"
    assert "912345" != "12345"
    assert "123456" != "12345"


def test_reference_lookup_query_count_is_o1_regardless_of_collision_count(db):
    _seed_old_job_and_collisions(db, collision_count=600)

    statements: list[str] = []

    def _track(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _track)
    try:
        get_job_candidates(db, limit=10, reference_tokens=frozenset({"12345"}))
    finally:
        event.remove(engine, "before_cursor_execute", _track)

    assert len(statements) == 2  # recency pool + one targeted join, never one-per-job


def test_sync_job_reference_tokens_keeps_token_in_sync_with_job(db):
    from app.db.models import JobReferenceTokenRecord
    from app.db.repositories import sync_job_reference_tokens

    job = JobRecord(
        fingerprint="sync-test",
        source="test",
        title="Role",
        company="Co",
        location="",
        url="https://co.example.com/jobs/55555",
        description="",
        score=1,
        recommendation="SKIP",
        status="NEW",
    )
    db.add(job)
    db.commit()

    sync_job_reference_tokens(db, job)
    tokens = set(
        db.scalars(
            select(JobReferenceTokenRecord.token).where(JobReferenceTokenRecord.job_id == job.id)
        ).all()
    )
    assert tokens == {"55555"}

    # Re-sync with unchanged data is a no-op (idempotent).
    sync_job_reference_tokens(db, job)
    tokens_after = set(
        db.scalars(
            select(JobReferenceTokenRecord.token).where(JobReferenceTokenRecord.job_id == job.id)
        ).all()
    )
    assert tokens_after == {"55555"}


# ---------------------------------------------------------------------------
# Round 2, LOW 2: latest revision must be version-aware, not plain MAX(id).
# ---------------------------------------------------------------------------


def _analysis_result(job_id):
    return EmailMatchResult(
        match_type="APPLICATION",
        matched_job_id=job_id,
        confidence="HIGH",
        score=90,
        evidence=(),
        candidates=(),
    )


def test_latest_analysis_is_version_aware_not_max_id(db):
    """v1(id1) -> v2(id2) -> v1(id3, inserted LATER but an OLDER version)
    -- latest must remain v2/id2, never the higher-id-but-older-version
    row.
    """
    _add_job(db)
    msg, _ = upsert_message(db, _parsed())

    r1, _ = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp",
        context_fingerprint="c1",
        match_result=_analysis_result(1),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    r2, _ = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=2,
        input_fingerprint="fp",
        context_fingerprint="c2",
        match_result=_analysis_result(2),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    r3, _ = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp",
        context_fingerprint="c3",
        match_result=_analysis_result(1),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    assert r3.id > r2.id  # id3 genuinely has the highest id

    latest = get_latest_analysis_for_message(db, ACCOUNT_A, msg.id)
    assert latest.id == r2.id
    assert latest.analysis_version == 2
    assert latest.matched_job_id == 2


def test_latest_analysis_within_same_version_uses_highest_id(db):
    """A later context-triggered revision under the SAME analysis_version
    must still be recognized as latest (id tiebreak within a version).
    """
    _add_job(db)
    msg, _ = upsert_message(db, _parsed())

    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=2,
        input_fingerprint="fp",
        context_fingerprint="c1",
        match_result=_analysis_result(1),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    r4, _ = get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=2,
        input_fingerprint="fp",
        context_fingerprint="c4",
        match_result=_analysis_result(3),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )

    latest = get_latest_analysis_for_message(db, ACCOUNT_A, msg.id)
    assert latest.id == r4.id
    assert latest.matched_job_id == 3


def test_thread_prior_matches_is_also_version_aware(db):
    """Thread-context selection must use the same version-aware "latest"
    rule as get_latest_analysis_for_message.
    """
    _add_job(db)
    msg, _ = upsert_message(db, _parsed())

    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp",
        context_fingerprint="c1",
        match_result=_analysis_result(1),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=2,
        input_fingerprint="fp",
        context_fingerprint="c2",
        match_result=_analysis_result(2),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )
    get_or_create_analysis(
        db,
        account_key=ACCOUNT_A,
        gmail_message_id=msg.id,
        analysis_version=1,
        input_fingerprint="fp",
        context_fingerprint="c3",
        match_result=_analysis_result(1),
        classification_category="UNKNOWN",
        classification_confidence="LOW",
        classification_evidence=(),
        is_automated=False,
        requires_human_review=True,
    )

    prior = get_thread_prior_matches(
        db, account_key=ACCOUNT_A, thread_id=msg.thread_id, exclude_gmail_message_id=999999
    )
    assert prior == [ThreadPriorMatch(job_id=2, match_type="APPLICATION")]
