import json
import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import JobRecord, JobReferenceTokenRecord
from app.db.repositories import _fingerprint, upsert_job
from app.models.job import Job, JobScore


def test_job_deduplication():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    job = Job(
        source="xing",
        title="Python Developer",
        company="Example GmbH",
        url="https://example.com/jobs/1",
    )
    score = JobScore(score=80, recommendation="APPLY")

    with Session(engine) as db:
        first, created_first = upsert_job(db, job, score)
        second, created_second = upsert_job(db, job, score)

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


def test_upsert_inserts_enrichment_fields():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    job = Job(
        source="bundesagentur",
        title="Python Developer",
        company="Example GmbH",
        url="https://example.com/jobs/enriched",
        description="Python and Docker are required; AWS is a plus.",
        skills=["python", "docker", "aws"],
        must_have_skills=["python", "docker"],
        nice_to_have_skills=["aws"],
        skill_source="description_extracted",
    )
    score = JobScore(score=85, recommendation="APPLY", data_confidence=0.9)

    with Session(engine) as db:
        record, created = upsert_job(db, job, score)

        assert created is True
        assert record.data_confidence == 0.9
        assert record.skill_source == "description_extracted"
        assert json.loads(record.must_have_skills_json) == ["python", "docker"]
        assert json.loads(record.nice_to_have_skills_json) == ["aws"]


def test_upsert_updates_enrichment_without_losing_non_empty_description_or_deduplication():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    original = Job(
        source="bundesagentur",
        title="Python Developer",
        company="Example GmbH",
        url="https://example.com/jobs/update",
        description="Persisted enriched description",
        must_have_skills=["python"],
        nice_to_have_skills=["docker"],
        skill_source="description_extracted",
    )
    repeated = original.model_copy(
        update={
            "description": "",
            "must_have_skills": ["python", "fastapi"],
            "nice_to_have_skills": ["aws"],
            "skill_source": "description_inferred",
        }
    )

    with Session(engine) as db:
        first, created_first = upsert_job(
            db, original, JobScore(score=70, recommendation="MAYBE", data_confidence=0.7)
        )
        second, created_second = upsert_job(
            db, repeated, JobScore(score=88, recommendation="APPLY", data_confidence=0.95)
        )
        count = db.scalar(select(func.count()).select_from(JobRecord))

        assert created_first is True
        assert created_second is False
        assert second.id == first.id
        assert count == 1
        assert second.description == "Persisted enriched description"
        assert second.score == 88
        assert second.recommendation == "APPLY"
        assert second.data_confidence == 0.95
        assert second.skill_source == "description_inferred"
        assert json.loads(second.must_have_skills_json) == ["python", "fastapi"]
        assert json.loads(second.nice_to_have_skills_json) == ["aws"]


def test_xing_fingerprint_ignores_url():
    # XING digest emails embed a per-recipient tracking redirect as the
    # job's url — confirmed to differ across separate emails advertising
    # the same real posting (see app/collectors/xing_email.py). The
    # fingerprint must therefore not depend on url for this source.
    job_a = Job(
        source="xing",
        title="Junior Informatiker (m/w/d)",
        company="Institut gGmbH",
        location="Heidelberg",
        url="https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1",
    )
    job_b = Job(
        source="xing",
        title="Junior Informatiker (m/w/d)",
        company="Institut gGmbH",
        location="Heidelberg",
        url="https://www.xing.com/m/BBBBBBBBBBBBBBBBBBBB2",
    )

    assert _fingerprint(job_a) == _fingerprint(job_b)


def test_xing_fingerprint_still_distinguishes_different_postings():
    job_a = Job(
        source="xing",
        title="Junior Informatiker (m/w/d)",
        company="Institut gGmbH",
        location="Heidelberg",
        url="https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1",
    )
    job_b = Job(
        source="xing",
        title="Senior Informatiker (m/w/d)",
        company="Institut gGmbH",
        location="Heidelberg",
        url="https://www.xing.com/m/AAAAAAAAAAAAAAAAAAAA1",
    )

    assert _fingerprint(job_a) != _fingerprint(job_b)


def test_bundesagentur_fingerprint_still_depends_on_url():
    # Regression guard: existing sources (bundesagentur, and manual
    # /jobs/score calls with no known source) must keep the original
    # source+company+title+url formula unchanged — a different url for an
    # otherwise-identical job must still produce a different fingerprint,
    # exactly as it did before the per-source fingerprint fields were
    # introduced for "xing".
    job_a = Job(
        source="bundesagentur",
        title="Python Developer",
        company="Example GmbH",
        location="Berlin",
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1",
    )
    job_b = Job(
        source="bundesagentur",
        title="Python Developer",
        company="Example GmbH",
        location="Berlin",
        url="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-2",
    )

    assert _fingerprint(job_a) != _fingerprint(job_b)


