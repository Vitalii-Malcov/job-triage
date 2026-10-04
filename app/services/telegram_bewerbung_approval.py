"""Stage 9C: human review / approval of the EXACT Stage 9B Bewerbung
package in the operator's private Telegram chat.

**Stage 6E is the only approval state machine.** Telegram creates (or
reuses) exactly one Stage 6E review per Stage 9B package generation, shows
the COMPLETE content of the exact bound revision, and delegates the
explicit decision to `ReviewPackageService.approve` / `.reject`. The
immutable link (`telegram_bewerbung_approvals`) carries identity and the
opaque decision capability only -- never a status.

**APPROVED != SENT.** Nothing here sends an application or an email,
creates a Gmail draft, contacts an employer, or writes `JobRecord.status`,
Stage 9A review state or any Stage 9B preparation/package/preview field.
The only network call is the shared Telegram sender to the configured
private operator chat, and no DB transaction or lock is open while it runs.

Layout: the pure part (complete review document, deterministic pagination
with a completeness check, strict callback parsing) comes first; the
transactional orchestration (`handle_request_review`, `handle_review_page`,
`handle_decision`) follows.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.agents.review_package_builder import (
    ReviewBewerbungDraftJobMismatchError,
    ReviewBewerbungDraftNotFoundError,
    ReviewCurrentJobMissingError,
    ReviewCurrentProfileMissingError,
    ReviewCVDraftJobMismatchError,
    ReviewCVDraftNotFoundError,
    ReviewJobChangedError,
    ReviewManualOverrideAcknowledgmentRequiredError,
    ReviewNotFoundError,
    ReviewNotPendingError,
    ReviewProfileChangedError,
    ReviewSourceMismatchError,
    ReviewVersionConflictError,
)
from app.db.bewerbung_repository import get_bewerbung_draft_by_id, to_bewerbung_draft
from app.db.candidate_cv_draft_repository import get_draft_by_id, to_tailored_cv_draft
from app.db.candidate_job_match_repository import get_match_by_id, to_candidate_job_match
from app.db.candidate_profile_repository import get_candidate_profile
from app.db.models import (
    ApplicationPackageReviewRecord,
    CandidateProfileRecord,
    JobRecord,
    TelegramBewerbungApprovalRecord,
    TelegramBewerbungPreparationRecord,
    TelegramVacancyReviewRecord,
)
from app.db.review_package_repository import get_revision_by_id
from app.db.telegram_bewerbung_approval_repository import (
    APPROVAL_CAPABILITY_PATTERN,
    CAPABILITY_CONFLICT,
    GENERATION_CONFLICT,
    classify_link_conflict,
    get_bound_revision,
    get_link_by_capability,
    get_link_for_generation,
    insert_link,
    lock_job_fresh,
    lock_preparation_fresh,
    lock_profile_fresh,
    lock_review_header_fresh,
    new_approval_capability,
)
from app.db.telegram_bewerbung_repository import (
    PACKAGE_TOKEN_PATTERN,
    get_preparation_by_package_token,
)
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.cv_draft import TailoredCVDraft
from app.models.review_package import ReviewedBewerbungContent, ReviewedCVContent
from app.services.bewerbung_reuse import bewerbung_draft_is_current
from app.services.review_package import ReviewPackageService
from app.services.telegram import TelegramSendOutcome, send_telegram_message
from app.services.telegram_bewerbung import (
    ELIGIBLE_JOB_STATUSES,
    REVIEW_REQUEST_CALLBACK_PREFIX,
    compute_input_identity,
)
from app.services.telegram_bewerbung_preview import (
    TELEGRAM_UNIT_LIMIT,
    normalize_multiline,
    normalize_scalar,
    utf16_units,
)

MAX_REVIEW_PAGES = 30

REQUEST_PREFIX = REVIEW_REQUEST_CALLBACK_PREFIX
PAGE_PREFIX = "bv"
DECISION_PREFIX = "bz"
APPROVE_ACTION = "f"
REJECT_ACTION = "x"
_DECISION_ACTIONS = frozenset({APPROVE_ACTION, REJECT_ACTION})
_PAGE_PATTERN = re.compile(r"[1-9][0-9]?")

REVIEW_STATUS_LINE = "PRÜFUNG — ENTWURF, NICHT GESENDET"
STALE_PACKAGE_LABEL = "Paket veraltet — Freigabe nicht möglich"
NOTHING_SENT_FOOTER = "Ende des Prüfdokuments — Freigeben sendet nichts."

# Display modes for the final page's decision buttons.
MODE_DECIDE = "DECIDE"
MODE_REJECT_ONLY = "REJECT_ONLY"

# Fixed CV section order. A section the revision's `section_order` does not
# name is still rendered, in this order, after the named ones -- never
# omitted.
CV_SECTIONS = (
    "HEADER",
    "SUMMARY",
    "SKILLS",
    "EXPERIENCE",
    "PROJECTS",
    "EDUCATION",
    "CERTIFICATIONS",
    "LANGUAGES",
)


@dataclass(frozen=True)
class ReviewSnapshot:
    """Detached content of the exact bound review: the bound revision's
    reviewed letter/CV fields, the exact pinned CV draft and match, and the
    pinned letter's persisted job context and warnings. Built inside the DB
    transaction and rendered after it ends."""

    reviewed_cv: ReviewedCVContent
    reviewed_bewerbung: ReviewedBewerbungContent
    cv: TailoredCVDraft
    match: CandidateJobMatch
    job_title: str | None
    job_company: str | None
    letter_warnings: tuple[str, ...] = ()


@dataclass
class _Document:
    lines: list[str] = field(default_factory=list)
    fragments: list[str] = field(default_factory=list)

    def line(self, text: str = "") -> None:
        self.lines.append(text)

    def value(
        self,
        label: str,
        raw: str | None,
        *,
        missing: str | None = None,
        multiline: bool = False,
        indent: str = "",
    ) -> None:
        """Render one source value completely (normalized, never capped) and
        register it for the completeness check."""
        text = normalize_multiline(raw) if multiline else normalize_scalar(raw)
        if text:
            self.fragments.append(text)
        elif missing is None:
            return
        else:
            text = missing
        first, *rest = text.split("\n")
        self.lines.append(f"{indent}{label}{first}")
        self.lines.extend(f"{indent}  {line}" if line else "" for line in rest)


def _date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _period(start: date | None, end: date | None, *, current: bool = False) -> str:
    end_text = "heute" if current and end is None else (_date(end) or "nicht angegeben")
    return f"{_date(start) or 'nicht angegeben'} – {end_text}"


def _requirements(doc: _Document, title: str, requirements: list[RequirementMatch]) -> None:
    doc.line(title)
    if not requirements:
        doc.line("• keine")
    for requirement in requirements:
        doc.value("• ", requirement.requirement, missing="(ohne Bezeichnung)")


def _list(doc: _Document, label: str, values: list[str], indent: str) -> None:
    for value in values:
        doc.value(f"{label}", value, indent=indent)


def _section_header(doc: _Document, snapshot: ReviewSnapshot) -> None:
    header = snapshot.cv.header
    doc.line("Kopfzeile:")
    first = header.first_name.value if header.first_name else None
    last = header.last_name.value if header.last_name else None
    doc.value("Vorname: ", first, missing="nicht angegeben")
    doc.value("Nachname: ", last, missing="nicht angegeben")
    doc.value(
        "Berufsbezeichnung: ",
        snapshot.reviewed_cv.professional_title.value,
        missing="keine",
    )
    doc.value("Ort: ", header.location_city.value if header.location_city else None, missing="—")
    doc.value(
        "Land: ",
        header.location_country.value if header.location_country else None,
        missing="—",
    )
    doc.line("Kontakt: Kontaktdaten sind nicht Teil dieses Entwurfs.")


def _section_summary(doc: _Document, snapshot: ReviewSnapshot) -> None:
    doc.line("Profilzusammenfassung:")
    doc.value(
        "",
        snapshot.reviewed_cv.professional_summary.value,
        missing="Keine bestätigte Profilzusammenfassung vorhanden",
        multiline=True,
    )


def _section_skills(doc: _Document, snapshot: ReviewSnapshot) -> None:
    doc.line("Skills:")
    if not snapshot.cv.skills:
        doc.line("• keine")
    for skill in snapshot.cv.skills:
        doc.value("• ", skill.text, missing="(ohne Bezeichnung)")
        doc.value("Kategorie: ", skill.category, indent="  ")
        doc.value("Niveau: ", skill.proficiency, indent="  ")
        if skill.years_experience is not None:
            doc.line(f"  Erfahrung: {skill.years_experience:g} Jahre")


def _section_experience(doc: _Document, snapshot: ReviewSnapshot) -> None:
    doc.line("Berufserfahrung:")
    if not snapshot.cv.experience:
        doc.line("• keine")
    for item in snapshot.cv.experience:
        doc.value("• ", item.job_title, missing="(ohne Titel)")
        doc.value("Firma: ", item.company, indent="  ")
        doc.line(f"  Zeitraum: {_period(item.start_date, item.end_date, current=item.is_current)}")
        doc.value("Ort: ", item.location, indent="  ")
        doc.value("Beschreibung: ", item.description, multiline=True, indent="  ")
        _list(doc, "Aufgabe: ", item.responsibilities, "  ")
        _list(doc, "Erfolg: ", item.achievements, "  ")
        _list(doc, "Technologie: ", item.technologies, "  ")
        _list(doc, "Passender Skill: ", item.matched_skills, "  ")


def _section_projects(doc: _Document, snapshot: ReviewSnapshot) -> None:
    doc.line("Projekte:")
    if not snapshot.cv.projects:
        doc.line("• keine")
    for item in snapshot.cv.projects:
        doc.value("• ", item.name, missing="(ohne Namen)")
        doc.value("Rolle: ", item.role, indent="  ")
        if item.start_date or item.end_date:
            doc.line(f"  Zeitraum: {_period(item.start_date, item.end_date)}")
        doc.value("Beschreibung: ", item.description, multiline=True, indent="  ")
        _list(doc, "Technologie: ", item.technologies, "  ")
        _list(doc, "Highlight: ", item.highlights, "  ")
        doc.value("Repository: ", item.repository_url, indent="  ")
        doc.value("Demo: ", item.demo_url, indent="  ")
        _list(doc, "Passender Skill: ", item.matched_skills, "  ")


def _section_education(doc: _Document, snapshot: ReviewSnapshot) -> None:
    doc.line("Ausbildung:")
    if not snapshot.cv.education:
        doc.line("• keine")
    for item in snapshot.cv.education:
        doc.value("• ", item.institution, missing="(ohne Institution)")
        doc.value("Programm: ", item.program, indent="  ")
        doc.value("Abschluss: ", item.degree, indent="  ")
        doc.value("Fachrichtung: ", item.field_of_study, indent="  ")
        doc.line(f"  Zeitraum: {_period(item.start_date, item.end_date)}")
        doc.line(f"  Abgeschlossen: {'ja' if item.completed else 'nein'}")
        doc.value("Ort: ", item.location, indent="  ")


def _section_certifications(doc: _Document, snapshot: ReviewSnapshot) -> None:
    doc.line("Zertifikate:")
    if not snapshot.cv.certifications:
        doc.line("• keine")
    for item in snapshot.cv.certifications:
        doc.value("• ", item.name, missing="(ohne Namen)")
        doc.value("Aussteller: ", item.issuer, indent="  ")
        doc.value("Ausgestellt: ", _date(item.issued_date), indent="  ")
        doc.value("Gültig bis: ", _date(item.expires_date), indent="  ")
        doc.value("Status: ", item.status, indent="  ")


def _section_languages(doc: _Document, snapshot: ReviewSnapshot) -> None:
    doc.line("Sprachen:")
    if not snapshot.cv.languages:
        doc.line("• keine")
    for item in snapshot.cv.languages:
        doc.value("• ", item.language, missing="(ohne Bezeichnung)")
        doc.value("Niveau: ", item.level, indent="  ")
        doc.value("Zertifikat: ", item.certificate, indent="  ")


_SECTION_RENDERERS = {
    "HEADER": _section_header,
    "SUMMARY": _section_summary,
    "SKILLS": _section_skills,
    "EXPERIENCE": _section_experience,
    "PROJECTS": _section_projects,
    "EDUCATION": _section_education,
    "CERTIFICATIONS": _section_certifications,
    "LANGUAGES": _section_languages,
}


def cv_section_sequence(section_order: list[str]) -> list[str]:
    """The revision's order for known sections, then every remaining known
    section in the fixed trailing order. Unknown names are ignored; no
    known section is ever dropped or rendered twice."""
    ordered: list[str] = []
    for name in section_order:
        if name in _SECTION_RENDERERS and name not in ordered:
            ordered.append(name)
    ordered.extend(name for name in CV_SECTIONS if name not in ordered)
    return ordered


def _build_document(snapshot: ReviewSnapshot) -> _Document:
    doc = _Document()  # every page header already carries REVIEW_STATUS_LINE
    doc.value("Position: ", snapshot.job_title, missing="nicht angegeben")
    doc.value("Firma: ", snapshot.job_company, missing="nicht angegeben")
    doc.line("Freigeben sendet nichts: keine Bewerbung, keine E-Mail, kein Gmail-Entwurf.")
    doc.line()

    match = snapshot.match
    doc.line("KANDIDATEN-MATCH (Profil vs. Stelle)")
    doc.line(f"Kandidaten-Match (0-100): {match.overall_score}")
    _requirements(doc, "Belegte Anforderungen:", match.matched_requirements)
    _requirements(doc, "Teilweise belegt:", match.partial_requirements)
    _requirements(doc, "Nicht belegt (wird nicht behauptet):", match.missing_requirements)
    _requirements(doc, "Unklar (nicht belegt):", match.unknown_requirements)
    doc.line()

    letter = snapshot.reviewed_bewerbung
    doc.line("ANSCHREIBEN")
    doc.value("Betreff: ", letter.subject.value, missing="(kein Betreff)")
    doc.value("", letter.salutation.value, multiline=True)
    doc.line()
    doc.value("", letter.opening.value, multiline=True)
    for paragraph in letter.body_paragraphs:
        doc.line()
        doc.value("", paragraph.text, multiline=True)
    doc.line()
    doc.value("", letter.closing.value, multiline=True)
    doc.value(
        "",
        letter.signature_name.value,
        missing="[Name fehlt - kein bestätigter Name im Profil]",
    )
    doc.line()

    doc.line("LEBENSLAUF")
    for name in cv_section_sequence(snapshot.reviewed_cv.section_order.value):
        _SECTION_RENDERERS[name](doc, snapshot)
        doc.line()

    doc.line("Hinweise:")
    if match.missing_requirements or match.unknown_requirements:
        doc.line("• Entwurf unvollständig: nicht alle Anforderungen sind belegt.")
    for code in (*snapshot.letter_warnings, *snapshot.cv.warnings, *match.warnings):
        doc.value("• Hinweis: ", code)
    doc.line("• Kontaktdaten sind nicht Teil dieses Entwurfs.")
    doc.line()
    doc.line(NOTHING_SENT_FOOTER)
    return doc


# --- pagination ------------------------------------------------------------


def _page_header(page: int, total: int, label: str | None) -> str:
    lines = [f"📋 Prüfdokument - Seite {page}/{total}", REVIEW_STATUS_LINE]
    if label:
        lines.append(label)
    return "\n".join(lines) + "\n\n"


def _page_budget() -> int:
    widest = _page_header(MAX_REVIEW_PAGES, MAX_REVIEW_PAGES, STALE_PACKAGE_LABEL)
    return TELEGRAM_UNIT_LIMIT - utf16_units(widest)


def _hard_split(text: str, budget: int) -> list[str]:
    """Split by code point so that every chunk is within `budget` UTF-16
    units. Never drops or reorders a character."""
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        width = utf16_units(char)
        if used + width > budget and current:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += width
    if current:
        chunks.append("".join(current))
    return chunks


def _split_line(line: str, budget: int) -> list[str]:
    if utf16_units(line) <= budget:
        return [line]
    chunks: list[str] = []
    current = ""
    for word in line.split(" "):
        candidate = f"{current} {word}" if current else word
        if utf16_units(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if utf16_units(word) > budget:
            *full, word = _hard_split(word, budget)
            chunks.extend(full)
        current = word
    if current:
        chunks.append(current)
    return chunks


def _paginate_lines(lines: list[str], budget: int) -> list[str]:
    bodies: list[str] = []
    current: list[str] = []
    used = 0
    for line in (piece for raw in lines for piece in _split_line(raw, budget)):
        if not current and not line:
            continue  # no leading blank line on a page
        width = utf16_units(line) + (1 if current else 0)
        if current and used + width > budget:
            bodies.append("\n".join(current).rstrip("\n"))
            current, used = [], 0
            if not line:
                continue
            width = utf16_units(line)
        current.append(line)
        used += width
    if current:
        bodies.append("\n".join(current).rstrip("\n"))
    return bodies


def _squash(text: str) -> str:
    return "".join(text.split())


@dataclass(frozen=True)
class ReviewDocument:
    """Deterministic page bodies of the complete review document, or
    `too_large`/`incomplete` (then nothing may be offered for decision)."""

    bodies: tuple[str, ...]
    too_large: bool
    complete: bool

    @property
    def decidable(self) -> bool:
        return self.complete and not self.too_large and bool(self.bodies)

    @property
    def total(self) -> int:
        return len(self.bodies)


def render_review_document(snapshot: ReviewSnapshot) -> ReviewDocument:
    """The COMPLETE review document, paginated. More than
    `MAX_REVIEW_PAGES` pages is `too_large` -- never truncated. Every
    rendered source value must appear in the pages (completeness check)."""
    doc = _build_document(snapshot)
    bodies = _paginate_lines(doc.lines, _page_budget())
    if len(bodies) > MAX_REVIEW_PAGES:
        return ReviewDocument((), too_large=True, complete=False)
    joined = _squash("".join(bodies))
    complete = all(_squash(fragment) in joined for fragment in doc.fragments)
    return ReviewDocument(tuple(bodies), too_large=False, complete=complete)


def review_page_text(document: ReviewDocument, page: int, *, stale: bool) -> str:
    label = STALE_PACKAGE_LABEL if stale else None
    return _page_header(page, document.total, label) + document.bodies[page - 1]


# --- callback data ---------------------------------------------------------


def build_request_callback_data(package_token: str) -> str:
    return f"{REQUEST_PREFIX}:{package_token}"


def build_page_callback_data(capability: str, page: int) -> str:
    return f"{PAGE_PREFIX}:{capability}:{page}"


def build_decision_callback_data(capability: str, action: str) -> str:
    return f"{DECISION_PREFIX}:{capability}:{action}"


def parse_request_callback_data(data: str | None) -> str | None:
    """Strictly parse `ba:<package_token>`; anything else is None."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != REQUEST_PREFIX:
        return None
    return parts[1] if PACKAGE_TOKEN_PATTERN.fullmatch(parts[1]) else None


