"""Night audit 2026-10-27: pre-submit validation of one application package.

Live incidents this guards against: a PDF from another vacancy's package
was pre-filled into a job-board form (every package uses the same file
names), documents had been built from an older profile, the upload copy in
`.playwright-mcp/upload` had to be compared with the original by hand, and
a portal form demanded documents the candidate does not have (grade +
transcript) although the posting never mentioned them.

`validate_package` is read-only and deterministic: no network, no
database, no browser. It checks, per document kind (cv / letter):

- the original exists, is non-empty, below the portal size limit, is a
  structurally complete PDF (header + %%EOF) with a page count within limit;
- the PDF's own text (extracted with pypdf) names the candidate's contact
  e-mail and phone, the letter names the company and the role, and no
  unconfirmed claim appears (a PDF whose text cannot be read needs a human);
- the file actually uploaded is compared (no `attached` -> not READY), and a
  package PDF identical to another package's file is flagged;
- every file about to be attached is byte-identical (SHA-256) to this
  package's original -- identical to ANOTHER package's file is
  WRONG_PACKAGE;
- every portal field the operator marked as required-but-unavailable
  blocks (the candidate must supply it; nothing is invented);
- the profile version the package was built from, when recorded in
  `_manifest.json`, is the current one;
- when `_manifest.json` binds a PDF to its source (`write_manifest`, run
  right after rendering), a PDF or source changed afterwards is BLOCKED.

Result: BLOCKED (do not submit), NEEDS_OPERATOR (submit only after the
listed points were checked by a human) or READY. Even READY is not an
instruction to submit: the final click stays with the candidate. Findings
carry codes and file names only -- never the contact values themselves.
"""

import hashlib
import html
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.services.application_correlation import role_phrase
from app.services.application_duplicates import company_identity

Severity = Literal["BLOCK", "WARN", "OK"]
Outcome = Literal["BLOCKED", "NEEDS_OPERATOR", "READY"]
DocumentKind = Literal["cv", "letter"]

DOCUMENT_FILES: dict[DocumentKind, str] = {
    "cv": "Lebenslauf",
    "letter": "Anschreiben",
}
SOURCE_FILES: dict[DocumentKind, str] = {"cv": "_cv.html", "letter": "_letter.html"}
DEFAULT_MAX_PAGES: dict[DocumentKind, int] = {"cv": 3, "letter": 1}
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_LEVELS = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6, "NATIVE": 7}


@dataclass(frozen=True)
class Expectation:
    company: str
    role: str
    contact_email: str | None = None
    contact_phone: str | None = None
    profile_version: int | None = None
    # Confirmed German level from the profile; higher claims in a document
    # are flagged.
    german_level: str | None = None
    unconfirmed_claims: tuple[str, ...] = ()
    letter_required: bool = True
    max_pages: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_MAX_PAGES))
    max_bytes: int = DEFAULT_MAX_BYTES


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    detail: str


@dataclass(frozen=True)
class PreSubmitReport:
    package: str
    findings: tuple[Finding, ...]

    @property
    def outcome(self) -> Outcome:
        severities = {f.severity for f in self.findings}
        if "BLOCK" in severities:
            return "BLOCKED"
        if "WARN" in severities:
            return "NEEDS_OPERATOR"
        return "READY"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_page_count(data: bytes) -> int | None:
    """Pages of a structurally complete PDF, or None if it is not one;
    0 means the page tree cannot be read (treated as unreadable).

    The parser's page tree is authoritative (PDF 1.5+ object streams, escaped
    names such as `/P#61ge`); the raw `/Type /Page` count is only a floor, so
    a PDF is never counted shorter than the page objects it visibly holds
    (Codex rounds 4-5)."""
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
        return None
    if b"startxref" not in data[-4096:] or b"/Root" not in data:
        return None
    raw = len(re.findall(rb"/Type\s*/Page(?![s\w])", data))
    try:
        from io import BytesIO

        from pypdf import PdfReader

        parsed = len(PdfReader(BytesIO(data)).pages)
    except Exception:  # noqa: BLE001 - any parser failure means "unreadable"
        return 0
    return max(raw, parsed) if parsed else 0