def test_generic_source_fingerprint_still_depends_on_url():
    job_a = Job(
        source="manual",
        title="Python Developer",
        company="Example GmbH",
        url="https://example.com/jobs/1",
    )
    job_b = Job(
        source="manual",
        title="Python Developer",
        company="Example GmbH",
        url="https://example.com/jobs/2",
    )

    assert _fingerprint(job_a) != _fingerprint(job_b)


# ---------------------------------------------------------------------------
# Round 3, Blocker R3-001: upsert_job + job_reference_tokens sync must be
# ONE transaction with ONE commit -- an injected failure mid-token-write
# must roll back BOTH the JobRecord mutation and the token mutation, never
# leave a durable JobRecord with stale/empty derived tokens.
# ---------------------------------------------------------------------------


def _fail_on_token_add(monkeypatch, db):
    """Monkeypatches `db.add` to raise the moment `sync_job_reference_tokens`
    tries to insert a `JobReferenceTokenRecord` row -- simulates "injected
    failure during token INSERT" from the remediation spec without needing
    to reach into SQLAlchemy's flush internals.
    """
    original_add = db.add

    def add_with_failure(instance):
        if isinstance(instance, JobReferenceTokenRecord):
            raise RuntimeError("simulated token insert failure")
        return original_add(instance)

    monkeypatch.setattr(db, "add", add_with_failure)