def parse_page_callback_data(data: str | None) -> tuple[str, int] | None:
    """Strictly parse `bv:<capability>:<page 1..MAX_REVIEW_PAGES>`."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != PAGE_PREFIX:
        return None
    if not APPROVAL_CAPABILITY_PATTERN.fullmatch(parts[1]):
        return None
    if not _PAGE_PATTERN.fullmatch(parts[2]):
        return None
    page = int(parts[2])
    return (parts[1], page) if page <= MAX_REVIEW_PAGES else None


def parse_decision_callback_data(data: str | None) -> tuple[str, str] | None:
    """Strictly parse `bz:<capability>:f|x`."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != DECISION_PREFIX:
        return None
    if not APPROVAL_CAPABILITY_PATTERN.fullmatch(parts[1]) or parts[2] not in _DECISION_ACTIONS:
        return None
    return parts[1], parts[2]


def review_page_keyboard(capability: str, page: int, total: int, mode: str | None) -> dict | None:
    """◀/▶ navigation; decision buttons ONLY on the final page, and only
    for a decidable display mode."""
    rows: list[list[dict]] = []
    nav: list[dict] = []
    if page > 1:
        nav.append({"text": "◀", "callback_data": build_page_callback_data(capability, page - 1)})
    if page < total:
        nav.append({"text": "▶", "callback_data": build_page_callback_data(capability, page + 1)})
    if nav:
        rows.append(nav)
    if page == total and mode == MODE_DECIDE:
        rows.append(
            [
                {
                    "text": "✅ Freigeben",
                    "callback_data": build_decision_callback_data(capability, APPROVE_ACTION),
                },
                {
                    "text": "❌ Ablehnen",
                    "callback_data": build_decision_callback_data(capability, REJECT_ACTION),
                },
            ]
        )
    elif page == total and mode == MODE_REJECT_ONLY:
        rows.append(
            [
                {
                    "text": "❌ Ablehnen",
                    "callback_data": build_decision_callback_data(capability, REJECT_ACTION),
                }
            ]
        )
    return {"inline_keyboard": rows} if rows else None


