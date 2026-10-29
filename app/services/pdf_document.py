"""Stage 20: PDF rendering of the confirmed-fact document blocks.

Input is the same `Block` sequence the DOCX export writes
(`app.agents.cv_document.build_cv_document` -- trusted draft facts and the
confirmed identity only -- or `document_export.letter_blocks` of a reviewed
cover letter). This module adds no text: it lays the blocks out on A4 pages.

Safety properties:

- **No silent change of content.** A character the standard PDF fonts
  cannot show (outside cp1252 / WinAnsiEncoding, or a control character) is
  refused with `UnrenderableTextError` -- never replaced, dropped or
  transliterated. Only a no-break space becomes a space and a soft hyphen is
  removed (neither is visible text).
- **Round-trip check.** `render_pdf` extracts the text of the finished PDF
  with pypdf (the same extraction `scripts/presubmit_check.py` uses) and
  refuses the result (`PdfVerificationError`) unless it equals the blocks'
  text, ignoring only whitespace.
- **Deterministic.** Standard fonts (not embedded), no timestamps, no
  document ID, no compression: the same blocks always give the same bytes,
  so `write_manifest` hashes stay meaningful.
- Stdlib only for writing; pypdf (already a dependency) only for the check.
"""

import hashlib
import io
import unicodedata
from dataclasses import dataclass

from app.agents.cv_document import Block, BlockKind
from app.services import pdf_font_metrics as metrics

PDF_MEDIA_TYPE = "application/pdf"
PAGE_WIDTH = 595.28  # A4 in points
PAGE_HEIGHT = 841.89
MARGIN = 62.0
BULLET = "•"


@dataclass(frozen=True)
class _Style:
    bold: bool
    size: float
    before: float
    after: float
    indent: float = 0.0


_STYLES: dict[BlockKind, _Style] = {
    "TITLE": _Style(bold=True, size=18, before=0, after=8),
    "HEADING": _Style(bold=True, size=12.5, before=12, after=4),
    "SUBHEADING": _Style(bold=True, size=10.5, before=8, after=2),
    "PARAGRAPH": _Style(bold=False, size=10.5, before=0, after=4),
    "BULLET": _Style(bold=False, size=10.5, before=0, after=2, indent=14),
}
_KEEP_WITH_NEXT = frozenset({"TITLE", "HEADING", "SUBHEADING"})
_FONT_RESOURCE = {False: "F1", True: "F2"}


class UnrenderableTextError(ValueError):
    """The text contains characters the PDF fonts cannot show."""

    def __init__(self, characters: list[str]) -> None:
        self.characters = characters
        shown = ", ".join(f"U+{ord(c):04X}" for c in characters)
        super().__init__(f"characters not renderable in the PDF: {shown}")


class PdfVerificationError(RuntimeError):
    """The rendered PDF's extracted text differs from the source blocks."""


@dataclass(frozen=True)
class RenderedPdf:
    content: bytes
    pages: int


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    # no-break space -> space, soft hyphen -> nothing (neither is visible text)
    return text.replace(chr(0xA0), " ").replace(chr(0xAD), "").replace("\t", " ")


def _encode(text: str) -> bytes:
    """cp1252 bytes, or UnrenderableTextError listing every bad character.
    A newline is kept: it is a line break inside the block."""
    bad: list[str] = []
    out = bytearray()
    for char in text:
        if char == "\n":
            out += b"\n"
            continue
        try:
            byte = char.encode("cp1252")
        except UnicodeEncodeError:
            bad.append(char)
            continue
        code = byte[0]
        if code < metrics.FIRST_BYTE or metrics.HELVETICA[code - metrics.FIRST_BYTE] == 0:
            bad.append(char)
            continue
        out += byte
    if bad:
        raise UnrenderableTextError(list(dict.fromkeys(bad)))
    return bytes(out)


def _width(data: bytes, bold: bool, size: float) -> float:
    table = metrics.HELVETICA_BOLD if bold else metrics.HELVETICA
    return sum(table[b - metrics.FIRST_BYTE] for b in data) * size / 1000


def _wrap(data: bytes, bold: bool, size: float, width: float) -> list[bytes]:
    """Greedy word wrap; a word longer than the line is split by character."""
    lines: list[bytes] = []
    for raw_line in data.split(b"\n"):
        current = b""
        for word in raw_line.split(b" "):
            if not word:
                continue
            candidate = word if not current else current + b" " + word
            if _width(candidate, bold, size) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            chunks = _split_long_word(word, bold, size, width)
            lines.extend(chunks[:-1])
            current = chunks[-1]
        if current:
            lines.append(current)
    return lines


def _split_long_word(word: bytes, bold: bool, size: float, width: float) -> list[bytes]:
    """One pass (Codex review B, P2: the old prefix search was cubic); a
    chunk always holds at least one character."""
    table = metrics.HELVETICA_BOLD if bold else metrics.HELVETICA
    limit = width * 1000 / size
    chunks: list[bytes] = []
    start, used = 0, 0
    for index, byte in enumerate(word):
        advance = table[byte - metrics.FIRST_BYTE]
        if used + advance > limit and index > start:
            chunks.append(word[start:index])
            start, used = index, 0
        used += advance
    chunks.append(word[start:])
    return chunks


