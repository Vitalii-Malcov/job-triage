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
# The first entries of _DESCRIPTIVE are fluency/native wording (Stage 22 P1).
_STRONG_DESCRIPTORS = 2
_OPTIONAL = re.compile(
    r"wünschenswert|wuenschenswert|von\s+vorteil|idealerweise|nice\s+to\s+have|ein\s+plus"
    r"|optional|gerne|bevorzugt|preferred",
    re.IGNORECASE,
)


RequirementBasis = Literal[
    "EXPLICIT_REQUIREMENT", "INFERRED_REQUIREMENT", "PREFERRED_REQUIREMENT", "UNKNOWN"
]


@dataclass(frozen=True)
class LanguageRequirement:
    level: Cefr | None
    # False when the posting softens it (von Vorteil, wünschenswert, ...).
    mandatory: bool | None
    evidence: str | None = None
    # Night audit 2026-10-27: "sehr gute Deutschkenntnisse" names no CEFR
    # level or certificate; the C1 read from it is an interpretation
    # (INFERRED_REQUIREMENT), unlike a stated "C1" (EXPLICIT_REQUIREMENT).
    basis: RequirementBasis = "UNKNOWN"
    # Stage 22 P1: the selected requirement is worded as fluency / native
    # ("verhandlungssicher", "fließend", "Muttersprache") or states a C
    # level -- unlike "sehr gute", which reads as C1 but is only an inference.
    strong: bool = False


_CLAUSE_BREAK = re.compile(r"\.\s|\n|;|•")
# "(mind. B2)", "z. B.": a period after these does not end the statement.
_ABBREVIATIONS = frozenset(
    {"mind", "min", "max", "bzw", "ca", "inkl", "ggf", "evtl", "bspw", "etc", "vgl", "sog", "nr"}
    | {"z", "b", "u", "a", "d", "h", "usw"}
)
_OTHER_LANGUAGE = re.compile(
    r"englisch|english|französisch|franzoesisch|french|spanisch|spanish|italienisch"
    r"|polnisch|russisch|türkisch|tuerkisch|niederländisch|niederlaendisch|chinesisch|arabisch",
    re.IGNORECASE,
)


# Parts of a German requirement scope: split at commas and conjunctions, so
# "B2 zwingend erforderlich und C1 wünschenswert" are two statements.
_PART_BOUNDARY = re.compile(r",|\s(?:und|sowie|oder|bzw\.?)\s", re.IGNORECASE)
# A following part that is nothing but a softener ("..., von Vorteil")
# applies to the part before it; any other content starts a new statement.
_BARE_SOFTENER = re.compile(
    r"^\W*(?:(?:ist|sind|wäre|waere|wären|waeren)\s+)?(?:" + _OPTIONAL.pattern + r")\W*$",
    re.IGNORECASE,
)
# A CEFR level this far after the German mention still belongs to it.
# Codex round 9: "Deutschkenntnisse für die ... Kommunikation mit Kunden auf
# mindestens B2-Niveau" states the level well after the mention.
_LEVEL_REACH = 140
# "Führerschein Klasse C1" is a driving licence, not a language level.
_LICENCE = re.compile(r"(?:f(?:ü|ue)hrerschein|fahrerlaubnis|klasse)\W*$", re.IGNORECASE)
# "C1/C2", "B2–C1", "C1, C2 oder Muttersprache": alternatives or a range;
# the lowest one is the minimum asked.
_ALTERNATIVE_JOIN = re.compile(r"\s*(?:[/,–-]|oder|bis)\s*", re.IGNORECASE)
# "Deutsch- und Englischkenntnisse", "Deutsch und Englisch auf B2-Niveau":
# languages joined directly by und/sowie/oder share what follows them (Codex
# round 9); a comma list ("Deutsch, Englisch von Vorteil") stays separate.
_COMPOUND_CONTINUES = re.compile(r"-?\s(?:und|sowie|oder)\s+", re.IGNORECASE)


def _parts(text: str, lo: int, hi: int) -> list[tuple[int, int]]:
    bounds, begin = [], lo
    for boundary in _PART_BOUNDARY.finditer(text, lo, hi):
        bounds.append((begin, boundary.start()))
        begin = boundary.end()
    bounds.append((begin, hi))
    return bounds


