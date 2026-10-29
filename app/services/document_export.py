"""Stage 14D: local application-document files and the attachment plan.

LOCAL FILESYSTEM ONLY. Nothing here sends, uploads, attaches to an e-mail
or contacts anyone; the attachment plan is a description for the human who
applies (portal upload or, later, a reviewed draft), with delivery always
`NOT_SENT`.

- **DOCX without new dependencies.** A minimal WordprocessingML package is
  written with the standard library (`zipfile`), byte-deterministic: fixed
  member order and fixed 1980-01-01 timestamps, so the same document always
  has the same SHA-256.
- **PDF (Stage 20)**: the same blocks rendered by `app.services.pdf_document`
  (standard fonts, deterministic, round-trip text check, unrenderable
  characters refused); `document_format="pdf"`. DOCX stays the default.
- **Deterministic, sanitized filenames**: `Lebenslauf_<Vorname>_<Nachname>_
  <Firma>_<Referenz>.docx|.pdf`; umlauts transliterated, every other character
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
from app.services.pdf_document import PDF_MEDIA_TYPE, expected_text, render_pdf, text_sha256
from app.services.pre_submit_validation import SOURCE_DIRECTORY, SOURCE_FILES, SOURCE_TEXT_META

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DELIVERY_NOT_SENT = "NOT_SENT"
APPROVAL_PENDING = "APPROVAL_PENDING"

_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_TRANSLITERATION = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}
)
_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9]+")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,180}\.(docx|pdf)$")
DocumentFormat = Literal["docx", "pdf"]
_MEDIA_TYPES: dict[str, str] = {"docx": DOCX_MEDIA_TYPE, "pdf": PDF_MEDIA_TYPE}
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
    extension: DocumentFormat = "docx",
) -> str:
    parts = [safe_filename_component(prefix)] + [
        safe_filename_component(value) for value in (first_name, last_name, company, reference)
    ]
    name = "_".join(part for part in parts if part) + f".{extension}"
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


def letter_blocks(subject: str, body: str, sender: tuple[str, ...] = ()) -> tuple[Block, ...]:
    """The cover letter as document blocks: the sender lines (if any),
    subject heading, then one paragraph per blank-line-separated block of
    the rendered letter."""
    paragraphs = [part.strip() for part in body.split("\n\n") if part.strip()]
    head = (Block("PARAGRAPH", "\n".join(sender)),) if sender else ()
    return (
        *head,
        Block("SUBHEADING", subject),
        *(Block("PARAGRAPH", part) for part in paragraphs),
    )


def sender_lines(identity: ApplicationIdentity) -> tuple[str, ...]:
    """Stage 20: the letter's sender block, from the operator-confirmed
    identity only (same fields as the CV's contact lines)."""
    address = ", ".join(
        part for part in (identity.street, identity.postal_line, identity.country) if part
    )
    lines = [identity.full_name]
    if address:
        lines.append(address)
    lines += [f"Telefon: {identity.phone}", f"E-Mail: {identity.email}"]
    return tuple(lines)


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
        raise UnsafeExportTargetError("filename is not a safe .docx/.pdf name")
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
        media_type=_MEDIA_TYPES[filename.rsplit(".", 1)[1]],
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

_SOURCE_TAGS = {"TITLE": "h1", "HEADING": "h2", "SUBHEADING": "h3", "BULLET": "li"}


def source_html(blocks: tuple[Block, ...], title: str) -> bytes:
    """The exact text of an exported PDF as the package's source file
    (`_quelle_html/_cv.html` / `_letter.html`), so `presubmit_check.py
    --bind` can bind the PDF to it. The file names the hash of that text;
    --bind refuses a PDF whose text differs (`write_manifest`)."""
    lines = [
        "<!doctype html>",
        '<html lang="de"><head><meta charset="utf-8">',
        f'<meta name="{SOURCE_TEXT_META}" content="{text_sha256(expected_text(blocks))}">',
        f"<title>{escape(title)}</title></head><body>",
    ]
    for block in blocks:
        tag = _SOURCE_TAGS.get(block.kind, "p")
        lines.append(f"<{tag}>{escape(block.text)}</{tag}>")
    lines.append("</body></html>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_sources(directory: Path, sources: dict[str, bytes], *, dry_run_directory: bool) -> None:
    """Write the PDF sources; like write_document, never over an existing
    file outside a dry-run directory."""
    folder = directory / SOURCE_DIRECTORY
    folder.mkdir(exist_ok=True)
    for kind, content in sources.items():
        target = folder / SOURCE_FILES[kind]
        if dry_run_directory:
            target.write_bytes(content)
            continue
        with target.open("xb") as handle:
            handle.write(content)


def export_package_documents(
    *,
    cv: TailoredCVDraft,
    reviewed_bewerbung: ReviewedBewerbungContent | None,
    identity: ApplicationIdentity | None,
    company: str | None,
    reference: str | None,
    directory: Path,
    dry_run_directory: bool = False,
    document_format: DocumentFormat = "docx",
) -> tuple[list[PlannedAttachment], tuple[str, ...]]:
    """Write the CV (and, when given, the reviewed cover letter) as local
    DOCX or PDF files. Returns the planned attachments and the document
    warnings (contact-data problems, an unrenderable letter) for
    `build_attachment_plan`. Writes files only; sends nothing. Both
    documents are rendered (and a PDF verified) before any file is written,
    so a refused PDF never leaves half a package behind."""
    document = build_cv_document(cv, identity)
    names = dict(
        first_name=document.first_name,
        last_name=document.last_name,
        company=company,
        reference=reference,
        extension=document_format,
    )

    def render(blocks: tuple[Block, ...], title: str) -> bytes:
        if document_format == "pdf":
            return render_pdf(blocks, title=title).content
        return render_docx(blocks)

    cv_content = render(document.blocks, "Lebenslauf")
    rendered = render_draft_content(reviewed_bewerbung) if reviewed_bewerbung else None
    # Contact lines only when the CV's own contact checks passed (no
    # CONTACT_* warning): the same confirmed identity, never a partial block.
    sender = sender_lines(identity) if identity is not None and not document.warnings else ()
    letter = (
        letter_blocks(rendered.subject, rendered.body_lf, sender)
        if rendered is not None and not isinstance(rendered, str)
        else None
    )
    letter_content = render(letter, "Anschreiben") if letter is not None else None
    # PDF only: the source files `presubmit_check.py --bind` binds the PDFs to.
    sources: dict[str, bytes] = {}
    if document_format == "pdf":
        sources["cv"] = source_html(document.blocks, "Lebenslauf")
        if letter is not None:
            sources["letter"] = source_html(letter, "Anschreiben")
        existing = [
            name
            for kind, name in SOURCE_FILES.items()
            if kind in sources and (directory / SOURCE_DIRECTORY / name).exists()
        ]
        if existing and not dry_run_directory:
            raise DocumentExistsError(f"refusing to overwrite existing source {existing[0]}")
    attachments = [
        PlannedAttachment(
            "CV",
            write_document(
                cv_content,
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
        if isinstance(rendered, str):
            warnings.append(f"COVER_LETTER_{rendered}")
        elif letter_content is not None:
            attachments.append(
                PlannedAttachment(
                    "COVER_LETTER",
                    write_document(
                        letter_content,
                        directory,
                        document_filename("Anschreiben", **names),
                        dry_run_directory=dry_run_directory,
                    ),
                )
            )
    if sources:
        _write_sources(directory, sources, dry_run_directory=dry_run_directory)
    return attachments, tuple(warnings)
