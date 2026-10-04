"""Stage 9B: pure, bounded Telegram rendering of a persisted Bewerbung
package -- the frozen summary preview and the paginated full-letter view.

**Rendered only from the exact persisted artifacts** (the pinned 6B match,
6C CV draft and 6D letter, including the letter's own persisted job display
context). Nothing here reads the live candidate profile or job, so an old
preview stays reproducible and can never show facts the package does not
contain. No candidate fact is added: every line is a projection of a
persisted field, and gaps are labeled as gaps ("Nicht belegt"), never as
candidate skills.

**Telegram limits.** Telegram counts message length in UTF-16 code units, so
every bound here uses `utf16_units`, not `len()`. Every message is at most
`TELEGRAM_UNIT_LIMIT` (3500, well under Telegram's 4096). Messages are plain
text -- the sender never sets `parse_mode`.

**Display normalization** (`normalize_scalar` / `normalize_multiline`)
strips control, format and bidi-override characters (keeping the zero-width
joiner that emoji sequences need) and collapses line breaks in scalar
fields, so untrusted job/candidate strings cannot fake extra preview lines
or reorder the displayed text. It never modifies the persisted artifacts.
"""

import unicodedata

from app.models.bewerbung import BewerbungDraft
from app.models.candidate_job_match import CandidateJobMatch, RequirementMatch
from app.models.cv_draft import TailoredCVDraft

PREVIEW_RENDERER_VERSION = "tg-bw-preview-v1"
TELEGRAM_UNIT_LIMIT = 3500
MAX_LETTER_PAGES = 20

DRAFT_STATUS_LINE = "ENTWURF — NICHT GESENDET"
_ELLIPSIS = "…"

_TITLE_LIMIT = 200
_COMPANY_LIMIT = 150
_ITEM_LIMIT = 80
_MAX_LIST_ITEMS = 8
_SUBJECT_LIMIT = 200
_EXCERPT_LIMIT = 600

# Format characters that must survive normalization: the zero-width joiner
# (U+200D) holds multi-codepoint emoji together.
_KEEP_FORMAT_CHARS = frozenset({"‍"})
_STRIPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})

# Persisted warning codes already stated by a fixed preview line.
_COVERED_WARNINGS = frozenset({"CONTACT_DATA_NOT_MODELED"})

_WARNING_TEXT = {
    "NO_TRUSTED_NAME": "Kein bestätigter Name - die Unterschrift fehlt im Anschreiben.",
}


