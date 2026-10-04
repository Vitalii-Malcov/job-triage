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

This first part is pure: the complete review document, its deterministic
pagination with a completeness check, and strict callback parsing.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from app.db.telegram_bewerbung_approval_repository import APPROVAL_CAPABILITY_PATTERN
from app.db.telegram_bewerbung_repository import PACKAGE_TOKEN_PATTERN
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.cv_draft import TailoredCVDraft
from app.models.review_package import ReviewedBewerbungContent, ReviewedCVContent
from app.services.telegram_bewerbung_preview import (
    TELEGRAM_UNIT_LIMIT,
    normalize_multiline,
    normalize_scalar,
    utf16_units,
)

MAX_REVIEW_PAGES = 30

REQUEST_PREFIX = "ba"
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


def request_review_button(package_token: str) -> dict:
    return {"text": "✅ Zur Prüfung", "callback_data": build_request_callback_data(package_token)}


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
