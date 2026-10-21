"""V1 quality patch: the employer-facing text boundary for CV content.

The CV draft (app.agents.cv_adapter) copies profile free text VERBATIM for
traceability -- including the operator's internal bookkeeping that lives in
the same fields ("Period per Lebenslauf: 02/2023 – current (month/year
only).", "Confirmed by candidate 2026-10-20. Not software-related."), raw
enums (UNKNOWN, LANGUAGE) and English source phrases. None of that may reach
an employer. Every employer-facing CV renderer (the DOCX document and the
operator's review document of exactly that content) goes through this
module:

- internal/provenance sentences are dropped, never rewritten -- from every
  free-text field alike (descriptions, responsibilities, achievements,
  highlights, skills): `public_phrase` / `public_list` never return them;
- a period that the profile records only in such a note is parsed and shown
  in German ("02/2023 – heute"), structured dates always win;
- enum values are mapped to German or hidden, never printed raw;
- English source phrases are mapped to German ONLY through the closed,
  exact-phrase glossary below (a faithful translation of the candidate's
  own wording). Anything not in the glossary is left verbatim -- no free
  translation, no invented meaning.

Pure, deterministic, no I/O.
"""

import re
from collections.abc import Iterable
from datetime import date

# --- internal notes ---------------------------------------------------------

_PERIOD_NOTE = re.compile(
    r"period per lebenslauf:\s*(?P<period>[^()]+?)\s*(?:\([^)]*\))?\s*(?:\.|$)",
    re.IGNORECASE,
)
_INTERNAL_SENTENCE_MARKERS = re.compile(
    r"period per lebenslauf|confirmed by|corrected by|per candidate|candidate said|"
    r"candidate statement|not software-related|previously recorded|not checked|"
    r"\bsource:|runtime profile|provenance|verified_at|(?-i:\bUNKNOWN\b)",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_DASH = re.compile(r"\s*[–—-]\s*")
_CURRENT_WORDS = frozenset({"current", "present", "today", "heute", "aktuell", "now"})
_PERIOD_PART = re.compile(r"^(?:\d{2}/)?\d{4}$")

# --- closed glossary (exact phrases, casefolded, trailing period ignored) ---

_GLOSSARY: dict[str, str] = {
    # experience: IT support
    "technical support": "Technischer Support",
    "error analysis and troubleshooting in ongoing it operations": (
        "Fehleranalyse und Fehlerbehebung im laufenden IT-Betrieb"
    ),
    "setup, maintenance and documentation of workstations and internal systems": (
        "Einrichtung, Wartung und Dokumentation von Arbeitsplätzen und internen Systemen"
    ),
    "participation in internal it processes": "Mitwirkung an internen IT-Prozessen",
    "ongoing system support": "Laufende Systembetreuung",
    "structured work in a german technical-support environment": (
        "Strukturiertes Arbeiten in einem deutschsprachigen technischen Supportumfeld"
    ),
    # experience: leadership
    "leadership responsibility in operational and tactical work": (
        "Führungsverantwortung im operativen und taktischen Bereich"
    ),
    "team leadership and coordination": "Teamführung und Koordination",
    "decision-making under pressure": "Entscheidungsfindung unter Druck",
    "strategic planning": "Strategische Planung",
    "resource management": "Ressourcenmanagement",
    "structured reporting": "Strukturiertes Berichtswesen",
    # projects
    "own python backend project: web application for movie search": (
        "Eigenes Python-Backend-Projekt: Webanwendung zur Filmsuche"
    ),
    "own exercise projects in python": "Eigene Übungsprojekte in Python",
    "own python project: cli tool for analysis of stock momentum": (
        "Eigenes Python-Projekt: CLI-Tool zur Analyse des Aktien-Momentums"
    ),
    "web application": "Webanwendung",
    "movie search": "Filmsuche",
    "filtering": "Filterfunktionen",
    "search-query logging": "Protokollierung von Suchanfragen",
    "statistics page": "Statistikseite",
    "structured project architecture": "Strukturierte Projektarchitektur",
    "file processing": "Dateiverarbeitung",
    "workflow optimization": "Workflow-Optimierung",
    "processing structured data": "Verarbeitung strukturierter Daten",
    "analysis/evaluation of structured data": "Analyse und Auswertung strukturierter Daten",
    "cli tool for analysis of stock momentum": "CLI-Tool zur Analyse des Aktien-Momentums",
    "html reporting": "HTML-Reporting",
    "caching mechanism": "Caching-Mechanismus",
    "automated unit tests": "Automatisierte Unit-Tests",
    # skills
    "data processing": "Datenverarbeitung",
    "backend architecture": "Backend-Architektur",
    "unit testing": "Unit-Tests",
    # header
    "germany": "Deutschland",
}

_LANGUAGE_LEVELS = {"NATIVE": "Muttersprache"}
_CEFR = frozenset({"A1", "A2", "B1", "B2", "C1", "C2"})
_PROFICIENCY = {
    "BEGINNER": "Grundkenntnisse",
    "BASIC": "Grundkenntnisse",
    "INTERMEDIATE": "fortgeschritten",
    "ADVANCED": "sehr gut",
    "EXPERT": "Expertenwissen",
}
_CERTIFICATION_STATUS = {
    "COMPLETED": "abgeschlossen",
    "IN_PROGRESS": "in Bearbeitung",
    "PLANNED": "geplant",
}


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().rstrip(".").strip()).casefold()


