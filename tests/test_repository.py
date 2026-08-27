import json

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import JobRecord
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