def _part_optional(text: str, parts: list[tuple[int, int]], index: int) -> bool:
    begin, end = parts[index]
    if _OPTIONAL.search(text, begin, end):
        return True
    following = parts[index + 1] if index + 1 < len(parts) else None
    return following is not None and bool(_BARE_SOFTENER.match(text[following[0] : following[1]]))


def _part_index(parts: list[tuple[int, int]], offset: int) -> int:
    return max(i for i, (begin, _) in enumerate(parts) if begin <= offset)


def _german_scope(text: str, mention: re.Match[str], clause: tuple[int, int]) -> tuple[int, int]:
    """The span that can state this German mention's level: never across
    another language mention or another German mention (Codex rounds 7-8:
    "Deutsch B2 zwingend erforderlich und Englisch C1 wünschenswert")."""
    lo = max(clause[0], mention.start() - 30)
    for other in _OTHER_LANGUAGE.finditer(text, lo, mention.start()):
        lo = other.end()
    hi = clause[1]
    begin = mention.end()
    compound = _COMPOUND_CONTINUES.match(text, mention.end())
    if compound is not None:
        # The shared ending and anything after it (a softener) belong to
        # German too; a word between them ("idealerweise auch") does not.
        partner = _OTHER_LANGUAGE.match(text, compound.end())
        if partner is not None:
            begin = partner.end()
    for pattern in (_OTHER_LANGUAGE, _GERMAN):
        following = pattern.search(text, begin, hi)
        if following is not None:
            hi = following.start()
    return lo, hi


def _merge_compound(
    text: str, mention: re.Match[str], parts: list[tuple[int, int]], own: int
) -> list[tuple[int, int]]:
    """Join "Deutsch-" with the "und Englischkenntnisse ..." part it shares."""
    if _COMPOUND_CONTINUES.match(text, mention.end()) is None or own + 1 >= len(parts):
        return parts
    nxt = parts[own + 1]
    piece = text[nxt[0] : nxt[1]]
    partner = _OTHER_LANGUAGE.match(text, nxt[0] + len(piece) - len(piece.lstrip()))
    if partner is None:
        return parts
    return [*parts[:own], (parts[own][0], nxt[1]), *parts[own + 2 :]]


def _clause_bounds(text: str, match: re.Match[str]) -> tuple[int, int]:
    start, end = 0, len(text)
    for brk in _CLAUSE_BREAK.finditer(text):
        if brk.group().startswith("."):
            word = re.search(r"(\w+)$", text[max(0, brk.start() - 12) : brk.start()])
            if word is not None and word.group(1).casefold() in _ABBREVIATIONS:
                continue
        if brk.end() <= match.start():
            start = brk.end()
        elif brk.start() >= match.end():
            end = brk.start()
            break
    return start, end


def _nearest_descriptor(
    text: str, mention: re.Match[str], clause_start: int, clause_end: int
) -> tuple[Cefr, bool] | None:
    """The descriptive level word closest to the German mention (before or
    after it, same clause); on equal distance the longer phrase wins, so
    "sehr gute" beats its own "gute"."""
    lo = max(clause_start, mention.start() - 30)
    hi = min(clause_end, mention.end() + 25)
    best: tuple[int, int, Cefr, bool] | None = None
    for index, (pattern, value) in enumerate(_DESCRIPTIVE):
        strong = index < _STRONG_DESCRIPTORS
        for match in pattern.finditer(text, lo, hi):
            if match.end() <= mention.start():
                distance = mention.start() - match.end()
            elif match.start() >= mention.end():
                distance = match.start() - mention.end()
            else:
                distance = 0
            key = (distance, -(match.end() - match.start()), value, strong)
            if best is None or key[:2] < best[:2]:
                best = key
    return (best[2], best[3]) if best else None