def utf16_units(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def _strip_unsafe(text: str, *, keep_newlines: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for char in text:
        if char == "\n" or char in (" ", " "):
            out.append("\n" if keep_newlines else " ")
        elif char == "\t":
            out.append(" ")
        elif unicodedata.category(char) in _STRIPPED_CATEGORIES and char not in _KEEP_FORMAT_CHARS:
            continue
        else:
            out.append(char)
    return "".join(out)


def normalize_scalar(value: str | None) -> str:
    """One display line: unsafe characters removed, every line break and
    whitespace run collapsed to a single space."""
    if not value:
        return ""
    return " ".join(_strip_unsafe(value, keep_newlines=False).split())


def normalize_multiline(value: str | None) -> str:
    """Multi-line display text: unsafe characters removed, each line's
    whitespace collapsed, at most one blank line between paragraphs."""
    if not value:
        return ""
    lines = [
        " ".join(line.split()) for line in _strip_unsafe(value, keep_newlines=True).split("\n")
    ]
    result: list[str] = []
    for line in lines:
        if not line and (not result or not result[-1]):
            continue
        result.append(line)
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


def cap_units(text: str, limit: int) -> str:
    """Truncate to at most `limit` UTF-16 units, marking truncation with an
    ellipsis. Never splits a code point (and so never a surrogate pair)."""
    if utf16_units(text) <= limit:
        return text
    budget = max(limit - utf16_units(_ELLIPSIS), 0)
    out: list[str] = []
    used = 0
    for char in text:
        width = utf16_units(char)
        if used + width > budget:
            break
        out.append(char)
        used += width
    return "".join(out).rstrip() + _ELLIPSIS


def _field(value: str | None, limit: int, missing: str = "nicht angegeben") -> str:
    normalized = normalize_scalar(value)
    return cap_units(normalized, limit) if normalized else missing


def _requirement_lines(requirements: list[RequirementMatch]) -> list[str]:
    names: list[str] = []
    for requirement in requirements:
        name = normalize_scalar(requirement.requirement)
        if name and name not in names:
            names.append(name)
    lines = [f"• {cap_units(name, _ITEM_LIMIT)}" for name in names[:_MAX_LIST_ITEMS]]
    if len(names) > _MAX_LIST_ITEMS:
        lines.append(f"• … und {len(names) - _MAX_LIST_ITEMS} weitere")
    return lines


def _section(title: str, lines: list[str], empty: str | None = None) -> list[str]:
    if not lines:
        return [title, f"• {empty}"] if empty else []
    return [title, *lines]


def _letter_excerpt(letter: BewerbungDraft) -> str:
    parts = [letter.opening]
    if letter.body_paragraphs:
        parts.append(letter.body_paragraphs[0].text)
    return cap_units(normalize_scalar(" ".join(parts)), _EXCERPT_LIMIT)


def _cv_change_lines(cv: TailoredCVDraft) -> list[str]:
    """Only changes 6C actually performs: selection and ordering. The
    professional summary is copied verbatim, never adapted."""
    lines: list[str] = []
    if cv.skills:
        lines.append(f"• {len(cv.skills)} passende Skills nach den Anforderungen priorisiert")
    if cv.projects:
        lines.append(f"• {len(cv.projects)} relevante Projekte ausgewählt")
    highlighted = [exp for exp in cv.experience if exp.matched_skills]
    if highlighted:
        lines.append(f"• {len(highlighted)} Berufsstationen mit passenden Skills hervorgehoben")
    if cv.professional_summary is not None:
        lines.append("• Profilzusammenfassung unverändert übernommen")
    else:
        lines.append("• Keine bestätigte Profilzusammenfassung vorhanden")
    return lines


def _warning_lines(
    letter: BewerbungDraft, cv: TailoredCVDraft, match: CandidateJobMatch
) -> list[str]:
    lines: list[str] = []
    for code in letter.warnings:
        text = _WARNING_TEXT.get(code, f"Hinweis: {code}")
        lines.append(f"• {cap_units(normalize_scalar(text), _ITEM_LIMIT * 2)}")
    if match.missing_requirements or match.unknown_requirements:
        lines.append("• Entwurf unvollständig: nicht alle Anforderungen sind belegt.")
    lines.append("• Kontaktdaten sind nicht Teil dieses Entwurfs.")
    codes = [code for code in [*cv.warnings, *match.warnings] if code not in _COVERED_WARNINGS]
    for code in codes[:_MAX_LIST_ITEMS]:
        lines.append(f"• Hinweis: {cap_units(normalize_scalar(code), _ITEM_LIMIT)}")
    return lines


def render_summary(letter: BewerbungDraft, cv: TailoredCVDraft, match: CandidateJobMatch) -> str:
    """The frozen initial preview. Always starts and ends with the draft
    status, and is at most `TELEGRAM_UNIT_LIMIT` UTF-16 units."""
    context = letter.job_context
    title = context.title if context else None
    company = context.company if context else None

    header = "\n".join(["📄 Bewerbungsentwurf vorbereitet", DRAFT_STATUS_LINE])
    footer = "\n".join(
        [
            "Nichts wurde gesendet: keine Bewerbung, keine E-Mail, kein PDF.",
            f"Status: {DRAFT_STATUS_LINE}",
        ]
    )

    body: list[str] = [
        f"Position: {_field(title, _TITLE_LIMIT)}",
        f"Firma: {_field(company, _COMPANY_LIMIT)}",
        f"Kandidaten-Match (Profil vs. Stelle, 0-100): {match.overall_score}",
        "",
        *_section(
            "Belegte Anforderungen:",
            _requirement_lines(match.matched_requirements),
            "keine",
        ),
        *_section("Teilweise belegt:", _requirement_lines(match.partial_requirements)),
        *_section(
            "Nicht belegt (wird nicht behauptet):",
            _requirement_lines(match.missing_requirements),
        ),
        *_section("Unklar (nicht belegt):", _requirement_lines(match.unknown_requirements)),
        "",
        *_section(
            "Ausgewählte Projekte:",
            [
                f"• {cap_units(normalize_scalar(project.name), _ITEM_LIMIT)}"
                for project in cv.projects[:_MAX_LIST_ITEMS]
            ],
            "keine",
        ),
        "",
        "Anschreiben (Entwurf):",
        f"Betreff: {_field(letter.subject, _SUBJECT_LIMIT)}",
        f"Auszug: {_letter_excerpt(letter)}",
        "",
        "Lebenslauf (CV-Entwurf):",
        *_cv_change_lines(cv),
        "",
        "Hinweise:",
        *_warning_lines(letter, cv, match),
    ]

    body_text = "\n".join(body)
    fixed = utf16_units(header) + utf16_units(footer) + 2 * utf16_units("\n\n")
    body_text = cap_units(body_text, TELEGRAM_UNIT_LIMIT - fixed)
    return f"{header}\n\n{body_text}\n\n{footer}"


def letter_full_text(letter: BewerbungDraft) -> str:
    """The exact persisted letter as display text (normalized only)."""
    blocks = [f"Betreff: {normalize_scalar(letter.subject)}", letter.salutation, letter.opening]
    blocks.extend(paragraph.text for paragraph in letter.body_paragraphs)
    blocks.append(letter.closing)
    blocks.append(letter.signature_name or "[Name fehlt - kein bestätigter Name im Profil]")
    normalized = [normalize_multiline(block) for block in blocks]
    return "\n\n".join(block for block in normalized if block)


def _page_header(page: int, total: int) -> str:
    return f"👁 Anschreiben - Seite {page}/{total}\n{DRAFT_STATUS_LINE}\n\n"


def _split_long_block(block: str, budget: int) -> list[str]:
    """Split one paragraph by words, hard-splitting a word only if it alone
    exceeds the budget. Deterministic."""
    chunks: list[str] = []
    current = ""
    for word in block.split(" "):
        candidate = f"{current} {word}" if current else word
        if utf16_units(candidate) <= budget:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while utf16_units(word) > budget:
            head = cap_units(word, budget + 1)[:-1]  # exact prefix, no ellipsis
            if not head:
                head = word[0]
            chunks.append(head)
            word = word[len(head) :]
        current = word
    if current:
        chunks.append(current)
    return chunks


def paginate_letter(letter: BewerbungDraft) -> list[str]:
    """Deterministic pages of the full letter, each at most
    `TELEGRAM_UNIT_LIMIT` UTF-16 units including its page header. At most
    `MAX_LETTER_PAGES` pages; anything beyond is marked as truncated."""
    header_budget = utf16_units(_page_header(MAX_LETTER_PAGES, MAX_LETTER_PAGES))
    budget = TELEGRAM_UNIT_LIMIT - header_budget

    pieces: list[str] = []
    for block in letter_full_text(letter).split("\n\n"):
        if utf16_units(block) <= budget:
            pieces.append(block)
        else:
            pieces.extend(_split_long_block(block.replace("\n", " "), budget))

    bodies: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if utf16_units(candidate) <= budget:
            current = candidate
        else:
            bodies.append(current)
            current = piece
    if current:
        bodies.append(current)

    if len(bodies) > MAX_LETTER_PAGES:
        bodies = bodies[:MAX_LETTER_PAGES]
        bodies[-1] = cap_units(bodies[-1] + "\n\n(gekürzt)", budget)

    total = len(bodies)
    return [_page_header(index + 1, total) + body for index, body in enumerate(bodies)]