MIN_PDF_TEXT_CHARS = 200
# A CV/letter never has more pages; a longer PDF is the wrong file and is
# not parsed at all.
MAX_PAGES_TO_READ = 10


def pdf_text(path: Path) -> str | None:
    """The PDF's own text (pypdf), whitespace-collapsed; None if it cannot
    be read. Untrusted data: only searched, never interpreted."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is declared
        return None
    try:
        reader = PdfReader(str(path))
        text = " ".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # noqa: BLE001 - any parser failure means "unreadable"
        return None
    return " ".join(text.split())


def source_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", raw)).split())


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[2:] if digits.startswith("49") else digits.lstrip("0")


def _contains_phone(text: str, phone: str) -> bool:
    wanted = _digits(phone)
    return bool(wanted) and any(
        _digits(candidate).endswith(wanted) for candidate in re.findall(r"\+?[\d ()/\-]{8,}", text)
    )


_ROLE_STOPWORDS = frozenset({"m", "w", "d", "x", "div", "und", "and", "fuer", "für", "im", "in"})


def _role_covered(text: str, role: str) -> bool:
    """Most of the role's own words appear in the letter (gender suffix and
    filler ignored): the letter is written for THIS vacancy."""
    words = [w for w in re.findall(r"[\wäöüß]{3,}", role_phrase(role)) if w not in _ROLE_STOPWORDS]
    if not words:
        return False
    lowered = text.casefold()
    hits = sum(1 for w in words if w in lowered)
    return hits / len(words) >= 0.6


# Codex rounds 13-15: German levels in a document are read per mention, not
# with one regex per phrasing. After "Deutsch..." every CEFR level up to the
# next language name or the end of the statement is claimed for German
# ("Deutschkenntnisse: Niveau B2", "auf B2-Niveau"); before it, only a level
# directly in front ("B2-Niveau in Deutsch", "C1 Deutsch"). A language name is
# any "-isch" word or English one, so "Arabisch B2" on the next CV row (pdf_text
# joins rows) is never German.
_GERMAN_WORD = re.compile(r"deutsch(?!land)\w*", re.I)
_LANGUAGE_WORD = re.compile(
    r"\b(?:\w+isch\w*|english|french|spanish|italian|arabic|russian|polish|turkish)\b", re.I
)
_CEFR_LEVEL = re.compile(r"(?<![A-Za-z0-9])([ABC][12])(?![0-9])")
# A level being worked towards is not a claim: "Deutsch B1 (B2-Kurs
# laufend)", "ein B2-Kurs läuft", "Ziel: B2", "B2 angestrebt" (the real
# packages say exactly this).
_GOAL_AFTER = re.compile(
    r"[\s-]*(?:kurs|pr(?:ü|ue)fung|in\s+vorbereitung|angestrebt|geplant|laufend)", re.I
)
_GOAL_BEFORE = re.compile(r"(?:ziel\w*|angestrebt\w*|auf\s+dem\s+weg\s+zu)\W*$", re.I)
# The next "Label:" starts a new CV row; pdf_text joins rows, so "Deutsch:
# B1" + "Muttersprache: Arabisch" arrive as one line (Codex round 19).
_NEXT_LABEL = re.compile(r"\b\w+\s*:")
_STATEMENT_END = re.compile(r"[.;•|]\s|\n")
_LEVEL_BEFORE = re.compile(r"([ABC][12])[\s(\-–:]*(?:niveau\s*)?(?:in\s+|im\s+)?$", re.I)
# A descriptor directly before the mention ("verhandlungssicheres Deutsch");
# like a level, it is German only when no language name precedes it in the
# same row ("Arabisch: Muttersprache Deutsch: B1", Codex round 17).
_DESCRIPTOR_BEFORE = re.compile(
    r"(muttersprach\w*|verhandlungssicher\w*|flie(?:ß|ss)end\w*)[\s:–-]*$", re.I
)
# The same words after the mention, inside its window ("Deutsch:
# verhandlungssicher", "Deutsch – Muttersprache"; Codex round 16).
_DESCRIPTOR_AFTER = (
    (re.compile(r"muttersprach|native", re.I), "NATIVE"),
    (re.compile(r"verhandlungssicher|flie(?:ß|ss)end|fluent", re.I), "C1"),
)


def _owned_by_german(row_before: str) -> bool:
    """Whether a level/descriptor whose preceding text is `row_before`
    belongs to the "Deutsch" after it: not when a language name stands
    before it in the same comma-free part of the row ("Arabisch: in der
    Familie meine Muttersprache Deutsch: B1"; Codex rounds 15-18)."""
    part = re.split(r"[.;•|\n,]", row_before)[-1]
    return not _LANGUAGE_WORD.search(part)


def _german_claims(text: str) -> set[str]:
    claims: set[str] = set()
    for mention in _GERMAN_WORD.finditer(text):
        stop = min(len(text), mention.end() + 40)
        for end in (
            _LANGUAGE_WORD.search(text, mention.end(), stop),
            _STATEMENT_END.search(text, mention.end(), stop),
            _NEXT_LABEL.search(text, mention.end(), stop),
        ):
            if end is not None:
                stop = min(stop, end.start())
        for level in _CEFR_LEVEL.finditer(text, mention.end(), stop):
            # Goal words are read from the full text (the window may cut
            # "B2-Kurs" after "B2-").
            if _GOAL_AFTER.match(text, level.end()) or _GOAL_BEFORE.search(
                text, mention.end(), level.start()
            ):
                continue
            claims.add(level.group(1).upper())
        # A descriptor after the mention counts up to the first comma only
        # ("Deutsch B1, Muttersprache: Arabisch" is Arabic's; round 18).
        comma = text.find(",", mention.end(), stop)
        descriptor_stop = stop if comma == -1 else comma
        for pattern, level_name in _DESCRIPTOR_AFTER:
            if pattern.search(text, mention.end(), descriptor_stop):
                claims.add(level_name)
        lead = text[max(0, mention.start() - 60) : mention.start()]
        before = _LEVEL_BEFORE.search(lead)
        if before is not None and _owned_by_german(lead[: before.start()]):
            # "Japanisch B2 Deutsch B1", "Arabisch: Niveau B2 Deutsch B1": a
            # level with a language name before it in the same row belongs to
            # that language (Codex rounds 15-16).
            claims.add(before.group(1).upper())
        descriptor = _DESCRIPTOR_BEFORE.search(lead)
        if descriptor is not None and _owned_by_german(lead[: descriptor.start()]):
            word = descriptor.group(1).casefold()
            claims.add("NATIVE" if word.startswith("muttersprach") else "C1")
    return claims


def _overclaimed_german(text: str, confirmed: str | None) -> list[str]:
    # Codex round 12: without a confirmed level (None / UNKNOWN) every
    # stated claim is unconfirmed, never silently accepted.
    held = _LEVELS.get((confirmed or "").upper(), 0)
    return sorted(
        (level for level in _german_claims(text) if _LEVELS[level] > held),
        key=_LEVELS.__getitem__,
        reverse=True,
    )


MANIFEST = "_manifest.json"


def write_manifest(package: Path, profile_version: int) -> dict:
    """Bind each PDF of `package` to its source text (and record the profile
    version). Call it right after rendering, once the PDF was looked at;
    it never touches the documents themselves."""
    documents = {}
    for kind in ("cv", "letter"):
        original = _original(package, kind)  # type: ignore[arg-type]
        source = package / "_quelle_html" / SOURCE_FILES[kind]  # type: ignore[index]
        if original is not None and source.is_file():
            documents[kind] = {
                "pdf": original.name,
                "pdf_sha256": sha256_file(original),
                "source_sha256": sha256_file(source),
            }
    manifest = {"profile_version": profile_version, "documents": documents}
    (package / MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


def _load_manifest(package: Path) -> dict:
    path = package / MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _original(package: Path, kind: DocumentKind) -> Path | None:
    stem = DOCUMENT_FILES[kind].casefold()
    matches = [p for p in package.glob("*.pdf") if stem in p.name.casefold()]
    return matches[0] if len(matches) == 1 else None


def validate_package(
    package: Path,
    expected: Expectation,
    *,
    attached: Mapping[DocumentKind, Path] | None = None,
    sibling_packages: Iterable[Path] = (),
    missing_required_fields: Iterable[str] = (),
    current_profile_version: int | None = None,
) -> PreSubmitReport:
    findings: list[Finding] = []
    add = findings.append
    kinds: tuple[DocumentKind, ...] = ("cv", "letter") if expected.letter_required else ("cv",)
    originals: dict[DocumentKind, Path] = {}
    blocked_pdfs: set[str] = set()
    for kind in kinds:
        original = _original(package, kind)
        if original is None:
            add(
                Finding(f"{kind.upper()}_MISSING", "BLOCK", f"no single {DOCUMENT_FILES[kind]} PDF")
            )
            continue
        originals[kind] = original
        data = original.read_bytes()
        if not data:
            add(Finding(f"{kind.upper()}_EMPTY", "BLOCK", original.name))
            continue
        if len(data) > expected.max_bytes:
            add(Finding(f"{kind.upper()}_TOO_LARGE", "BLOCK", f"{len(data)} bytes"))
            # Codex round 3: an oversized PDF is not parsed at all.
            blocked_pdfs.add(kind)
            continue
        pages = pdf_page_count(data)
        if not pages or pages > MAX_PAGES_TO_READ:
            # Codex round 3: an oversized / already-failed PDF is not parsed.
            blocked_pdfs.add(kind)
        if not pages:
            add(
                Finding(
                    f"{kind.upper()}_UNREADABLE", "BLOCK", f"{original.name} is not a complete PDF"
                )
            )
        elif pages > expected.max_pages.get(kind, 99):
            add(
                Finding(
                    f"{kind.upper()}_TOO_MANY_PAGES",
                    "WARN",
                    f"{pages} pages (limit {expected.max_pages[kind]})",
                )
            )
        else:
            add(Finding(f"{kind.upper()}_PDF_OK", "OK", f"{pages} page(s), {len(data)} bytes"))

    manifest = _load_manifest(package)
    bindings = manifest.get("documents") if isinstance(manifest.get("documents"), dict) else {}
    texts: dict[DocumentKind, str] = {}
    for kind in kinds:
        original = originals.get(kind)
        if original is None or kind in blocked_pdfs:
            continue
        # Codex night-audit review P1 (round 2): the content checks read the
        # PDF that will be uploaded, never only the HTML it was made from.
        text = pdf_text(original)
        if text is None or len(text) < MIN_PDF_TEXT_CHARS:
            add(
                Finding(
                    f"{kind.upper()}_TEXT_UNVERIFIED",
                    "WARN",
                    "the PDF's text could not be read; open the PDF and check it by hand",
                )
            )
        else:
            texts[kind] = text
        binding = bindings.get(kind) if isinstance(bindings.get(kind), dict) else None
        source = package / "_quelle_html" / SOURCE_FILES[kind]
        if binding is None and manifest:
            # Codex round 5: a manifest that binds only some documents must
            # not lend its profile-version stamp to the unbound one.
            add(
                Finding(
                    f"{kind.upper()}_NOT_BOUND",
                    "WARN",
                    "the manifest does not bind this PDF to a source; check it by hand "
                    "or re-render and re-bind",
                )
            )
        if binding is not None:
            if binding.get("pdf_sha256") != sha256_file(original):
                add(Finding(f"{kind.upper()}_PDF_CHANGED", "BLOCK", "PDF changed after binding"))
            elif source.is_file() and binding.get("source_sha256") != sha256_file(source):
                add(
                    Finding(
                        f"{kind.upper()}_SOURCE_CHANGED",
                        "BLOCK",
                        "source text changed after binding; re-render and re-bind",
                    )
                )
    for kind, text in texts.items():
        before = len(findings)
        if expected.contact_email and expected.contact_email.casefold() not in text.casefold():
            add(Finding(f"{kind.upper()}_CONTACT_EMAIL", "BLOCK", "profile e-mail not in document"))
        if expected.contact_phone and not _contains_phone(text, expected.contact_phone):
            add(Finding(f"{kind.upper()}_CONTACT_PHONE", "BLOCK", "profile phone not in document"))
        for level in _overclaimed_german(text, expected.german_level):
            add(
                Finding(
                    f"{kind.upper()}_GERMAN_OVERCLAIM",
                    "BLOCK",
                    f"claims German {level}, confirmed {expected.german_level or 'none'}",
                )
            )
        for claim in expected.unconfirmed_claims:
            if claim.casefold() in text.casefold():
                add(Finding(f"{kind.upper()}_UNCONFIRMED_CLAIM", "BLOCK", f"contains '{claim}'"))
        if len(findings) == before:
            add(Finding(f"{kind.upper()}_TEXT_OK", "OK", "contacts present, no over-claim"))
    letter = texts.get("letter")
    if letter is not None:
        company = company_identity(expected.company)
        # Addresses and links are not the letter naming the company
        # ("max@example.de" does not name "Example GmbH").
        prose = re.sub(r"\S+@\S+|https?://\S+|www\.\S+", " ", letter)
        if not company or not re.search(
            rf"(?<!\w){re.escape(company)}(?!\w)", company_identity(prose)
        ):
            add(Finding("LETTER_COMPANY", "BLOCK", "letter does not name this company"))
        if not _role_covered(letter, expected.role):
            add(Finding("LETTER_ROLE", "WARN", "letter does not clearly name this role"))

    sibling_hashes: dict[str, str] = {}
    for sibling in sibling_packages:
        if sibling.resolve() == package.resolve() or not sibling.is_dir():
            continue
        for pdf in sibling.glob("*.pdf"):
            sibling_hashes.setdefault(sha256_file(pdf), sibling.name)
    for kind, original in originals.items():
        twin = sibling_hashes.get(sha256_file(original))
        if twin is not None:
            add(
                Finding(
                    f"{kind.upper()}_SAME_FILE_AS_OTHER_PACKAGE",
                    "WARN",
                    f"this package's PDF is byte-identical to package '{twin}'; "
                    "make sure it was meant for this vacancy",
                )
            )
    if not attached or "cv" not in attached:
        # Codex round 3: comparing only the letter leaves a CV the portal
        # pre-filled from another package unchecked.
        add(
            Finding(
                "UPLOAD_NOT_CHECKED",
                "WARN",
                "the CV actually uploaded to the portal was not compared (--attach cv=...)",
            )
        )
    for kind, path in (attached or {}).items():
        if not path.is_file():
            add(Finding(f"{kind.upper()}_ATTACHMENT_MISSING", "BLOCK", path.name))
            continue
        digest = sha256_file(path)
        original = originals.get(kind)
        if original is not None and digest == sha256_file(original):
            add(Finding(f"{kind.upper()}_ATTACHMENT_MATCHES", "OK", f"sha256 {digest[:12]}"))
        elif digest in sibling_hashes:
            add(
                Finding(
                    f"{kind.upper()}_WRONG_PACKAGE",
                    "BLOCK",
                    f"attachment is the file of package '{sibling_hashes[digest]}'",
                )
            )
        else:
            add(
                Finding(
                    f"{kind.upper()}_ATTACHMENT_MISMATCH",
                    "BLOCK",
                    "attachment differs from this package's original",
                )
            )
    if attached and "letter" not in attached and expected.letter_required:
        add(Finding("LETTER_NOT_ATTACHED", "WARN", "no Anschreiben attached (no form field?)"))

    for name in missing_required_fields:
        add(
            Finding(
                "FORM_FIELD_NEEDS_CANDIDATE_INPUT",
                "BLOCK",
                f"required portal field '{name}' has no confirmed value; ask the candidate",
            )
        )

    built_from = manifest.get("profile_version")
    if current_profile_version is not None:
        if built_from is None:
            add(
                Finding(
                    "PROFILE_VERSION_UNRECORDED",
                    "WARN",
                    "package does not record its profile version",
                )
            )
        elif built_from != current_profile_version:
            add(
                Finding(
                    "PROFILE_VERSION_STALE",
                    "BLOCK",
                    f"built from profile v{built_from}, current v{current_profile_version}",
                )
            )
    return PreSubmitReport(package.name, tuple(findings))