def extract_german_requirement(text: str) -> LanguageRequirement:
    """The strongest mandatory German level the posting asks for (an
    optional level only when none is mandatory), or level None. The
    level is read from a narrow window around each German mention, so
    "sehr gute Englisch- und gute Deutschkenntnisse" yields B2, not C1."""
    order = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6}
    best: tuple[int, int, int, Cefr, str] | None = None
    for mention in _GERMAN.finditer(text):
        clause_start, clause_end = _clause_bounds(text, mention)
        lo, hi = _german_scope(text, mention, (clause_start, clause_end))
        parts = _parts(text, lo, hi)
        own = _part_index(parts, mention.start())
        parts = _merge_compound(text, mention, parts, own)
        evidence = " ".join(text[clause_start:clause_end].split())[:160]
        # Every stated CEFR level is judged in its own part (Codex round 8:
        # "Deutsch C1 wünschenswert, B2 zwingend erforderlich" states a
        # mandatory B2). A level before the mention counts only in the
        # mention's own part ("C1 Deutsch", not "Englisch C1, Deutsch").
        levels: list[tuple[Cefr, bool, bool, bool]] = []
        previous: re.Match[str] | None = None
        for cefr in _CEFR.finditer(text, lo, min(hi, mention.end() + _LEVEL_REACH)):
            index = _part_index(parts, cefr.start())
            if cefr.end() <= mention.start() and index != own:
                continue
            if _LICENCE.search(text, max(0, cefr.start() - 25), cefr.start()):
                continue
            level: Cefr = cefr.group(1)  # type: ignore[assignment]
            if previous is not None and _ALTERNATIVE_JOIN.fullmatch(
                text, previous.end(), cefr.start()
            ):
                # Same statement: keep the lower level of the alternatives.
                last_level, last_mandatory, _, _ = levels[-1]
                lower = min(last_level, level, key=order.__getitem__)
                levels[-1] = (lower, last_mandatory, True, lower in ("C1", "C2"))
                previous = cefr
                continue
            levels.append(
                (level, not _part_optional(text, parts, index), True, level in ("C1", "C2"))
            )
            previous = cefr
        if not levels:
            descriptor = _nearest_descriptor(text, mention, clause_start, clause_end)
            if descriptor is not None:
                value, strong = descriptor
                levels.append((value, not _part_optional(text, parts, own), False, strong))
        for level, mandatory, explicit, strong in levels:
            # A mandatory level beats any optional one (Codex round 6), then
            # the higher level; an explicit level beats the same inferred.
            candidate = (
                1 if mandatory else 0,
                order[level],
                1 if explicit else 0,
                # Stage 22 P1 (Codex): on an otherwise equal requirement the
                # fluency wording wins, independent of sentence order.
                1 if strong else 0,
                level,
                evidence,
                strong,
            )
            if best is None or candidate[:4] > best[:4]:
                best = candidate
    if best is None:
        return LanguageRequirement(level=None, mandatory=None)
    mandatory_rank, _, explicit, _, level, evidence, strong = best
    mandatory = bool(mandatory_rank)
    if not mandatory:
        basis: RequirementBasis = "PREFERRED_REQUIREMENT"
    else:
        basis = "EXPLICIT_REQUIREMENT" if explicit else "INFERRED_REQUIREMENT"
    return LanguageRequirement(
        level=level, mandatory=mandatory, evidence=evidence, basis=basis, strong=strong
    )


# --- salary ------------------------------------------------------------------
#
# Night audit 2026-10-27: "IT-Zulage 1.500 € mtl." was read as the base pay
# (18,000 EUR/year), "Weihnachtsgeld 1.000 €" as 12,000 EUR/year and
# "Jahresbonus bis 5.000 €" as 60,000 EUR/year; "x 13 Gehälter" was counted
# x12. Every amount is now read together with the words just before it: an
# allowance/bonus is reported as a component and never becomes the salary,
# an hourly rate stays hourly, and an estimate shown by a job board is
# labelled as such instead of passing for the employer's figure.

