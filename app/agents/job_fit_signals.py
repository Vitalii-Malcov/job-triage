"""Stage 17C: deterministic signals read from a vacancy's own text.

Pure functions, no I/O. Each returns what the posting states plus a short
evidence snippet, or an explicit UNKNOWN -- never a guess. They feed the
independent criteria of app.services.job_fit_assessment; none of them makes
a decision on its own.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

RemoteMode = Literal["FULLY_REMOTE", "PARTIAL_REMOTE", "HYBRID", "ONSITE", "UNKNOWN"]
Cefr = Literal["A1", "A2", "B1", "B2", "C1", "C2"]
SalaryPeriod = Literal["YEAR", "MONTH"]

_STATES = {
    "baden-württemberg": "Baden-Württemberg",
    "bayern": "Bayern",
    "berlin": "Berlin",
    "brandenburg": "Brandenburg",
    "bremen": "Bremen",
    "hamburg": "Hamburg",
    "hessen": "Hessen",
    "mecklenburg-vorpommern": "Mecklenburg-Vorpommern",
    "niedersachsen": "Niedersachsen",
    "nordrhein-westfalen": "Nordrhein-Westfalen",
    "nrw": "Nordrhein-Westfalen",
    "rheinland-pfalz": "Rheinland-Pfalz",
    "saarland": "Saarland",
    "sachsen-anhalt": "Sachsen-Anhalt",
    "sachsen": "Sachsen",
    "schleswig-holstein": "Schleswig-Holstein",
    "thüringen": "Thüringen",
}
_STATE_ALTERNATION = "|".join(re.escape(name) for name in sorted(_STATES, key=len, reverse=True))


def _snippet(text: str, match: re.Match[str], width: int = 60) -> str:
    start = max(0, match.start() - width // 2)
    end = min(len(text), match.end() + width // 2)
    return " ".join(text[start:end].split())


# --- remote work ----------------------------------------------------------

# "bis zu 100% remote" is an upper bound, not a confirmed fully-remote job.
_UP_TO_REMOTE = re.compile(
    r"bis\s+zu\s+100\s*%\s*(?:remote|home\s*-?\s*office|mobil)", re.IGNORECASE
)
_FULLY_REMOTE = re.compile(
    r"(?:100\s*%|vollständig|vollstaendig|komplett|ausschließlich|ausschliesslich|full(?:y)?)"
    r"[\s-]*(?:remote|im\s+home\s*-?\s*office|home\s*-?\s*office|mobil(?:es\s+arbeiten)?)"
    r"|remote[\s-]*only|standortunabhängig|standortunabhaengig"
    r"|(?:deutschlandweit|bundesweit)[\s-]*remote|remote[\s(]*(?:deutschlandweit|bundesweit)"
    r"|arbeitsort\s*:\s*remote",
    re.IGNORECASE,
)
_NO_REMOTE = re.compile(
    r"kein(?:e|en)?\s+(?:home\s*-?\s*office|remote|mobiles\s+arbeiten)"
    r"|(?:ausschließlich|ausschliesslich|nur)\s+vor\s+ort",
    re.IGNORECASE,
)
_HYBRID = re.compile(
    r"\bhybrid|präsenztag|praesenztag|\d\s*(?:tage?|x)\s*(?:pro\s+woche|/\s*woche|wöchentlich)?"
    r"\s*(?:im\s+büro|im\s+buero|vor\s+ort|präsenz)",
    re.IGNORECASE,
)
_SOME_REMOTE = re.compile(
    r"remote|home\s*-?\s*office|mobiles?\s+arbeit|telearbeit|heimarbeit", re.IGNORECASE
)
_REMOTE_REGION = re.compile(
    r"(?:remote|home\s*-?\s*office|mobil\w*)[^.;\n]{0,40}?(?:aus|in|innerhalb(?:\s+von)?)\s+"
    r"(?:dem\s+|der\s+)?(" + _STATE_ALTERNATION + r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RemoteSignal:
    mode: RemoteMode
    # A German state the remote work is restricted to, if the posting says so.
    region_restriction: str | None = None
    evidence: str | None = None


def classify_remote(text: str) -> RemoteSignal:
    restriction = None
    region = _REMOTE_REGION.search(text)
    if region:
        restriction = _STATES[region.group(1).lower()]
    for pattern, mode in (
        (_NO_REMOTE, "ONSITE"),
        (_UP_TO_REMOTE, "PARTIAL_REMOTE"),
        (_FULLY_REMOTE, "FULLY_REMOTE"),
        (_HYBRID, "HYBRID"),
        (_SOME_REMOTE, "PARTIAL_REMOTE"),
    ):
        match = pattern.search(text)
        if match:
            return RemoteSignal(mode, restriction, _snippet(text, match))
    return RemoteSignal("UNKNOWN")


# --- German language requirement -----------------------------------------

_GERMAN = re.compile(r"deutsch(?!land)\w*|german(?!y)", re.IGNORECASE)
_CEFR = re.compile(r"(?<![A-Za-z0-9])([ABC][12])(?![0-9])")
_DESCRIPTIVE: tuple[tuple[re.Pattern[str], Cefr], ...] = (
    (re.compile(r"muttersprach|native", re.IGNORECASE), "C2"),
    (re.compile(r"verhandlungssicher|fließend|fliessend|fluent|exzellent", re.IGNORECASE), "C1"),
    (re.compile(r"sehr\s+gut\w*|ausgezeichnet\w*|excellent|very\s+good", re.IGNORECASE), "C1"),
    (re.compile(r"\bgut(?:e|en)?\b|\bgood\b|solide", re.IGNORECASE), "B2"),
    (re.compile(r"grundkenntnisse|basic|grundlegend", re.IGNORECASE), "A2"),
)
_OPTIONAL = re.compile(
    r"wünschenswert|wuenschenswert|von\s+vorteil|idealerweise|nice\s+to\s+have|ein\s+plus"
    r"|optional|gerne|bevorzugt|preferred",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LanguageRequirement:
    level: Cefr | None
    # False when the posting softens it (von Vorteil, wünschenswert, ...).
    mandatory: bool | None
    evidence: str | None = None


_CLAUSE_SEPARATORS = (". ", "\n", ";", "•")


def _clause_bounds(text: str, match: re.Match[str]) -> tuple[int, int]:
    start = max(text.rfind(sep, 0, match.start()) for sep in _CLAUSE_SEPARATORS) + 1
    ends = [text.find(sep, match.end()) for sep in _CLAUSE_SEPARATORS]
    return start, min([e for e in ends if e != -1], default=len(text))


def _nearest_descriptor(
    text: str, mention: re.Match[str], clause_start: int, clause_end: int
) -> Cefr | None:
    """The descriptive level word closest to the German mention (before or
    after it, same clause); on equal distance the longer phrase wins, so
    "sehr gute" beats its own "gute"."""
    lo = max(clause_start, mention.start() - 30)
    hi = min(clause_end, mention.end() + 25)
    best: tuple[int, int, Cefr] | None = None
    for pattern, value in _DESCRIPTIVE:
        for match in pattern.finditer(text, lo, hi):
            if match.end() <= mention.start():
                distance = mention.start() - match.end()
            elif match.start() >= mention.end():
                distance = match.start() - mention.end()
            else:
                distance = 0
            key = (distance, -(match.end() - match.start()), value)
            if best is None or key[:2] < best[:2]:
                best = key
    return best[2] if best else None


def extract_german_requirement(text: str) -> LanguageRequirement:
    """The strongest German level the posting asks for, or level None. The
    level is read from a narrow window around each German mention, so
    "sehr gute Englisch- und gute Deutschkenntnisse" yields B2, not C1."""
    order = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
    best: tuple[int, Cefr, bool, str] | None = None
    for mention in _GERMAN.finditer(text):
        clause_start, clause_end = _clause_bounds(text, mention)
        window = text[max(clause_start, mention.start() - 30) : min(clause_end, mention.end() + 25)]
        level: Cefr | None = None
        cefr = _CEFR.search(window)
        if cefr:
            level = cefr.group(1)  # type: ignore[assignment]
        else:
            level = _nearest_descriptor(text, mention, clause_start, clause_end)
        if level is None:
            continue
        clause = text[clause_start:clause_end]
        mandatory = not _OPTIONAL.search(clause)
        candidate = (order[level], level, mandatory, " ".join(clause.split())[:160])
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return LanguageRequirement(level=None, mandatory=None)
    return LanguageRequirement(level=best[1], mandatory=best[2], evidence=best[3])


# --- salary ------------------------------------------------------------------

_TARIFF = re.compile(r"\bTV-?L\b|\bTVöD\b|\bTVoeD\b|tarifvertrag|tarifliche|\bE\s?\d{1,2}\b")
_AMOUNT = r"(\d{1,3}(?:[.\s]\d{3})+|\d{4,6}|\d{2,3}\s?[kK])"
_SALARY = re.compile(
    _AMOUNT + r"(?:\s*(?:€|EUR|Euro))?(?:\s*(?:-|–|bis)\s*" + _AMOUNT + r")?\s*(?:€|EUR|Euro)",
    re.IGNORECASE,
)
_SALARY_PREFIX = re.compile(
    r"(?:€|EUR)\s*" + _AMOUNT + r"(?:\s*(?:-|–|bis)\s*(?:€|EUR)?\s*" + _AMOUNT + r")?",
    re.IGNORECASE,
)
_MONTH = re.compile(r"monat|/\s*m\b|mtl|month", re.IGNORECASE)
_YEAR = re.compile(r"jahr|p\.\s?a\.|jährlich|jaehrlich|annual|year", re.IGNORECASE)


@dataclass(frozen=True)
class SalaryInfo:
    yearly_min: float | None = None
    yearly_max: float | None = None
    # MONTH = the posting gave a monthly figure; yearly values are x12.
    period: SalaryPeriod | None = None
    tariff: bool = False
    evidence: str | None = None


def _amount(raw: str) -> float:
    raw = raw.strip()
    if raw[-1] in "kK":
        return float(raw[:-1].strip()) * 1000
    return float(re.sub(r"[.\s]", "", raw))


def extract_salary(text: str) -> SalaryInfo:
    for pattern in (_SALARY, _SALARY_PREFIX):
        for match in pattern.finditer(text):
            low = _amount(match.group(1))
            high = _amount(match.group(2)) if match.group(2) else low
            context = text[match.end() : match.end() + 40]
            if _MONTH.search(context) or (1000 <= high < 10000 and not _YEAR.search(context)):
                period: SalaryPeriod = "MONTH"
                factor = 12
            elif high >= 20000:
                period, factor = "YEAR", 1
            else:
                continue  # an hourly rate or an unrelated number
            return SalaryInfo(low * factor, high * factor, period, False, _snippet(text, match))
    tariff = _TARIFF.search(text)
    if tariff:
        return SalaryInfo(tariff=True, evidence=_snippet(text, tariff))
    return SalaryInfo()


# --- entry level ---------------------------------------------------------------

_ENTRY_POSITIVE = re.compile(
    r"\bjunior|\btrainee|berufseinst|einsteiger|quereinst|absolvent|graduate|entry[\s-]?level"
    r"|erste\s+(?:berufs)?erfahrung|keine\s+berufserfahrung",
    re.IGNORECASE,
)
_ENTRY_NEGATIVE = re.compile(
    r"\bsenior|\blead\b|\bprincipal|head\s+of|\bleiter|teamleit|architekt|architect"
    r"|mehrjährig|mehrjaehrig|langjährig|langjaehrig|umfangreiche\s+(?:berufs)?erfahrung",
    re.IGNORECASE,
)
_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:jahre|years)[^.;\n]{0,40}?(?:erfahrung|experience)"
    r"|(?:erfahrung|experience)[^.;\n]{0,30}?(\d{1,2})\s*\+?\s*(?:jahre|years)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EntryLevelSignals:
    positive: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    required_years: int | None = None


def entry_level_signals(title: str, text: str) -> EntryLevelSignals:
    combined = f"{title}\n{text}"
    positive = sorted({m.group(0).lower() for m in _ENTRY_POSITIVE.finditer(combined)})
    negative = sorted({m.group(0).lower() for m in _ENTRY_NEGATIVE.finditer(combined)})
    years = [int(m.group(1) or m.group(2)) for m in _YEARS.finditer(combined)]
    return EntryLevelSignals(positive, negative, max(years) if years else None)


# --- start date --------------------------------------------------------------

_IMMEDIATE = re.compile(r"ab\s+sofort|zum\s+nächstmöglichen|zum\s+naechstmoeglichen|asap", re.I)


def asks_immediate_start(text: str) -> bool:
    return bool(_IMMEDIATE.search(text))
