"""Stage 17C: no new Bewerbung for a vacancy that was already applied to.

`ensure_not_already_applied` runs the Stage 15 duplicate check (same job row,
BA reference, URL, fingerprint, or same employer + role -- across sources:
StepStone, XING, LinkedIn, Indeed, Bundesagentur) against every recorded
application. It refuses when a STRONG match is an application that was
actually sent (SUBMITTED or any later status). An application still being
prepared (DISCOVERED .. APPROVED) is the normal package flow and is not
refused. Called by BewerbungService.generate and ReviewPackageService.create,
so the API, the automation shortlist and Telegram all go through it.
"""

from sqlalchemy.orm import Session

from app.db import application_repository as app_repo
from app.db.models import JobRecord
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


def vacancy_findings(db: Session, job: JobRecord) -> list[DuplicateFinding]:
    applications = [app_repo.to_application(r) for r in app_repo.iter_applications(db)]
    view = VacancyView(
        job.id, job.source, job.company, job.title, job.url, job.fingerprint, job.location
    )
    return assess_vacancy(view, applications)


def sent_duplicate(findings: list[DuplicateFinding]) -> DuplicateFinding | None:
    return next(
        (f for f in findings if f.strength == "STRONG" and f.status in SENT_STATUSES),
        None,
    )


def ensure_not_already_applied(db: Session, job: JobRecord) -> None:
    finding = sent_duplicate(vacancy_findings(db, job))
    if finding is not None:
        raise AlreadyAppliedError(job.id, finding)