# --- orchestration ---------------------------------------------------------

logger = logging.getLogger(__name__)

Sender = Callable[..., Awaitable]

PENDING = "PENDING_REVIEW"
DECISION_NOTE = "Telegram Stage 9C"
# Complete-operation retries: a lost link race (the winner is then reused),
# a capability collision (a new token), or a SQLite busy/locked error.
_MAX_OPERATION_ATTEMPTS = 3

_NOTHING_SENT = "Es wurde nichts gesendet."
_NOTHING_DONE = "Es wurde nichts freigegeben oder gesendet."
_NOT_SENT_DETAIL = (
    "Es wurde keine Bewerbung und keine E-Mail gesendet und kein Gmail-Entwurf erstellt."
)

NOTICES: dict[str, str] = {
    "UNKNOWN_CAPABILITY": "Unbekannte oder abgelaufene Prüfung. " + _NOTHING_DONE,
    "PACKAGE_EXPIRED": (
        "Dieses Paket ist nicht mehr aktuell, weil der Entwurf inzwischen neu vorbereitet "
        "wurde. " + _NOTHING_DONE
    ),
    "PACKAGE_REPLACED": (
        "Das Paket wurde inzwischen ersetzt. Diese Prüfung kann nicht mehr freigegeben "
        "werden. " + _NOTHING_DONE
    ),
    "PACKAGE_STALE": (
        "Profil oder Stelle haben sich geändert. Dieses Paket kann nicht freigegeben werden; "
        'bitte "Bewerbung erstellen" erneut ausführen. ' + _NOTHING_DONE
    ),
    "PACKAGE_UNAVAILABLE": "Das Paket ist nicht mehr vollständig verfügbar. " + _NOTHING_DONE,
    "JOB_NOT_ELIGIBLE": (
        "Für diese Stelle ist keine Freigabe möglich (Status nicht NEW/SAVED). " + _NOTHING_DONE
    ),
    "NO_PROFILE": "Kein Kandidatenprofil vorhanden. " + _NOTHING_DONE,
    "REVIEW_TOO_LARGE": (
        "Dieses Paket ist zu umfangreich, um es in Telegram vollständig zu prüfen. "
        "Bitte über die API prüfen. " + _NOTHING_DONE
    ),
    "REVIEW_INCOMPLETE": (
        "Das Prüfdokument konnte nicht vollständig dargestellt werden; eine Freigabe in "
        "Telegram ist nicht möglich. Bitte über die API prüfen. " + _NOTHING_DONE
    ),
    "REVIEW_CHANGED": (
        "Diese Prüfung wurde außerhalb von Telegram geändert. Die hier gezeigte Fassung kann "
        "in Telegram weder freigegeben noch abgelehnt werden; bitte über die API prüfen. "
        + _NOTHING_SENT
    ),
    "DISPLAY_FAILED": (
        'Das Prüfdokument konnte nicht angezeigt werden. Bitte erneut "Zur Prüfung" drücken. '
        + _NOTHING_DONE
    ),
    "DISPLAY_UNCERTAIN": (
        "Das Prüfdokument wurde möglicherweise nicht zugestellt. Bitte bei Bedarf erneut "
        '"Zur Prüfung" drücken. ' + _NOTHING_DONE
    ),
    "INVALID_PAGE": "Diese Seite gibt es nicht.",
    "BUSY": "Gerade beschäftigt, bitte erneut versuchen. " + _NOTHING_DONE,
    "APPROVED": "✅ FREIGEGEBEN — NOCH NICHT GESENDET. " + _NOT_SENT_DETAIL,
    "ALREADY_APPROVED": "Bereits FREIGEGEBEN — NOCH NICHT GESENDET. " + _NOT_SENT_DETAIL,
    "APPROVED_ELSEWHERE": (
        "Diese Prüfung wurde außerhalb von Telegram in einer anderen Revision freigegeben; "
        "die hier gezeigte Revision wurde NICHT freigegeben. " + _NOTHING_SENT
    ),
    "REJECTED": "❌ ABGELEHNT — NICHT GESENDET.",
    "ALREADY_REJECTED": "Bereits ABGELEHNT — NICHT GESENDET.",
}

