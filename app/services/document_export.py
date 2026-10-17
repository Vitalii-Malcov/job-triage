"""Stage 14D: local application-document files and the attachment plan.

LOCAL FILESYSTEM ONLY. Nothing here sends, uploads, attaches to an e-mail
or contacts anyone; the attachment plan is a description for the human who
applies (portal upload or, later, a reviewed draft), with delivery always
`NOT_SENT`.

- **DOCX without new dependencies.** A minimal WordprocessingML package is
  written with the standard library (`zipfile`), byte-deterministic: fixed
  member order and fixed 1980-01-01 timestamps, so the same document always
  has the same SHA-256. PDF is not produced (no PDF library in the project);
  see docs/STAGE_14D_LIVE_PILOT_READINESS.md.
- **Deterministic, sanitized filenames**: `Lebenslauf_<Vorname>_<Nachname>_
  <Firma>_<Referenz>.docx`; umlauts transliterated, every other character
  outside `[A-Za-z0-9]` collapsed to "-", each part capped.
- **No silent overwrite**: an existing target file is refused
  (`DocumentExistsError`). Only an explicitly flagged dry-run directory
  inside the system temp directory may be overwritten.
"""

import hashlib
import re
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from app.agents.cv_document import Block, build_cv_document
from app.models.application_identity import ApplicationIdentity
from app.models.cv_draft import TailoredCVDraft
from app.models.review_package import ReviewedBewerbungContent
from app.services.gmail_application_draft import render_draft_content

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DELIVERY_NOT_SENT = "NOT_SENT"
APPROVAL_PENDING = "APPROVAL_PENDING"

_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_TRANSLITERATION = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
)
_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9]+")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,180}\.docx$")
# XML 1.0 forbids most C0 controls; tab/newline are not used inside runs.
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_PART_LIMIT = 40


class DocumentExistsError(FileExistsError):
    """The target file already exists and may not be overwritten."""


class UnsafeExportTargetError(ValueError):
    """The export directory or filename is not acceptable."""


# --- filenames ----------------------------------------------------------------


def safe_filename_component(text: str | None, *, limit: int = _PART_LIMIT) -> str:
    folded = unicodedata.normalize("NFKD", (text or "").translate(_TRANSLITERATION))
    ascii_text = folded.encode("ascii", "ignore").decode("ascii")
    return _UNSAFE_RUN.sub("-", ascii_text).strip("-")[:limit].strip("-")


def document_filename(
    prefix: str,
    *,
    first_name: str | None,
    last_name: str | None,
    company: str | None,
    reference: str | None,
) -> str:
    parts = [safe_filename_component(prefix)] + [
        safe_filename_component(value) for value in (first_name, last_name, company, reference)
    ]
    name = "_".join(part for part in parts if part) + ".docx"
    if not _FILENAME_RE.match(name):
        raise UnsafeExportTargetError("generated filename is not safe")
    return name


# --- DOCX -----------------------------------------------------------------------

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _style(style_id: str, name: str, *, size: int, bold: bool = False, extra: str = "") -> str:
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
        f'<w:pPr><w:spacing w:after="80"/>{extra}</w:pPr>'
        f'<w:rPr>{"<w:b/>" if bold else ""}<w:sz w:val="{size}"/></w:rPr></w:style>'
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f"<w:styles {_W}>"
    '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" '
    'w:cs="Calibri"/><w:sz w:val="22"/><w:lang w:val="de-DE"/></w:rPr></w:rPrDefault>'
    "</w:docDefaults>"
    + _style("Normal", "Normal", size=22)
    + _style("Title", "Title", size=36, bold=True)
    + _style("Heading1", "heading 1", size=28, bold=True, extra='<w:spacing w:before="240"/>')
    + _style("Heading2", "heading 2", size=23, bold=True, extra='<w:spacing w:before="120"/>')
    + _style("ListBullet", "List Bullet", size=22, extra='<w:ind w:left="360" w:hanging="240"/>')
    + "</w:styles>"
)
_BLOCK_STYLE = {
    "TITLE": "Title",
    "HEADING": "Heading1",
    "SUBHEADING": "Heading2",
    "PARAGRAPH": "Normal",
    "BULLET": "ListBullet",
}


def _paragraph(block: Block) -> str:
    text = _XML_INVALID.sub("", block.text)
    if block.kind == "BULLET":
        text = "• " + text
    runs = []
    for index, line in enumerate(text.split("\n")):
        if index:
            runs.append("<w:br/>")
        runs.append(f'<w:t xml:space="preserve">{escape(line)}</w:t>')
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{_BLOCK_STYLE[block.kind]}"/></w:pPr>'
        f"<w:r>{''.join(runs)}</w:r></w:p>"
    )


def render_docx(blocks: tuple[Block, ...] | list[Block]) -> bytes:
    body = "".join(_paragraph(block) for block in blocks)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {_W}><w:body>{body}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
        'w:header="709" w:footer="709" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _ROOT_RELS),
            ("word/_rels/document.xml.rels", _DOCUMENT_RELS),
            ("word/document.xml", document),
            ("word/styles.xml", _STYLES),
        ):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content.encode("utf-8"))
    return buffer.getvalue()


def letter_blocks(subject: str, body: str) -> tuple[Block, ...]:
    """The cover letter as document blocks: subject heading, then one
    paragraph per blank-line-separated block of the rendered letter."""
    paragraphs = [part.strip() for part in body.split("\n\n") if part.strip()]
    return (Block("SUBHEADING", subject), *(Block("PARAGRAPH", part) for part in paragraphs))


