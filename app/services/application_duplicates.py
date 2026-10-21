"""Duplicate-application check for a vacancy, BEFORE the operator applies.

One real vacancy can appear several times in the job table: the employer's
own Bundesagentur post, a XING copy, a Jobriver listing, a recruiter or
agency mirror. Applying twice to the same employer for the same role is a
real-world harm that cannot be undone, so before preparing or submitting
anything the operator checks the vacancy against every recorded
application (`stage15_application_tracker.py check-job <job_id>`).

**Deterministic, conservative, read-only.** No fuzzy similarity, no LLM, no
network, no writes. Each finding names the exact signals that produced it:

- STRONG (do not apply again without a deliberate decision):
  SAME_JOB (the application was recorded for this job row),
  SAME_REFERENCE (the application's source reference is this job's
  Bundesagentur reference, from its BA detail URL),
  SAME_URL (identical normalized posting URL),
  SAME_FINGERPRINT (identical vacancy fingerprint),
  SAME_EMPLOYER_AND_ROLE (normalized company AND normalized role title).
- PROBABLE (verify by hand; never a block):
  SAME_TITLE_OTHER_EMPLOYER -- the identical full title, gender suffix
  included, at a different company name. This is how anonymized agency
  mirrors look ("Backend Developer (m/w/div.)" from a staffing agency), but
  common titles collide too, so it only ever asks for a human look.
- SAME_EMPLOYER_OTHER_ROLE is informational only: a different role at the
  same employer is a genuinely different application (e.g. ALD Backend
  Developer vs. ALD Software Engineer Prototyping).

Terminal applications (REJECTED/WITHDRAWN/OFFER) still count: a second
application after a rejection is exactly what the operator must notice.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.models.application_lifecycle import Application
from app.services.application_correlation import role_phrase
from app.services.email_matching import normalize_company_name

Strength = Literal["STRONG", "PROBABLE", "INFO"]

_BA_DETAIL = re.compile(r"/jobsuche/jobdetail/([^/?#]+)", re.IGNORECASE)
_STRONG = ("SAME_JOB", "SAME_REFERENCE", "SAME_URL", "SAME_FINGERPRINT", "SAME_EMPLOYER_AND_ROLE")
# Query parameters that only track a click, never identify a vacancy
# (compared case-insensitively; plus every `utm_*`). Anything else -- e.g.
# `?id=123` -- may be the vacancy identity and is kept.
_TRACKING_PARAMS = frozenset({"trackingid", "trk", "refid", "gclid", "fbclid"})


@dataclass(frozen=True)
class VacancyView:
    """The job-row facts the check needs (no description, no scores)."""

    job_id: int | None
    source: str
    company: str
    title: str
    url: str | None
    fingerprint: str | None = None


@dataclass(frozen=True)
class DuplicateFinding:
    application_id: int
    strength: Strength
    signals: tuple[str, ...]
    company: str
    role: str
    status: str


def _is_tracking_param(name: str) -> bool:
    name = name.casefold()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def normalize_posting_url(url: str | None) -> str | None:
    """Scheme/host case-folded, trailing slash and fragment removed, known
    tracking parameters dropped, the remaining query parameters kept (they
    may identify the vacancy) in a stable order. Never fetched -- string
    comparison only."""
    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return None
    path = parts.path.rstrip("/")
    query = urlencode(
        sorted(
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_param(name)
        )
    )
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, query, ""))


def bundesagentur_reference(url: str | None) -> str | None:
    match = _BA_DETAIL.search(url or "")
    return match.group(1).casefold() if match else None


def _title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def assess_vacancy(
    vacancy: VacancyView, applications: Iterable[Application]
) -> list[DuplicateFinding]:
    """Every application this vacancy may duplicate, strongest first."""
    company = normalize_company_name(vacancy.company)
    role = role_phrase(vacancy.title)
    title = _title(vacancy.title)
    url = normalize_posting_url(vacancy.url)
    ba_ref = bundesagentur_reference(vacancy.url)
    findings: list[DuplicateFinding] = []
    for app in applications:
        signals: list[str] = []
        if vacancy.job_id is not None and app.job_id == vacancy.job_id:
            signals.append("SAME_JOB")
        reference = (app.source_reference or "").casefold()
        if reference and ba_ref == reference:
            signals.append("SAME_REFERENCE")
        if url and normalize_posting_url(app.destination_url) == url:
            signals.append("SAME_URL")
        if vacancy.fingerprint and app.vacancy_fingerprint == vacancy.fingerprint:
            signals.append("SAME_FINGERPRINT")
        same_company = bool(company) and normalize_company_name(app.company_name) == company
        same_role = bool(role) and role_phrase(app.role_title) == role
        if same_company and same_role:
            signals.append("SAME_EMPLOYER_AND_ROLE")
        if any(s in _STRONG for s in signals):
            strength: Strength = "STRONG"
        elif not same_company and title and _title(app.role_title) == title:
            signals.append("SAME_TITLE_OTHER_EMPLOYER")
            strength = "PROBABLE"
        elif same_company:
            signals.append("SAME_EMPLOYER_OTHER_ROLE")
            strength = "INFO"
        else:
            continue
        findings.append(
            DuplicateFinding(
                application_id=app.id,
                strength=strength,
                signals=tuple(signals),
                company=app.company_name,
                role=app.role_title,
                status=app.current_status.value,
            )
        )
    order = {"STRONG": 0, "PROBABLE": 1, "INFO": 2}
    return sorted(findings, key=lambda f: (order[f.strength], f.application_id))


def job_association_mismatches(
    job: VacancyView,
    *,
    company: str,
    role: str,
    source: str,
    reference: str,
    destination_url: str | None = None,
) -> list[str]:
    """Why a submission must NOT be linked to this collected job (empty =
    consistent). Conservative: normalized company AND role must match (legal
    form, case, punctuation and gender suffix ignored -- an empty
    normalization never matches), the source must be the job's source, and
    a Bundesagentur reference derivable from the job's URL (or from a BA
    destination URL) must equal the submission reference."""
    mismatches: list[str] = []
    job_company = normalize_company_name(job.company)
    if not job_company or job_company != normalize_company_name(company):
        mismatches.append("COMPANY")
    job_role = role_phrase(job.title)
    if not job_role or job_role != role_phrase(role):
        mismatches.append("ROLE")
    if job.source.strip().casefold() != source.strip().casefold():
        mismatches.append("SOURCE")
    job_reference = bundesagentur_reference(job.url)
    if job_reference is not None and job_reference != reference.casefold():
        mismatches.append("REFERENCE")
    destination_reference = bundesagentur_reference(destination_url)
    if destination_reference is not None and destination_reference != (
        job_reference or reference.casefold()
    ):
        mismatches.append("DESTINATION_URL")
    return mismatches


def verdict(findings: Iterable[DuplicateFinding]) -> str:
    """ALREADY_APPLIED | POSSIBLE_DUPLICATE | NO_DUPLICATE_FOUND."""
    strengths = {f.strength for f in findings}
    if "STRONG" in strengths:
        return "ALREADY_APPLIED"
    if "PROBABLE" in strengths:
        return "POSSIBLE_DUPLICATE"
    return "NO_DUPLICATE_FOUND"
