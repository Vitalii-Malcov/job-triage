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
  SIMILAR_ROLE_VIA_INTERMEDIARY -- the vacancy is posted by a staffing
  agency / job board, in the same city as the application, and its title
  shares the distinctive words of the application's role (night audit
  2026-10-27: "IT-Spezialist Produktions-IT & Systemintegration" at an agency
  vs. the applied "IT-Systemintegrator Produktions-IT / Shopfloor", same
  city, was NO_DUPLICATE_FOUND). The agency hides the real employer, so
  this can only ever ask for a human look.
- SAME_EMPLOYER_OTHER_ROLE is informational only: a different role at the
  same employer is a genuinely different application (e.g. ALD Backend
  Developer vs. ALD Software Engineer Prototyping).

Company identity for these checks folds what the shared
`normalize_company_name` keeps apart on purpose (it also feeds stored
keys): umlaut transliteration, "e.G."/"eG" and dotted abbreviations
("N.O.C" = "NOC"). A branch suffix ("... Standort Frankfurt") is NOT
folded: "Example Standort Berlin GmbH" and "Example GmbH" may be different
employers, and SAME_EMPLOYER_AND_ROLE blocks (Codex night-audit review).

`classify_duplicate` grades the findings for the operator:
EXACT_DUPLICATE (job row, reference, URL or fingerprint identical),
PROBABLE_DUPLICATE (same employer AND role, no identifier proof),
POSSIBLE_DUPLICATE (agency/mirror signals only), NOT_DUPLICATE, or
INSUFFICIENT_EVIDENCE (the vacancy has no usable company or title and no
identifier matched).

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
DuplicateClass = Literal[
    "EXACT_DUPLICATE",
    "PROBABLE_DUPLICATE",
    "POSSIBLE_DUPLICATE",
    "NOT_DUPLICATE",
    "INSUFFICIENT_EVIDENCE",
]
_EXACT = frozenset({"SAME_JOB", "SAME_REFERENCE", "SAME_URL", "SAME_FINGERPRINT"})

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
    location: str | None = None


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


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_DOTTED_ABBREVIATION = re.compile(r"\b(?:[a-z0-9]\.)+[a-z0-9]\b\.?")
_EG_SUFFIX = re.compile(r"\be\.?\s?g\.?(?=\s|$)")


def company_identity(name: str | None) -> str:
    """Comparison key for "is this the same employer?" (duplicates only)."""
    value = (name or "").casefold().translate(_UMLAUTS)
    value = _DOTTED_ABBREVIATION.sub(lambda m: m.group(0).replace(".", ""), value)
    value = _EG_SUFFIX.sub(" ", value)
    return normalize_company_name(value)


# Staffing agencies and job boards: their postings hide (or mirror) the
# actual employer. Whole words of the company identity.
_INTERMEDIARY = re.compile(
    r"\b(?:personal\w*|zeitarbeit|arbeitnehmerueberlassung|recruit\w*|jobboerse|"
    r"jobriver|stellenmarkt|akkodis|hays|adecco|randstad|manpower|gulp|brunel|ferchau|"
    r"orizon|experis|amadeus fire|modis|solcom|etengo|piening|puro|allgeier|"
    r"gi group|tempton|timepartner|jobactive|bindan|trenkwalder|noc engineering)\b"
)


def is_intermediary(company: str | None) -> bool:
    return bool(_INTERMEDIARY.search(company_identity(company)))


# Title words that say nothing about WHICH job it is.
_GENERIC_TITLE_WORDS = frozenset(
    (
        "div divers genders innen fuer bei zur zum der die das als oder junior senior lead "
        "mitarbeiter mitarbeiterin spezialist specialist engineer ingenieur manager fachkraft "
        "bereich schwerpunkt vollzeit teilzeit remote hybrid with from"
    ).split()
)


def _title_stems(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", role_phrase(title).translate(_UMLAUTS))
    return frozenset(w[:10] for w in words if len(w) >= 4 and w not in _GENERIC_TITLE_WORDS)


def _city(location: str | None) -> str:
    value = (location or "").casefold().translate(_UMLAUTS).split(",")[0]
    return re.sub(r"\s+", " ", re.sub(r"\d+", " ", value)).strip()


def _similar_role(a: str, b: str) -> bool:
    left, right = _title_stems(a), _title_stems(b)
    shared = left & right
    return len(shared) >= 2 or (len(shared) == 1 and min(len(left), len(right)) == 1)


def assess_vacancy(
    vacancy: VacancyView, applications: Iterable[Application]
) -> list[DuplicateFinding]:
    """Every application this vacancy may duplicate, strongest first."""
    company = company_identity(vacancy.company)
    via_intermediary = is_intermediary(vacancy.company)
    city = _city(vacancy.location)
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
        same_company = bool(company) and company_identity(app.company_name) == company
        same_role = bool(role) and role_phrase(app.role_title) == role
        if same_company and same_role:
            signals.append("SAME_EMPLOYER_AND_ROLE")
        if any(s in _STRONG for s in signals):
            strength: Strength = "STRONG"
        elif not same_company and title and _title(app.role_title) == title:
            signals.append("SAME_TITLE_OTHER_EMPLOYER")
            strength = "PROBABLE"
        elif (
            not same_company
            and via_intermediary
            and city
            and _city(app.location) == city
            and _similar_role(vacancy.title, app.role_title)
        ):
            signals.append("SIMILAR_ROLE_VIA_INTERMEDIARY")
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


def classify_duplicate(
    vacancy: VacancyView, findings: Iterable[DuplicateFinding]
) -> DuplicateClass:
    """Operator-facing grade of `assess_vacancy` findings (see module doc).
    Only EXACT_DUPLICATE is proven; everything else asks for a human look
    before applying, and NOT_DUPLICATE never claims more than "no signal"."""
    findings = list(findings)
    signals = {s for f in findings for s in f.signals}
    if signals & _EXACT:
        return "EXACT_DUPLICATE"
    if "SAME_EMPLOYER_AND_ROLE" in signals:
        return "PROBABLE_DUPLICATE"
    if not company_identity(vacancy.company) or not role_phrase(vacancy.title):
        return "INSUFFICIENT_EVIDENCE"
    if any(f.strength == "PROBABLE" for f in findings):
        return "POSSIBLE_DUPLICATE"
    return "NOT_DUPLICATE"


def verdict(findings: Iterable[DuplicateFinding]) -> str:
    """ALREADY_APPLIED | POSSIBLE_DUPLICATE | NO_DUPLICATE_FOUND."""
    strengths = {f.strength for f in findings}
    if "STRONG" in strengths:
        return "ALREADY_APPLIED"
    if "PROBABLE" in strengths:
        return "POSSIBLE_DUPLICATE"
    return "NO_DUPLICATE_FOUND"