# --- files ----------------------------------------------------------------------


@dataclass(frozen=True)
class ExportedDocument:
    path: Path
    filename: str
    media_type: str
    size_bytes: int
    sha256: str


def _is_dry_run_directory(directory: Path) -> bool:
    temp_root = Path(tempfile.gettempdir()).resolve()
    resolved = directory.resolve()
    return resolved != temp_root and temp_root in resolved.parents


def write_document(
    content: bytes, directory: Path, filename: str, *, dry_run_directory: bool = False
) -> ExportedDocument:
    """Write `content` as `directory/filename`. Refuses to overwrite an
    existing file unless `dry_run_directory=True` AND `directory` lies
    inside the system temp directory."""
    if not _FILENAME_RE.match(filename):
        raise UnsafeExportTargetError("filename is not a safe .docx name")
    if not directory.is_dir():
        raise UnsafeExportTargetError("export directory does not exist")
    if dry_run_directory and not _is_dry_run_directory(directory):
        raise UnsafeExportTargetError("a dry-run directory must lie inside the system temp dir")
    target = directory / filename
    if dry_run_directory:
        target.write_bytes(content)
    else:
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            raise DocumentExistsError(f"refusing to overwrite existing {filename}") from None
    return ExportedDocument(
        path=target,
        filename=filename,
        media_type=DOCX_MEDIA_TYPE,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


# --- attachment plan --------------------------------------------------------------

AttachmentRole = Literal["CV", "COVER_LETTER"]


@dataclass(frozen=True)
class PlannedAttachment:
    role: AttachmentRole
    document: ExportedDocument


@dataclass(frozen=True)
class AttachmentPlan:
    """What a human would attach/upload for one vacancy. Never executed by
    the system; `delivery` is always NOT_SENT."""

    job_id: int
    vacancy_reference: str | None
    channel: str
    attachments: tuple[PlannedAttachment, ...]
    blockers: tuple[str, ...]
    delivery: str = DELIVERY_NOT_SENT

    @property
    def ready_for_human_handoff(self) -> bool:
        return not self.blockers


def build_attachment_plan(
    *,
    job_id: int,
    vacancy_reference: str | None,
    channel: str,
    attachments: list[PlannedAttachment],
    document_warnings: tuple[str, ...],
    review_status: str | None,
    readiness_blockers: tuple[str, ...] = (),
) -> AttachmentPlan:
    """Blockers, in order: document warnings (e.g. CONTACT_DATA_MISSING),
    APPROVAL_PENDING unless the package is APPROVED, then the failing
    send-readiness check codes. Duplicates are dropped."""
    blockers = list(document_warnings)
    if review_status != "APPROVED":
        blockers.append(APPROVAL_PENDING)
    blockers += readiness_blockers
    return AttachmentPlan(
        job_id=job_id,
        vacancy_reference=vacancy_reference,
        channel=channel,
        attachments=tuple(attachments),
        blockers=tuple(dict.fromkeys(blockers)),
    )


def render_attachment_plan(plan: AttachmentPlan) -> str:
    lines = [
        f"ATTACHMENT PLAN (job_id={plan.job_id}, reference={plan.vacancy_reference or '-'})",
        f"channel: {plan.channel}",
        f"delivery: {plan.delivery}",
    ]
    for item in plan.attachments:
        doc = item.document
        lines.append(
            f"- {item.role}: {doc.filename} ({doc.media_type}, {doc.size_bytes} bytes, "
            f"sha256 {doc.sha256})"
        )
    lines.append("blockers: " + (", ".join(plan.blockers) if plan.blockers else "none"))
    lines.append(f"ready for human handoff: {'YES' if plan.ready_for_human_handoff else 'NO'}")
    return "\n".join(lines)


# --- package --------------------------------------------------------------------


def export_package_documents(
    *,
    cv: TailoredCVDraft,
    reviewed_bewerbung: ReviewedBewerbungContent | None,
    identity: ApplicationIdentity | None,
    company: str | None,
    reference: str | None,
    directory: Path,
    dry_run_directory: bool = False,
) -> tuple[list[PlannedAttachment], tuple[str, ...]]:
    """Write the CV (and, when given, the reviewed cover letter) as local
    DOCX files. Returns the planned attachments and the document warnings
    (contact-data problems, an unrenderable letter) for
    `build_attachment_plan`. Writes files only; sends nothing."""
    document = build_cv_document(cv, identity)
    names = dict(
        first_name=document.first_name,
        last_name=document.last_name,
        company=company,
        reference=reference,
    )
    attachments = [
        PlannedAttachment(
            "CV",
            write_document(
                render_docx(document.blocks),
                directory,
                document_filename("Lebenslauf", **names),
                dry_run_directory=dry_run_directory,
            ),
        )
    ]
    warnings = list(document.warnings)
    if reviewed_bewerbung is None:
        warnings.append("COVER_LETTER_MISSING")
    else:
        rendered = render_draft_content(reviewed_bewerbung)
        if isinstance(rendered, str):
            warnings.append(f"COVER_LETTER_{rendered}")
        else:
            attachments.append(
                PlannedAttachment(
                    "COVER_LETTER",
                    write_document(
                        render_docx(letter_blocks(rendered.subject, rendered.body_lf)),
                        directory,
                        document_filename("Anschreiben", **names),
                        dry_run_directory=dry_run_directory,
                    ),
                )
            )
    return attachments, tuple(warnings)