_REPLACED_SUFFIX = (
    " Das Paket wurde inzwischen ersetzt; diese frühere Freigabe gilt nicht für das neue Paket."
)


@dataclass(frozen=True)
class ApprovalOutcome:
    """What happened, plus an optional short notice for the operator's chat
    (no candidate data)."""

    code: str
    notice: str | None = None


def _outcome(code: str, *, suffix: str = "") -> ApprovalOutcome:
    notice = NOTICES.get(code)
    return ApprovalOutcome(code, notice + suffix if notice else None)


@dataclass(frozen=True)
class _CapturedPackage:
    """The exact Stage 9B package a `ba:` capability authorized, captured
    once at token lookup and never rebuilt from a later read."""

    preparation_id: int
    generation: int
    package_token: str
    match_id: int
    cv_draft_id: int
    bewerbung_draft_id: int
    preview_renderer_version: str | None

    @classmethod
    def of(cls, prep: TelegramBewerbungPreparationRecord) -> "_CapturedPackage":
        return cls(
            prep.id,
            prep.generation,
            prep.package_token,
            prep.match_id,
            prep.cv_draft_id,
            prep.bewerbung_draft_id,
            prep.preview_renderer_version,
        )

    def matches(self, prep: TelegramBewerbungPreparationRecord | None) -> bool:
        return prep is not None and prep.state == "PREPARED" and _CapturedPackage.of(prep) == self


