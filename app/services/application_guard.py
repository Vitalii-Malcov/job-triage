"""Stage 17C: no new Bewerbung for a vacancy that was already applied to.

`ensure_not_already_applied` runs the Stage 15 duplicate check (same job row,
BA reference, URL, fingerprint, or same employer + role -- across sources:
StepStone, XING, LinkedIn, Indeed, Bundesagentur) against every recorded
application. It refuses when a STRONG match is an application that was
actually sent (SUBMITTED or any later status). An application still being
prepared (DISCOVERED .. APPROVED) is the normal package flow and is not
refused. Called by BewerbungService.generate and ReviewPackageService.create,
so the API, the automation shortlist and Telegram all go through it.

`vacancy_findings` also passes the vacancy's description and the
descriptions of the jobs the applications were recorded for, so the
advisory SIMILAR_DESCRIPTION signal (Stage 22) reaches the fit assessment
and the UI. It is PROBABLE only and never refuses here.
"""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import application_repository as app_repo
from app.db.models import JobRecord
from app.models.application_lifecycle import Application
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.services.application_duplicates import DuplicateFinding, VacancyView, assess_vacancy

SENT_STATUSES = frozenset(
    {
        S.SUBMITTED,
        S.RECEIVED,
        S.UNDER_REVIEW,
        S.INTERVIEW,
        S.TECHNICAL_INTERVIEW,
        S.OFFER,
        S.REJECTED,
        S.WITHDRAWN,
    }
)


class AlreadyAppliedError(Exception):
    """A Bewerbung for an already-applied vacancy was refused."""

    def __init__(self, job_id: int, finding: DuplicateFinding) -> None:
        self.job_id = job_id
        self.application_id = finding.application_id
        self.application_status = finding.status
        self.signals = finding.signals
        super().__init__(
            f"job {job_id} was already applied to: application {finding.application_id} "
            f"[{finding.status}] ({', '.join(finding.signals)}); no new Bewerbung is created"
        )


def applied_job_descriptions(
    db: Session, applications: Iterable[Application]
) -> dict[int, str | None]:
    """Application id -> description of the job row it was recorded for
    (applications without a job row, or whose job row is gone, are absent).
    `applications.job_id` has no foreign key: a missing row is not an error."""
    by_application = {a.id: a.job_id for a in applications if a.job_id is not None}
    if not by_application:
        return {}
    rows = db.execute(
        select(JobRecord.id, JobRecord.description).where(
            JobRecord.id.in_(set(by_application.values()))
        )
    ).all()
    descriptions = dict(rows)
    return {
        app_id: descriptions[job_id]
        for app_id, job_id in by_application.items()
        if job_id in descriptions
    }


def vacancy_findings(db: Session, job: JobRecord) -> list[DuplicateFinding]:
    applications = [app_repo.to_application(r) for r in app_repo.iter_applications(db)]
    view = VacancyView(
        job.id,
        job.source,
        job.company,
        job.title,
        job.url,
        job.fingerprint,
        job.location,
        job.description,
        job.source_reference,
    )
    return assess_vacancy(view, applications, applied_job_descriptions(db, applications))


def sent_duplicate(findings: list[DuplicateFinding]) -> DuplicateFinding | None:
    return next(
        (f for f in findings if f.strength == "STRONG" and f.status in SENT_STATUSES),
        None,
    )


def ensure_not_already_applied(db: Session, job: JobRecord) -> None:
    finding = sent_duplicate(vacancy_findings(db, job))
    if finding is not None:
        raise AlreadyAppliedError(job.id, finding)