_TARIFF = re.compile(r"\bTV-?L\b|\bTVöD\b|\bTVoeD\b|tarifvertrag|tarifliche|\bE\s?\d{1,2}\b")
_AMOUNT = r"(\d{1,3}(?:[.\s]\d{3})+|\d{4,6}|\d{2,3}\s?[kK])"
_SALARY = re.compile(
    _AMOUNT + r"(?:\s*(?:€|EUR|Euro))?(?:\s*(?:-|–|bis)\s*" + _AMOUNT + r")?\s*(?:€|EUR|Euro)",
    re.IGNORECASE,
)
# Stage 19: "zwischen 42.000 und 48.000 €" is a range (both bounds), not
# two amounts of which only the second one was kept.
_SALARY_BETWEEN = re.compile(
    r"zwischen\s+" + _AMOUNT + r"(?:\s*(?:€|EUR|Euro))?\s+und\s+" + _AMOUNT + r"\s*(?:€|EUR|Euro)",
    re.IGNORECASE,
)
_SALARY_PREFIX = re.compile(
    r"(?:€|EUR)\s*" + _AMOUNT + r"(?:\s*(?:-|–|bis)\s*(?:€|EUR)?\s*" + _AMOUNT + r")?",
    re.IGNORECASE,
)
_MONTH = re.compile(r"monat|/\s*m\b|mtl|month", re.IGNORECASE)
_MONTHS_COUNT = re.compile(r"monatsgeh(?:ä|ae)lter|monatsgehalt", re.IGNORECASE)
_YEAR = re.compile(r"jahr|p\.\s?a\.|jährlich|jaehrlich|annual|year", re.IGNORECASE)
_HOURLY_AMOUNT = r"(\d{1,3}(?:,\d{1,2})?)"
_HOURLY = re.compile(
    r"stundenlohn\D{0,20}?"
    + _HOURLY_AMOUNT
    + r"\s*(?:€|EUR|Euro)|"
    + _HOURLY_AMOUNT
    + r"\s*(?:€|EUR|Euro)\s*(?:/|pro|je)\s*(?:std\.?|stunde|h)\b",
    re.IGNORECASE,
)
# Pay components that are NOT the base salary, by the word naming them.
_COMPONENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ALLOWANCE",
        re.compile(
            r"zulage|zuschuss|zuschlag|pauschale|budget|jobticket|gutschein|"
            r"fahrtkosten|erstattung|inflationsausgleich|einmalzahlung|allowance",
            re.IGNORECASE,
        ),
    ),
    (
        "BONUS",
        re.compile(
            r"bonus|prämie|praemie|provision|tantieme|gewinnbeteiligung|"
            r"erfolgsbeteiligung|variable[nr]?\s+(?:anteil|vergütung|gehalt)",
            re.IGNORECASE,
        ),
    ),
    ("CHRISTMAS_BONUS", re.compile(r"weihnachtsgeld", re.IGNORECASE)),
    ("HOLIDAY_PAY", re.compile(r"urlaubsgeld", re.IGNORECASE)),
)
_LEAD_BOUNDARY = re.compile(r"[.;:!?\n•]|,\s|\s[-–]\s|\b(?:und|sowie|plus|zzgl\.?|zuzüglich)\b")
_UP_TO = re.compile(r"(?:bis\s+zu|maximal|max\.|höchstens)\s*$", re.IGNORECASE)
_ESTIMATE = re.compile(
    r"gesch(?:ä|ae)tzt|sch(?:ä|ae)tzung|estimate|prognose|gehaltsspanne\s+laut|"
    r"laut\s+(?:xing|stepstone|indeed|kununu|glassdoor)|gehaltsrechner|gehaltsvergleich",
    re.IGNORECASE,
)
_SALARIES_PER_YEAR = re.compile(
    r"(?:x|×|\*)\s*(1[34])\b|\b(1[34])\s*(?:monats)?geh(?:ä|ae)lter|\b(1[34])\.\s*monatsgehalt",
    re.IGNORECASE,
)
_NEGOTIABLE = re.compile(
    r"gehalt\w*\s+(?:nach|n\.)\s+(?:vereinbarung|absprache)|nach\s+vereinbarung|"
    r"verhandlungssache|verhandelbar|\bVB\b",
    re.IGNORECASE,
)

SalaryProvenance = Literal["EMPLOYER_STATED", "JOB_BOARD_ESTIMATE"]


@dataclass(frozen=True)
class SalaryComponent:
    kind: str  # ALLOWANCE | BONUS | CHRISTMAS_BONUS | HOLIDAY_PAY
    evidence: str


@dataclass(frozen=True)
class SalaryInfo:
    # Base salary per year. `yearly_min` is None for an upper bound only
    # ("bis zu 55.000 €"); both None when no base salary is stated.
    yearly_min: float | None = None
    yearly_max: float | None = None
    # MONTH = the posting gave a monthly figure; yearly values are
    # x `salaries_per_year` (12 unless the posting states 13/14).
    period: SalaryPeriod | None = None
    tariff: bool = False
    evidence: str | None = None
    provenance: SalaryProvenance | None = None
    salaries_per_year: int | None = None
    hourly_min: float | None = None
    hourly_max: float | None = None
    negotiable: bool = False
    components: tuple[SalaryComponent, ...] = ()


def _amount(raw: str) -> float:
    raw = raw.strip()
    if raw[-1] in "kK":
        return float(raw[:-1].strip()) * 1000
    return float(re.sub(r"[.\s]", "", raw))