@dataclass(frozen=True)
class _ReviewView:
    """A detached display of the exact bound revision."""

    capability: str
    snapshot: ReviewSnapshot
    mode: str


# --- shared reads/checks (inside a transaction) -----------------------------


def _vacancy_review(db: Session, review_id: int) -> TelegramVacancyReviewRecord | None:
    return db.scalar(
        select(TelegramVacancyReviewRecord)
        .where(TelegramVacancyReviewRecord.id == review_id)
        .execution_options(populate_existing=True)
    )


def _review_header(db: Session, review_id: int) -> ApplicationPackageReviewRecord | None:
    """Fresh, unlocked read (for reporting only -- never decision authority)."""
    return db.scalar(
        select(ApplicationPackageReviewRecord)
        .where(ApplicationPackageReviewRecord.id == review_id)
        .execution_options(populate_existing=True)
    )


def _package_problem(
    db: Session,
    *,
    prep: TelegramBewerbungPreparationRecord | None,
    job: JobRecord | None,
    profile: CandidateProfileRecord | None,
    generation: int,
    package_token: str,
    pins: tuple[int, int, int],
    link_identity: str | None,
    replaced_code: str,
) -> str | None:
    """None if `prep` is still exactly the current, eligible, fresh package
    for `pins` (match, CV, letter); otherwise the failure code. Whether the
    objects were read under locks is the caller's responsibility."""
    if prep is None:
        return replaced_code
    if (
        prep.state != "PREPARED"
        or prep.generation != generation
        or prep.package_token != package_token
        or (prep.match_id, prep.cv_draft_id, prep.bewerbung_draft_id) != pins
    ):
        return replaced_code
    vacancy = _vacancy_review(db, prep.review_id)
    if vacancy is None or job is None or vacancy.job_id != job.id:
        return replaced_code
    if job.status not in ELIGIBLE_JOB_STATUSES:
        return "JOB_NOT_ELIGIBLE"
    if profile is None:
        return "NO_PROFILE"
    # The runtime freshness authority: matcher/adapter/generator/provider/
    # renderer versions, profile version, fingerprint and exact title/company.
    current_identity = compute_input_identity(job, profile.profile_version)
    if current_identity != prep.input_identity:
        return "PACKAGE_STALE"
    if link_identity is not None and current_identity != link_identity:
        return "PACKAGE_STALE"
    match = get_match_by_id(db, pins[0])
    cv = get_draft_by_id(db, pins[1])
    letter = get_bewerbung_draft_by_id(db, pins[2])
    if match is None or cv is None or letter is None:
        return "PACKAGE_UNAVAILABLE"
    consistent = (
        match.job_id == job.id
        and cv.job_id == job.id
        and letter.job_id == job.id
        and cv.match_id == match.id
        and letter.cv_draft_id == cv.id
        and letter.match_id == match.id
    )
    if not consistent:
        return "PACKAGE_UNAVAILABLE"
    if cv.candidate_profile_version != profile.profile_version:
        return "PACKAGE_STALE"
    if not bewerbung_draft_is_current(letter, cv, job):
        return "PACKAGE_STALE"
    return None


def _package_current_unlocked(
    db: Session, link: TelegramBewerbungApprovalRecord, review: ApplicationPackageReviewRecord
) -> bool:
    """Display-only currency (which buttons to offer). Decisions re-check
    everything under locks."""
    db.expire_all()
    prep = db.get(TelegramBewerbungPreparationRecord, link.preparation_id)
    job = db.get(JobRecord, review.job_id)
    profile = get_candidate_profile(db)
    problem = _package_problem(
        db,
        prep=prep,
        job=job,
        profile=profile,
        generation=link.generation,
        package_token=link.package_token,
        pins=(review.match_id, review.cv_draft_id, review.bewerbung_draft_id),
        link_identity=link.input_identity,
        replaced_code="PACKAGE_REPLACED",
    )
    return problem is None