def _pdf_string(data: bytes) -> bytes:
    out = bytearray(b"(")
    for byte in data:
        if byte in b"()\\":
            out += b"\\" + bytes([byte])
        elif 32 <= byte < 127:
            out.append(byte)
        else:
            out += b"\\%03o" % byte
    return bytes(out + b")")


def _layout(blocks: list[Block]) -> list[list[bytes]]:
    """Content-stream operators per page."""
    text_width = PAGE_WIDTH - 2 * MARGIN
    top, bottom = PAGE_HEIGHT - MARGIN, MARGIN
    pages: list[list[bytes]] = [[]]
    y = top

    def leading(style: _Style) -> float:
        return style.size * 1.3

    # Encode and wrap everything first: an unrenderable character anywhere
    # fails before any layout work.
    wrapped = [
        (
            block,
            _wrap(
                _encode(_normalize(block.text)),
                _STYLES[block.kind].bold,
                _STYLES[block.kind].size,
                text_width - _STYLES[block.kind].indent,
            ),
        )
        for block in blocks
    ]
    wrapped = [(block, lines) for block, lines in wrapped if lines]

    def height_needed(index: int) -> float:
        """Space this block needs on the current page. A heading chain
        (TITLE/HEADING/SUBHEADING ...) is kept together with the first line
        of the block that follows it (Codex review B, P3); any other block
        with at least two lines (or all of a one-line block)."""
        block, lines = wrapped[index]
        style = _STYLES[block.kind]
        if block.kind not in _KEEP_WITH_NEXT:
            return style.before + leading(style) * min(len(lines), 2)
        needed = 0.0
        while index < len(wrapped) and wrapped[index][0].kind in _KEEP_WITH_NEXT:
            chain_style = _STYLES[wrapped[index][0].kind]
            needed += chain_style.before + leading(chain_style) * len(wrapped[index][1])
            needed += chain_style.after
            index += 1
        if index < len(wrapped):
            next_style = _STYLES[wrapped[index][0].kind]
            needed += next_style.before + leading(next_style)
        return needed

    for index, (block, lines) in enumerate(wrapped):
        style = _STYLES[block.kind]
        indent = style.indent
        needed = height_needed(index)
        if y != top and y - needed < bottom:
            pages.append([])
            y = top
        if y != top:
            y -= style.before
        for number, line in enumerate(lines):
            if y - leading(style) < bottom:
                pages.append([])
                y = top
            y -= leading(style)
            font = _FONT_RESOURCE[style.bold]
            ops = pages[-1]
            if block.kind == "BULLET" and number == 0:
                bullet = _encode(BULLET)
                ops.append(
                    b"BT /%s %.2f Tf %.2f %.2f Td %s Tj ET"
                    % (font.encode(), style.size, MARGIN + 3, y, _pdf_string(bullet))
                )
            ops.append(
                b"BT /%s %.2f Tf %.2f %.2f Td %s Tj ET"
                % (font.encode(), style.size, MARGIN + indent, y, _pdf_string(line))
            )
        y -= style.after
    return [page for page in pages if page] or [[]]


def _assemble(pages: list[list[bytes]], title: str) -> bytes:
    objects: list[bytes] = []
    page_ids = [5 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects.append(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(pages)))
    for name in (b"Helvetica", b"Helvetica-Bold"):
        objects.append(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % name
        )
    for ops in pages:
        page_id = 5 + len(objects) - 4
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_WIDTH, PAGE_HEIGHT, page_id + 1)
        )
        stream = b"\n".join(ops)
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    info_id = len(objects) + 1
    objects.append(
        b"<< /Title %s /Producer (JobTriage) >>" % _pdf_string(_encode(_normalize(title)))
    )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, info_id, xref)
    )
    return out.getvalue()


def _compact(text: str) -> str:
    return "".join(text.split())


def text_sha256(text: str) -> str:
    """SHA-256 of `text` without whitespace -- the comparison render_pdf
    verifies between a PDF's extracted text and its blocks. The exported
    source file names it, and `presubmit_check.py --bind` refuses to bind a
    PDF whose text does not have it."""
    return hashlib.sha256(_compact(text).encode("utf-8")).hexdigest()


def expected_text(blocks: list[Block] | tuple[Block, ...]) -> str:
    parts = []
    for block in blocks:
        text = _normalize(block.text)
        parts.append(f"{BULLET}{text}" if block.kind == "BULLET" and text.strip() else text)
    return "\n".join(parts)


def extracted_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def render_pdf(blocks: list[Block] | tuple[Block, ...], *, title: str) -> RenderedPdf:
    """A4 PDF of `blocks`; verified by extracting its text again."""
    blocks = list(blocks)
    pages = _layout(blocks)
    content = _assemble(pages, title)
    if _compact(extracted_text(content)) != _compact(expected_text(blocks)):
        raise PdfVerificationError("the rendered PDF text differs from the source blocks")
    return RenderedPdf(content=content, pages=len(pages))