def _lead(text: str, start: int, width: int = 60) -> str:
    """The words before an amount, back to the nearest clause boundary."""
    window = text[max(0, start - width) : start]
    boundaries = list(_LEAD_BOUNDARY.finditer(window))
    return window[boundaries[-1].end() :] if boundaries else window


def _trail(text: str, end: int) -> str:
    """The noun right after an amount ("1.500 € IT-Zulage"): at most two
    words, never across "+", "(" or a clause boundary, so "€150000 (EUR) +
    20% bonus" keeps the salary."""
    window = re.split(r"[+(]", text[end : end + 40], maxsplit=1)[0]
    boundary = _LEAD_BOUNDARY.search(window)
    window = window[: boundary.start()] if boundary else window
    return " ".join(re.findall(r"[\w-]+", window)[:2])


_SENTENCE_END = re.compile(r"[!?\n]|\.\s")


def _sentence(text: str, match: re.Match[str]) -> str:
    starts = [m.end() for m in _SENTENCE_END.finditer(text, 0, match.start())]
    end = _SENTENCE_END.search(text, match.end())
    return text[(starts[-1] if starts else 0) : end.start() if end else len(text)]


def _component(lead: str, trail: str) -> str | None:
    for kind, pattern in _COMPONENTS:
        if pattern.search(lead) or pattern.search(trail):
            return kind
    return None


def _salary_matches(text: str) -> list[re.Match[str]]:
    found: list[re.Match[str]] = []
    for pattern in (_SALARY_BETWEEN, _SALARY, _SALARY_PREFIX):
        for match in pattern.finditer(text):
            if not any(m.start() < match.end() and match.start() < m.end() for m in found):
                found.append(match)
    return sorted(found, key=lambda m: m.start())


def extract_salary(text: str) -> SalaryInfo:
    components: list[SalaryComponent] = []
    candidates: list[SalaryInfo] = []
    per_year = _SALARIES_PER_YEAR.search(text)
    months = int(next(g for g in per_year.groups() if g)) if per_year else 12
    for match in _salary_matches(text):
        lead = _lead(text, match.start())
        trail = text[match.end() : match.end() + 40]
        kind = _component(lead, _trail(text, match.end()))
        if kind is not None:
            components.append(SalaryComponent(kind, _snippet(text, match)))
            continue
        low = _amount(match.group(1))
        high = _amount(match.group(2)) if match.group(2) else low
        # "59.724 Euro (14 Monatsgehälter)" is a yearly figure: the word
        # "Monatsgehälter" counts salaries, it is not a period, and no
        # monthly salary reaches 15,000.
        monthly_word = _MONTH.search(_MONTHS_COUNT.sub(" ", trail))
        if (monthly_word and high < 15000) or (1000 <= high < 10000 and not _YEAR.search(trail)):
            period: SalaryPeriod = "MONTH"
            factor = months
        elif high >= 20000:
            period, factor = "YEAR", 1
        else:
            continue  # an hourly rate or an unrelated number
        candidates.append(
            SalaryInfo(
                yearly_min=None if _UP_TO.search(lead) else low * factor,
                yearly_max=high * factor,
                period=period,
                evidence=_snippet(text, match),
                provenance=(
                    "JOB_BOARD_ESTIMATE"
                    if _ESTIMATE.search(_sentence(text, match))
                    else "EMPLOYER_STATED"
                ),
                salaries_per_year=factor if period == "MONTH" else None,
            )
        )
    hourly = _HOURLY.search(text)
    hourly_value = float((hourly.group(1) or hourly.group(2)).replace(",", ".")) if hourly else None
    extras = dict(
        hourly_min=hourly_value,
        hourly_max=hourly_value,
        negotiable=bool(_NEGOTIABLE.search(text)),
        components=tuple(components),
    )
    # The employer's own figure wins over a job board's estimate.
    chosen = next((c for c in candidates if c.provenance == "EMPLOYER_STATED"), None) or (
        candidates[0] if candidates else None
    )
    if chosen is not None:
        return SalaryInfo(**{**chosen.__dict__, **extras})
    tariff = _TARIFF.search(text)
    if tariff:
        return SalaryInfo(tariff=True, evidence=_snippet(text, tariff), **extras)
    if hourly:
        return SalaryInfo(evidence=_snippet(text, hourly), **extras)
    return SalaryInfo(**extras)


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