def _approved_revision_is_bound(
    db: Session, review: ApplicationPackageReviewRecord, link: TelegramBewerbungApprovalRecord
) -> bool:
    if review.approved_revision_id is None:
        return False
    revision = get_revision_by_id(db, review.approved_revision_id)
    return (
        revision is not None
        and revision.review_id == review.id
        and revision.revision_number == link.bound_review_version
        and review.review_version == link.bound_review_version
    )


def _terminal_outcome(
    db: Session,
    review: ApplicationPackageReviewRecord,
    link: TelegramBewerbungApprovalRecord,
    *,
    package_current: bool | None,
) -> ApprovalOutcome | None:
    """Authoritative report of a terminal Stage 6E state. APPROVED counts
    as THIS Telegram revision's approval only if the approved revision is
    exactly the bound one."""
    if review.status == "REJECTED":
        return _outcome("ALREADY_REJECTED")
    if review.status == "APPROVED":
        if not _approved_revision_is_bound(db, review, link):
            return _outcome("APPROVED_ELSEWHERE")
        suffix = _REPLACED_SUFFIX if package_current is False else ""
        return _outcome("ALREADY_APPROVED", suffix=suffix)
    return None


def _revision_unchanged(
    review: ApplicationPackageReviewRecord, link: TelegramBewerbungApprovalRecord
) -> bool:
    return (
        review.status == PENDING
        and review.review_version == link.bound_review_version
        and not review.has_manual_overrides
    )


def _build_view(
    db: Session,
    link: TelegramBewerbungApprovalRecord,
    review: ApplicationPackageReviewRecord | None,
    *,
    package_current: bool,
) -> "_ReviewView | ApprovalOutcome":
    """The detached snapshot of EXACTLY `(review, bound_review_version)`,
    or the outcome explaining why it cannot be shown for decision."""
    if review is None or review.id != link.review_id:
        return _outcome("PACKAGE_UNAVAILABLE")
    terminal = _terminal_outcome(db, review, link, package_current=package_current)
    if terminal is not None:
        return terminal
    if not _revision_unchanged(review, link):
        return _outcome("REVIEW_CHANGED")
    revision = get_bound_revision(db, review.id, link.bound_review_version)
    if (
        revision is None
        or revision.review_id != review.id
        or revision.revision_number != link.bound_review_version
    ):
        return _outcome("PACKAGE_UNAVAILABLE")
    cv = get_draft_by_id(db, review.cv_draft_id)
    match = get_match_by_id(db, review.match_id)
    letter = get_bewerbung_draft_by_id(db, review.bewerbung_draft_id)
    if cv is None or match is None or letter is None:
        return _outcome("PACKAGE_UNAVAILABLE")
    letter_model = to_bewerbung_draft(letter)
    context = letter_model.job_context
    snapshot = ReviewSnapshot(
        reviewed_cv=ReviewedCVContent.model_validate_json(revision.reviewed_cv_json),
        reviewed_bewerbung=ReviewedBewerbungContent.model_validate_json(
            revision.reviewed_bewerbung_json
        ),
        cv=to_tailored_cv_draft(cv),
        match=to_candidate_job_match(match),
        job_title=context.title if context else None,
        job_company=context.company if context else None,
        letter_warnings=tuple(letter_model.warnings),
    )
    mode = MODE_DECIDE if package_current else MODE_REJECT_ONLY
    return _ReviewView(link.approval_capability, snapshot, mode)


def _document_problem(document: ReviewDocument) -> str | None:
    if document.too_large:
        return "REVIEW_TOO_LARGE"
    if not document.complete or not document.bodies:
        return "REVIEW_INCOMPLETE"
    return None


def _is_busy(exc: OperationalError) -> bool:
    message = str(exc.orig).lower()
    return "locked" in message or "busy" in message


def _run_operation(session_factory: Callable[[], Session], operation):
    """Run `operation(db)` in a fresh session, retrying the COMPLETE
    operation after a lost link race, a capability collision or a SQLite
    busy error (always after a full rollback). Any other integrity error is
    re-raised -- it is never taken as proof that another worker won."""
    for _ in range(_MAX_OPERATION_ATTEMPTS):
        db = session_factory()
        try:
            return operation(db)
        except IntegrityError as exc:
            db.rollback()
            if classify_link_conflict(exc) not in (GENERATION_CONFLICT, CAPABILITY_CONFLICT):
                raise
        except OperationalError as exc:
            db.rollback()
            if not _is_busy(exc):
                raise
        finally:
            db.close()
    return _outcome("BUSY")


# --- request: create or reuse the linked review ------------------------------


def _request_operation(package_token: str):
    def operation(db: Session) -> "_ReviewView | ApprovalOutcome":
        prep = get_preparation_by_package_token(db, package_token)
        if prep is None or prep.state != "PREPARED" or prep.package_token != package_token:
            return _outcome("PACKAGE_EXPIRED")
        captured = _CapturedPackage.of(prep)
        vacancy = _vacancy_review(db, prep.review_id)
        if vacancy is None:
            return _outcome("PACKAGE_EXPIRED")
        job_id = vacancy.job_id
        db.rollback()  # end the preflight read; authoritative checks follow under locks
        db.expire_all()

        # Lock order: profile -> job -> preparation (-> review header).
        profile = lock_profile_fresh(db)
        job = lock_job_fresh(db, job_id)
        prep = lock_preparation_fresh(db, captured.preparation_id)
        if not captured.matches(prep):
            db.rollback()
            return _outcome("PACKAGE_EXPIRED")
        problem = _package_problem(
            db,
            prep=prep,
            job=job,
            profile=profile,
            generation=captured.generation,
            package_token=captured.package_token,
            pins=(captured.match_id, captured.cv_draft_id, captured.bewerbung_draft_id),
            link_identity=None,
            replaced_code="PACKAGE_EXPIRED",
        )
        if problem is not None:
            db.rollback()
            return _outcome(problem)
        current_identity = prep.input_identity  # == recomputed identity, verified above

        link = get_link_for_generation(db, captured.preparation_id, captured.generation)
        if link is not None:
            if (
                link.input_identity != current_identity
                or link.package_token != captured.package_token
            ):
                db.rollback()
                return _outcome("PACKAGE_STALE")
        else:
            try:
                package = ReviewPackageService().create(
                    db, job, captured.cv_draft_id, captured.bewerbung_draft_id, commit=False
                )
            except (
                ReviewCVDraftNotFoundError,
                ReviewBewerbungDraftNotFoundError,
                ReviewCVDraftJobMismatchError,
                ReviewBewerbungDraftJobMismatchError,
                ReviewSourceMismatchError,
                ReviewCurrentProfileMissingError,
                ReviewProfileChangedError,
                ReviewJobChangedError,
            ):
                db.rollback()
                return _outcome("PACKAGE_STALE")
            pins = (package.match_id, package.cv_draft_id, package.bewerbung_draft_id)
            expected_pins = (captured.match_id, captured.cv_draft_id, captured.bewerbung_draft_id)
            if pins != expected_pins or package.review_version != 1 or package.status != PENDING:
                db.rollback()  # fail closed: never link a review with other pins
                return _outcome("PACKAGE_UNAVAILABLE")
            link = insert_link(
                db,
                preparation_id=captured.preparation_id,
                generation=captured.generation,
                package_token=captured.package_token,
                input_identity=current_identity,
                review_id=package.id,
                approval_capability=new_approval_capability(),
            )
            logger.info(
                "telegram_review_created link_id=%s review_id=%s generation=%s",
                link.id,
                package.id,
                captured.generation,
            )

        review = lock_review_header_fresh(db, link.review_id)
        view = _build_view(db, link, review, package_current=True)
        db.commit()  # review + revision 1 + link together (a no-op for reuse)
        return view

    return operation


