from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
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