def test_rollback_probe_new_job_token_insert_failure_leaves_nothing_durable(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback_new_job.db'}")
    Base.metadata.create_all(engine)
    job = Job(
        source="test",
        title="Python Developer",
        company="Acme",
        url="https://acme.example.com/jobs/AAA111",
    )
    score = JobScore(score=80, recommendation="APPLY")

    db = Session(engine)
    _fail_on_token_add(monkeypatch, db)

    with pytest.raises(RuntimeError):
        upsert_job(db, job, score)

    # Session recovered via rollback -- still usable for further queries.
    assert db.scalar(select(func.count()).select_from(JobRecord)) == 0
    db.close()

    fresh = Session(engine)
    try:
        assert fresh.scalar(select(func.count()).select_from(JobRecord)) == 0
        assert fresh.scalar(select(func.count()).select_from(JobReferenceTokenRecord)) == 0
    finally:
        fresh.close()


def test_rollback_probe_existing_job_update_token_insert_failure_keeps_old_state(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback_update_job.db'}")
    Base.metadata.create_all(engine)
    score = JobScore(score=80, recommendation="APPLY")

    # xing's fingerprint formula deliberately excludes `url` (see
    # app.db.repositories's _FINGERPRINT_FIELDS_BY_SOURCE docstring) so an
    # update that only changes the URL still resolves to the SAME existing
    # JobRecord via get_job_by_fingerprint, exercising the update path.
    original_job = Job(
        source="xing",
        title="Python Developer",
        company="Acme",
        location="Berlin",
        url="https://acme.example.com/jobs/AAA111",
    )
    setup_db = Session(engine)
    record, created = upsert_job(setup_db, original_job, score)
    assert created is True
    job_id = record.id
    initial_tokens = set(
        setup_db.scalars(
            select(JobReferenceTokenRecord.token).where(JobReferenceTokenRecord.job_id == job_id)
        ).all()
    )
    assert initial_tokens == {"AAA111"}
    setup_db.close()

    # `upsert_job`'s existing-record update path deliberately never touches
    # `url` (see that path's own fields) -- so a URL change is simulated
    # the same way this project's own repository-boundary helper would see
    # one: mutate the loaded record directly and drive it through
    # `_finalize_job_write`, the SAME atomic write+sync+commit boundary
    # `upsert_job` itself uses. This isolates the R3-001 transaction
    # contract from `upsert_job`'s unrelated field-selection behavior,
    # which this round must not change.
    from app.db.repositories import _finalize_job_write

    db = Session(engine)
    _fail_on_token_add(monkeypatch, db)
    record_to_update = db.get(JobRecord, job_id)
    record_to_update.url = "https://acme.example.com/jobs/BBB222"

    with pytest.raises(RuntimeError):
        _finalize_job_write(db, record_to_update)
    db.close()

    fresh = Session(engine)
    try:
        final_record = fresh.get(JobRecord, job_id)
        assert final_record.url == "https://acme.example.com/jobs/AAA111"
        final_tokens = set(
            fresh.scalars(
                select(JobReferenceTokenRecord.token).where(
                    JobReferenceTokenRecord.job_id == job_id
                )
            ).all()
        )
        assert final_tokens == {"AAA111"}
    finally:
        fresh.close()


def test_successful_update_leaves_only_new_token_no_stale_old_token(tmp_path):
    from app.db.repositories import _finalize_job_write

    engine = create_engine(f"sqlite:///{tmp_path / 'successful_update.db'}")
    Base.metadata.create_all(engine)
    score = JobScore(score=80, recommendation="APPLY")

    db = Session(engine)
    original_job = Job(
        source="xing",
        title="Python Developer",
        company="Acme",
        location="Berlin",
        url="https://acme.example.com/jobs/AAA111",
    )
    record, created = upsert_job(db, original_job, score)
    assert created is True
    job_id = record.id

    record.url = "https://acme.example.com/jobs/BBB222"
    updated_record = _finalize_job_write(db, record)
    assert updated_record.id == job_id
    assert updated_record.url == "https://acme.example.com/jobs/BBB222"

    tokens = set(
        db.scalars(
            select(JobReferenceTokenRecord.token).where(JobReferenceTokenRecord.job_id == job_id)
        ).all()
    )
    assert tokens == {"BBB222"}
    db.close()


def test_concurrent_updates_to_same_job_reference_never_duplicate_token_rows(tmp_path):
    """Two real Sessions/threads racing to sync job_reference_tokens for the
    SAME existing job must never both land the same (job_id, token) row --
    UNIQUE(job_id, token) is the final DB-level guard (see
    JobReferenceTokenRecord). A losing thread may see its own upsert_job
    raise (the transaction it lost rolls back), but its Session must stay
    usable afterward, and the table must never end up with a duplicate row
    for the same (job_id, token) pair no matter which thread "wins".
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent_token_update.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    score = JobScore(score=80, recommendation="APPLY")

    from app.db.repositories import _finalize_job_write

    setup_db = session_factory()
    original_job = Job(
        source="xing",
        title="Python Developer",
        company="Acme",
        location="Berlin",
        url="https://acme.example.com/jobs/AAA111",
    )
    record, _created = upsert_job(setup_db, original_job, score)
    job_id = record.id
    setup_db.close()

    barrier = threading.Barrier(4)
    errors: dict[int, BaseException] = {}

    def worker(index: int) -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            record_to_update = session.get(JobRecord, job_id)
            record_to_update.url = "https://acme.example.com/jobs/BBB222"
            _finalize_job_write(session, record_to_update)
        except BaseException as exc:  # noqa: BLE001
            errors[index] = exc
        finally:
            # The session must remain usable even after a caught failure --
            # a trivial query must not itself raise.
            session.execute(select(func.count()).select_from(JobRecord))
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    verify = session_factory()
    try:
        rows = verify.scalars(
            select(JobReferenceTokenRecord).where(JobReferenceTokenRecord.job_id == job_id)
        ).all()
        tokens = [row.token for row in rows]
        assert len(tokens) == len(set(tokens)), "duplicate (job_id, token) rows persisted"
        assert set(tokens) == {"BBB222"}
    finally:
        verify.close()


def test_sync_job_reference_tokens_does_not_commit_itself(tmp_path):
    """Round 3 (Blocker R3-001) contract: `sync_job_reference_tokens` only
    mutates the current Session -- it must never commit, so its writes stay
    invisible to a DIFFERENT session until the caller (`upsert_job`, via
    `_finalize_job_write`) commits.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'no_self_commit.db'}")
    Base.metadata.create_all(engine)
    score = JobScore(score=80, recommendation="APPLY")

    db = Session(engine)
    job = Job(
        source="test",
        title="Python Developer",
        company="Acme",
        url="https://acme.example.com/jobs/AAA111",
    )
    record, _created = upsert_job(db, job, score)
    job_id = record.id

    from app.db.repositories import sync_job_reference_tokens

    record.url = "https://acme.example.com/jobs/CCC333"
    sync_job_reference_tokens(db, record)

    other = Session(engine)
    try:
        other_tokens = set(
            other.scalars(
                select(JobReferenceTokenRecord.token).where(
                    JobReferenceTokenRecord.job_id == job_id
                )
            ).all()
        )
        # sync_job_reference_tokens alone must not have committed -- a
        # different session must still see the OLD token set, never the
        # new one durably persisted without an explicit commit.
        assert "CCC333" not in other_tokens
        assert other_tokens == {"AAA111"}
    finally:
        other.close()
    db.close()