# --- page: display the exact bound revision ---------------------------------


def _page_operation(capability: str):
    def operation(db: Session) -> "_ReviewView | ApprovalOutcome":
        link = get_link_by_capability(db, capability)
        if link is None:
            return _outcome("UNKNOWN_CAPABILITY")
        header = _review_header(db, link.review_id)
        if header is None:
            return _outcome("PACKAGE_UNAVAILABLE")
        package_current = _package_current_unlocked(db, link, header)
        review = lock_review_header_fresh(db, link.review_id)
        view = _build_view(db, link, review, package_current=package_current)
        db.commit()
        return view

    return operation


async def _display(settings, view: _ReviewView, page: int, send: Sender) -> ApprovalOutcome:
    """Send one page of the complete review document. Best effort and
    repeatable: no DB transaction is open here and no business state
    depends on the result."""
    document = render_review_document(view.snapshot)
    problem = _document_problem(document)
    if problem is not None:
        return _outcome(problem)
    if page > document.total:
        return _outcome("INVALID_PAGE")
    text = review_page_text(document, page, stale=view.mode == MODE_REJECT_ONLY)
    markup = review_page_keyboard(view.capability, page, document.total, view.mode)
    try:
        result = await send(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            text,
            reply_markup=markup,
            timeout_seconds=settings.telegram_timeout_seconds,
        )
    except Exception as exc:  # the request may have reached Telegram
        logger.warning("telegram_review_display_error error_type=%s", type(exc).__name__)
        return _outcome("DISPLAY_UNCERTAIN")
    if result.outcome is TelegramSendOutcome.SENT:
        return ApprovalOutcome("REVIEW_SHOWN")
    if result.outcome is TelegramSendOutcome.FAILED:
        return _outcome("DISPLAY_FAILED")
    return _outcome("DISPLAY_UNCERTAIN")


async def handle_request_review(
    session_factory: Callable[[], Session],
    settings,
    package_token: str,
    *,
    send: Sender = send_telegram_message,
) -> ApprovalOutcome:
    """The "✅ Zur Prüfung" press (already authorized, private, flags on).
    Creates or reuses exactly one Stage 6E review for this exact Stage 9B
    generation, then shows page 1 of its complete review document."""
    result = _run_operation(session_factory, _request_operation(package_token))
    if isinstance(result, ApprovalOutcome):
        logger.info("telegram_review_request result=%s", result.code)
        return result
    outcome = await _display(settings, result, 1, send)
    logger.info("telegram_review_request result=%s", outcome.code)
    return outcome


async def handle_review_page(
    session_factory: Callable[[], Session],
    settings,
    capability: str,
    page: int,
    *,
    send: Sender = send_telegram_message,
) -> ApprovalOutcome:
    """One page of the exact bound revision's complete review document."""
    result = _run_operation(session_factory, _page_operation(capability))
    if isinstance(result, ApprovalOutcome):
        return result
    return await _display(settings, result, page, send)


# --- decisions ----------------------------------------------------------------


def _reported_terminal(db: Session, link: TelegramBewerbungApprovalRecord) -> ApprovalOutcome:
    """Fresh authoritative report (after a lost CAS, or for a replay)."""
    db.rollback()
    db.expire_all()
    review = _review_header(db, link.review_id)
    if review is None:
        return _outcome("PACKAGE_UNAVAILABLE")
    package_current = (
        _package_current_unlocked(db, link, review) if review.status == "APPROVED" else None
    )
    terminal = _terminal_outcome(db, review, link, package_current=package_current)
    db.rollback()
    return terminal or _outcome("REVIEW_CHANGED")


def _decidable(
    db: Session,
    link: TelegramBewerbungApprovalRecord,
    review: ApplicationPackageReviewRecord,
    *,
    package_current: bool,
) -> ApprovalOutcome | None:
    """None if the exact bound revision is still pending, unchanged and
    COMPLETELY displayable; otherwise the refusing outcome. A decision is
    never accepted for content Telegram cannot show in full."""
    view = _build_view(db, link, review, package_current=package_current)
    if isinstance(view, ApprovalOutcome):
        return view
    problem = _document_problem(render_review_document(view.snapshot))
    return _outcome(problem) if problem else None