def is_internal_text(text: str) -> bool:
    """True when `text` carries operator bookkeeping (provenance, candidate
    confirmations, raw enums) rather than CV prose. Matches the metadata
    patterns only -- ordinary vocabulary such as "internal systems" is not
    internal text."""
    return bool(_INTERNAL_SENTENCE_MARKERS.search(text))


def public_phrase(text: str) -> str:
    """The employer-facing rendering of one source phrase: internal sentences
    dropped, the rest glossary-mapped (whole phrase or per sentence) or kept
    verbatim. Empty when nothing employer-facing remains."""
    stripped = text.strip()
    if not stripped:
        return ""
    whole = _GLOSSARY.get(_key(stripped))
    if whole is not None:
        return whole + ("." if stripped.endswith(".") else "")
    sentences = _SENTENCE_SPLIT.split(stripped)
    if len(sentences) > 1:
        return " ".join(part for part in map(public_phrase, sentences) if part)
    return "" if is_internal_text(stripped) else stripped


def public_list(values: Iterable[str]) -> list[str]:
    """`public_phrase` of every item, items with nothing employer-facing left
    omitted (never rendered as blank bullets)."""
    return [phrase for phrase in map(public_phrase, values) if phrase]


def public_description(text: str | None) -> str | None:
    """`text` without internal/provenance sentences, glossary-mapped. None
    when nothing employer-facing remains."""
    if not text:
        return None
    kept = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT.split(text.strip())
        if sentence.strip() and not is_internal_text(sentence)
    ]
    if not kept:
        return None
    return public_phrase(" ".join(kept)) or None


def described_period(text: str | None) -> str | None:
    """The period recorded in an internal 'Period per Lebenslauf: ...' note,
    in German, or None. Only well-formed MM/YYYY or YYYY bounds are used."""
    if not text:
        return None
    found = _PERIOD_NOTE.search(text)
    if found is None:
        return None
    parts = _DASH.split(found.group("period").strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    start, end = (part.strip() for part in parts)
    if not _PERIOD_PART.match(start):
        return None
    if end.casefold() in _CURRENT_WORDS:
        return f"{start} – heute"
    if not _PERIOD_PART.match(end):
        return None
    return f"{start} – {end}"


def _month(value: date | None) -> str | None:
    return value.strftime("%m/%Y") if value is not None else None


def public_period(
    start: date | None,
    end: date | None,
    *,
    current: bool = False,
    description: str | None = None,
) -> str | None:
    """German period: structured dates first, then a period recorded in the
    description note; None when nothing is known (never estimated)."""
    start_text, end_text = _month(start), _month(end)
    if start_text and current and not end_text:
        return f"{start_text} – heute"
    if start_text and end_text:
        return f"{start_text} – {end_text}"
    noted = described_period(description)
    if noted:
        return noted
    if current:
        return "aktuell" if not start_text else f"{start_text} – heute"
    return start_text or end_text


def public_language_level(level: str | None) -> str | None:
    """CEFR levels as-is, NATIVE as 'Muttersprache', anything else
    (UNKNOWN) hidden -- the language is still listed, no level invented."""
    if not level:
        return None
    if level in _CEFR:
        return level
    return _LANGUAGE_LEVELS.get(level)


def public_proficiency(proficiency: str | None) -> str | None:
    return _PROFICIENCY.get(proficiency or "")


def public_certification_status(status: str | None) -> str | None:
    return _CERTIFICATION_STATUS.get(status or "")
