"""Stage 14D: the human-readable German CV document for one CV draft.

Pure and deterministic: built ONLY from the pinned `TailoredCVDraft` (which
already carries trusted facts only -- see app.agents.cv_adapter) plus the
operator-confirmed `ApplicationIdentity` for contact lines. Nothing is
inferred, translated, upgraded or filled in:

- a missing date is left out, never estimated; an unfinished education entry
  is labelled "nicht abgeschlossen", never shown as a degree;
- a language with level UNKNOWN is shown as "Niveau nicht angegeben";
- free-text fields (descriptions, responsibilities) are copied verbatim;
- without complete contact data the document carries NO contact lines and
  reports the problem codes in `warnings` (CONTACT_DATA_MISSING, ...).

Sections follow `cv.section_order` exactly. The output is a list of typed
blocks that `app.services.document_export` writes to a file.
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.models.application_identity import ApplicationIdentity, contact_problems_for_names
from app.models.cv_draft import (
    CVEducationItem,
    CVExperienceItem,
    CVProjectItem,
    TailoredCVDraft,
)

CV_DOCUMENT_VERSION = "cv-doc-v1"

BlockKind = Literal["TITLE", "HEADING", "SUBHEADING", "PARAGRAPH", "BULLET"]

_HEADINGS = {
    "SUMMARY": "Profil",
    "SKILLS": "Kenntnisse",
    "EXPERIENCE": "Berufserfahrung",
    "PROJECTS": "Projekte",
    "EDUCATION": "Ausbildung",
    "CERTIFICATIONS": "Zertifikate",
    "LANGUAGES": "Sprachen",
}
_LANGUAGE_LEVELS = {"NATIVE": "Muttersprache", "UNKNOWN": "Niveau nicht angegeben"}
_CERTIFICATION_STATUS = {
    "COMPLETED": "abgeschlossen",
    "IN_PROGRESS": "in Bearbeitung",
    "PLANNED": "geplant",
    "UNKNOWN": "Status nicht angegeben",
}


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str


@dataclass(frozen=True)
class CVDocument:
    blocks: tuple[Block, ...]
    warnings: tuple[str, ...]
    first_name: str | None
    last_name: str | None


def _month(value: date | None) -> str | None:
    return value.strftime("%m/%Y") if value is not None else None


def _period(start: date | None, end: date | None, current: bool) -> str | None:
    start_text, end_text = _month(start), _month(end)
    if current:
        return f"seit {start_text}" if start_text else "aktuell"
    if start_text and end_text:
        return f"{start_text} – {end_text}"
    return start_text or end_text


def _join(*parts: str | None, sep: str = ", ") -> str:
    return sep.join(part for part in parts if part)


def _header(cv: TailoredCVDraft, identity: ApplicationIdentity | None, contact_ok: bool):
    header = cv.header
    first = header.first_name.value if header.first_name else None
    last = header.last_name.value if header.last_name else None
    blocks = [Block("TITLE", "Lebenslauf")]
    name = _join(first, last, sep=" ")
    if name:
        blocks.append(Block("SUBHEADING", name))
    if header.professional_title:
        blocks.append(Block("PARAGRAPH", header.professional_title.value))
    if contact_ok and identity is not None:
        address = _join(identity.street, identity.postal_line, identity.country)
        if address:
            blocks.append(Block("PARAGRAPH", f"Adresse: {address}"))
        blocks.append(Block("PARAGRAPH", f"Telefon: {identity.phone}"))
        blocks.append(Block("PARAGRAPH", f"E-Mail: {identity.email}"))
    else:
        residence = _join(
            header.location_city.value if header.location_city else None,
            header.location_country.value if header.location_country else None,
        )
        if residence:
            blocks.append(Block("PARAGRAPH", f"Wohnort: {residence}"))
    return blocks, first, last


def _experience(item: CVExperienceItem) -> list[Block]:
    blocks = [Block("SUBHEADING", _join(item.job_title, item.company, sep=" – "))]
    meta = _join(_period(item.start_date, item.end_date, item.is_current), item.location)
    if meta:
        blocks.append(Block("PARAGRAPH", meta))
    if item.description:
        blocks.append(Block("PARAGRAPH", item.description))
    blocks += [Block("BULLET", text) for text in (*item.responsibilities, *item.achievements)]
    if item.technologies:
        blocks.append(Block("PARAGRAPH", "Technologien: " + ", ".join(item.technologies)))
    return blocks


def _project(item: CVProjectItem) -> list[Block]:
    blocks = [Block("SUBHEADING", item.name)]
    meta = _join(item.role, _period(item.start_date, item.end_date, False))
    if meta:
        blocks.append(Block("PARAGRAPH", meta))
    if item.description:
        blocks.append(Block("PARAGRAPH", item.description))
    blocks += [Block("BULLET", text) for text in item.highlights]
    if item.technologies:
        blocks.append(Block("PARAGRAPH", "Technologien: " + ", ".join(item.technologies)))
    for label, url in (("Repository", item.repository_url), ("Demo", item.demo_url)):
        if url:
            blocks.append(Block("PARAGRAPH", f"{label}: {url}"))
    return blocks


def _education(item: CVEducationItem) -> list[Block]:
    blocks = [Block("SUBHEADING", item.institution)]
    subject = _join(item.degree, item.program, item.field_of_study)
    if subject:
        blocks.append(Block("PARAGRAPH", subject))
    meta = _join(_period(item.start_date, item.end_date, False), item.location)
    if meta:
        blocks.append(Block("PARAGRAPH", meta))
    if not item.completed:
        blocks.append(Block("PARAGRAPH", "nicht abgeschlossen"))
    return blocks


def _section(cv: TailoredCVDraft, name: str) -> list[Block]:
    body: list[Block] = []
    if name == "SUMMARY" and cv.professional_summary:
        body = [Block("PARAGRAPH", cv.professional_summary.value)]
    elif name == "SKILLS" and cv.skills:
        body = [Block("PARAGRAPH", ", ".join(skill.text for skill in cv.skills))]
    elif name == "EXPERIENCE":
        body = [block for item in cv.experience for block in _experience(item)]
    elif name == "PROJECTS":
        body = [block for item in cv.projects for block in _project(item)]
    elif name == "EDUCATION":
        body = [block for item in cv.education for block in _education(item)]
    elif name == "CERTIFICATIONS":
        body = [
            Block(
                "BULLET",
                _join(item.name, item.issuer, _CERTIFICATION_STATUS.get(item.status, item.status)),
            )
            for item in cv.certifications
        ]
    elif name == "LANGUAGES":
        body = [
            Block("BULLET", f"{item.language} – {_LANGUAGE_LEVELS.get(item.level, item.level)}")
            for item in cv.languages
        ]
    return [Block("HEADING", _HEADINGS[name]), *body] if body else []


def build_cv_document(cv: TailoredCVDraft, identity: ApplicationIdentity | None) -> CVDocument:
    first = cv.header.first_name.value if cv.header.first_name else None
    last = cv.header.last_name.value if cv.header.last_name else None
    problems = contact_problems_for_names(identity, first, last)
    header, first, last = _header(cv, identity, contact_ok=not problems)
    blocks = list(header)
    for name in cv.section_order:
        if name in _HEADINGS:
            blocks += _section(cv, name)
    return CVDocument(
        blocks=tuple(blocks), warnings=tuple(problems), first_name=first, last_name=last
    )