def _approve(db: Session, link: TelegramBewerbungApprovalRecord, job_id: int) -> ApprovalOutcome:
    db.rollback()
    db.expire_all()
    # Lock order: profile -> job -> preparation -> review header -> revision.
    profile = lock_profile_fresh(db)
    job = lock_job_fresh(db, job_id)
    prep = lock_preparation_fresh(db, link.preparation_id)
    review = lock_review_header_fresh(db, link.review_id)
    if review is None:
        db.rollback()
        return _outcome("PACKAGE_UNAVAILABLE")
    if review.status != PENDING:
        return _reported_terminal(db, link)
    if not _revision_unchanged(review, link):
        db.rollback()
        return _outcome("REVIEW_CHANGED")
    problem = _package_problem(
        db,
        prep=prep,
        job=job,
        profile=profile,
        generation=link.generation,
        package_token=link.package_token,
        pins=(review.match_id, review.cv_draft_id, review.bewerbung_draft_id),
        link_identity=link.input_identity,
        replaced_code="PACKAGE_REPLACED",
    )
    if problem is not None:
        db.rollback()
        return _outcome(problem)
    refusal = _decidable(db, link, review, package_current=True)
    if refusal is not None:
        db.rollback()
        return refusal
    bound_revision = get_bound_revision(db, review.id, link.bound_review_version)
    review_id = review.id
    try:
        approved = ReviewPackageService().approve(
            db,
            review_id,
            link.bound_review_version,
            acknowledge_manual_overrides=False,
            decision_note=DECISION_NOTE,
        )
    except (ReviewNotPendingError, ReviewNotFoundError):
        return _reported_terminal(db, link)
    except (ReviewVersionConflictError, ReviewManualOverrideAcknowledgmentRequiredError):
        db.rollback()
        return _outcome("REVIEW_CHANGED")
    except (
        ReviewProfileChangedError,
        ReviewJobChangedError,
        ReviewCurrentProfileMissingError,
        ReviewCurrentJobMissingError,
    ):
        db.rollback()
        return _outcome("PACKAGE_STALE")
    if approved.approved_revision_id != bound_revision.id:  # 6E invariant; fail loudly
        logger.error("telegram_review_approved_unexpected_revision review_id=%s", review_id)
        return _outcome("APPROVED_ELSEWHERE")
    logger.info(
        "telegram_review_decision action=approve link_id=%s review_id=%s generation=%s",
        link.id,
        review_id,
        link.generation,
    )
    return _outcome("APPROVED")


def _reject(db: Session, link: TelegramBewerbungApprovalRecord) -> ApprovalOutcome:
    """Explicit rejection of the exact bound revision. Package/profile/job
    freshness is deliberately NOT required (a stale package may be
    rejected); only the review header (then its revision) is locked."""
    db.rollback()
    db.expire_all()
    review = lock_review_header_fresh(db, link.review_id)
    if review is None:
        db.rollback()
        return _outcome("PACKAGE_UNAVAILABLE")
    if review.status != PENDING:
        return _reported_terminal(db, link)
    if not _revision_unchanged(review, link):
        db.rollback()
        return _outcome("REVIEW_CHANGED")
    refusal = _decidable(db, link, review, package_current=False)
    if refusal is not None:
        db.rollback()
        return refusal
    review_id = review.id
    try:
        ReviewPackageService().reject(
            db, review_id, link.bound_review_version, decision_note=DECISION_NOTE
        )
    except (ReviewNotPendingError, ReviewNotFoundError):
        return _reported_terminal(db, link)
    except ReviewVersionConflictError:
        db.rollback()
        return _outcome("REVIEW_CHANGED")
    logger.info(
        "telegram_review_decision action=reject link_id=%s review_id=%s generation=%s",
        link.id,
        review_id,
        link.generation,
    )
    return _outcome("REJECTED")


def _decision_operation(capability: str, action: str):
    def operation(db: Session) -> ApprovalOutcome:
        link = get_link_by_capability(db, capability)
        if link is None:
            return _outcome("UNKNOWN_CAPABILITY")
        header = _review_header(db, link.review_id)
        if header is None:
            return _outcome("PACKAGE_UNAVAILABLE")
        if header.status != PENDING:  # a replay reports the authoritative state first
            return _reported_terminal(db, link)
        if action == APPROVE_ACTION:
            return _approve(db, link, header.job_id)
        return _reject(db, link)

    return operation


async def handle_decision(
    session_factory: Callable[[], Session],
    settings,
    capability: str,
    action: str,
) -> ApprovalOutcome:
    """✅ Freigeben / ❌ Ablehnen for the exact bound revision. Only the
    Stage 6E decision CAS changes durable state; nothing is sent."""
    if action not in _DECISION_ACTIONS:
        return _outcome("UNKNOWN_CAPABILITY")
    outcome = _run_operation(session_factory, _decision_operation(capability, action))
    logger.info("telegram_review_decision_result action=%s result=%s", action, outcome.code)
    return outcome


# --- exact Stage 9D handoff (read-only contract; Stage 9D is NOT here) -----


@dataclass(frozen=True)
class ApprovedPackageHandoff:
    """The exact object a future Stage 9D must consume -- resolved by link
    id, never by a "latest" lookup. Its existence proves only that THIS
    Telegram-bound revision was approved; current-use freshness must still
    be re-verified by the consumer."""

    link_id: int
    preparation_id: int
    generation: int
    package_token: str
    input_identity: str
    review_id: int
    approved_revision_id: int
    bound_review_version: int
    match_id: int
    cv_draft_id: int
    bewerbung_draft_id: int


def get_approved_handoff(db: Session, link_id: int) -> ApprovedPackageHandoff | None:
    """None unless the linked review is APPROVED and its approved revision
    is exactly the bound revision (same review, revision_number ==
    bound_review_version == review_version). An API approval of another
    revision is NOT a valid Telegram handoff."""
    link = db.get(TelegramBewerbungApprovalRecord, link_id)
    if link is None:
        return None
    review = _review_header(db, link.review_id)
    if review is None or review.status != "APPROVED":
        return None
    if not _approved_revision_is_bound(db, review, link):
        return None
    return ApprovedPackageHandoff(
        link_id=link.id,
        preparation_id=link.preparation_id,
        generation=link.generation,
        package_token=link.package_token,
        input_identity=link.input_identity,
        review_id=review.id,
        approved_revision_id=review.approved_revision_id,
        bound_review_version=link.bound_review_version,
        match_id=review.match_id,
        cv_draft_id=review.cv_draft_id,
        bewerbung_draft_id=review.bewerbung_draft_id,
    )
