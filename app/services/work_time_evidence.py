"""Stage 60: CONFIRMED current full-time evidence (the trust boundary for automatic preparation).

`classify_work_time` is a text heuristic that helps a human read a posting. A heuristic
FULL_TIME ("the word Vollzeit appears somewhere") must never by itself open the automatic
preparation gate: "Teilzeit, spaeter Vollzeit moeglich" and its many wordings are exactly the
class of text a regex cannot settle. Automatic preparation instead needs one of two narrow,
checkable positive sources, and neither may be contradicted by the text:

1. SOURCE FLAG -- the Bundesagentur `arbeitszeitVollzeit=true` of THIS posting
   (`jobs.full_time_offered`,
   only ever written by the BA collector for that posting), and the text neither says part time,
   denies full time, nor mentions Vollzeit in any way the classifier does not read as a plain
   current statement (deferred / future / staff / reduced).
2. STRUCTURED FIELD -- a whole line `Arbeitszeit: Vollzeit` (also Beschaeftigungsart,
   Anstellungsart, Arbeitszeitmodell) whose value is only Vollzeit / Teilzeit
   options, with the classifier agreeing on FULL_TIME and the source flag not saying False.

Everything else (UNKNOWN, CONFLICT, PART_TIME, a prose mention of Vollzeit, a future or staff
Vollzeit) is NOT_CONFIRMED. Fail-closed. Pure: no I/O, writes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.job_fit_signals import classify_work_time, full_time_denied

CRITERION_NAME = "work_time_evidence"
CONFIRMED = "CONFIRMED"
NOT_CONFIRMED = "NOT_CONFIRMED"

SOURCE_BA_FLAG = "bundesagentur_flag"
SOURCE_FIELD = "structured_field"
SOURCE_NONE = "none"

BA_SOURCE = "bundesagentur"

_OPTION = r"(?:vollzeit|teilzeit)"
_FIELD = re.compile(
    r"^[ \t>*\-]*(?:arbeitszeit(?:modell)?|besch(?:ä|ae)ftigungsart|anstellungsart)"
    r"[ \t]*[:：][ \t]*"
    rf"(?P<value>{_OPTION}(?:[ \t]*(?:/|,|oder|und|&)[ \t]*{_OPTION})*)"
    r"[ \t]*(?:\([^()\n]{0,40}\))?[ \t.]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ANY_FIELD = re.compile(
    r"^[ \t>*\-]*(?:arbeitszeit(?:modell)?|besch(?:ä|ae)ftigungsart|anstellungsart)"
    r"[ \t]*[:：][^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_KEY_VALUE = re.compile(
    r"^[ 	>*\-]*[^\W\d_][\w .\-/äöüÄÖÜß]{0,40}[ 	]*[:：][ 	]*\S.{0,80}$"
)
# Fail-closed numeric guard: ANY stated weekly hours below full time, a part-time percentage or a
# reduced job form anywhere in the text means "not confirmed" (over-blocking is the safe
# direction; the vacancy stays in the review queue).
_REDUCED_HOURS = re.compile(
    r"\b(?:[1-9]|[12]\d|3[0-4])(?:[.,]\d+)?\s*(?:-\s*(?:\d{1,2}\s*)?)?"
    r"(?:wochen\s*stunden|stunden|std\b\.?|h\b|hours?)"
    r"|\b(?:halbtag|minijob|midijob|werkstudent|geringf(?:ü|ue)gig)",
    re.IGNORECASE,
)
# A share of working time ("50 %-Stelle", "0,5 %-Stelle", "Beschaeftigungsgrad: 60,5 %"): any value
# other than exactly 100 is reduced. Parsed numerically, so no decimal/zero shape can slip past.
_SHARE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,]\d+)?)\s*%(?P<after>[\s-]*(?:stelle|besch|teilzeit|arbeit))?"
    r"|besch(?:ä|ae)ftigungsgrad\W{0,6}(?P<grad>\d{1,3}(?:[.,]\d+)?)\s*%",
    re.IGNORECASE,
)


def _reduced_share(text: str) -> bool:
    for m in _SHARE.finditer(text):
        raw = m.group(1) or m.group("grad")
        if not raw or not (m.group("after") or m.group("grad")):
            continue
        if float(raw.replace(",", ".")) != 100.0:
            return True
    return False


_VOLLZEIT_WORD = re.compile(r"\bvoll[\s-]?zeit|\bfull[\s-]?time|\bvoll-\s*(?:/|oder|und|&)", re.I)


@dataclass(frozen=True)
class WorkTimeEvidence:
    current_full_time_confirmed: bool
    source: str
    reason: str


def _not(reason: str) -> WorkTimeEvidence:
    return WorkTimeEvidence(False, SOURCE_NONE, reason)


def _leading_field_block(description: str) -> str:
    """The description's leading metadata block: consecutive non-empty `Key: value` lines from the
    very start. A field line after prose (an example, a quote, "folgende Beispieldaten:") is
    employer free text, not structured data, and is never evidence."""
    lines: list[str] = []
    for line in description.splitlines():
        if not line.strip():
            if lines:
                break
            continue
        if not _KEY_VALUE.match(line):
            break
        lines.append(line)
    return "\n".join(lines)


def work_time_evidence(
    text: str,
    job_source: str | None,
    full_time_offered: bool | None,
    description: str | None = None,
):
    """The strict, positive answer to "does this vacancy offer full time right now?"."""
    text = text or ""
    if full_time_offered is False:
        return _not("source flag says full time is not offered")
    signal = classify_work_time(text)
    if signal.mode in {"PART_TIME"}:
        return _not("the text states part time")
    if full_time_denied(text):
        return _not("the text denies or reduces full time")

    # any working-time field line anywhere must be a plain Vollzeit/Teilzeit value, but only the
    # description's leading metadata block can CONFIRM
    if _REDUCED_HOURS.search(text) or _reduced_share(text):
        return _not("the text states reduced weekly hours or a reduced job form")
    fields = [m.group(0) for m in _ANY_FIELD.finditer(text)]
    all_good = [m for m in _FIELD.finditer(text)]
    block = _leading_field_block(description or "")
    good = [m for m in _FIELD.finditer(block)]
    if len(all_good) != len(fields):
        return _not("a working-time field is not a plain Vollzeit/Teilzeit value")
    states_full = any("vollzeit" in m.group("value").lower() for m in good)
    if good and signal.mode == "FULL_TIME" and states_full:
        return WorkTimeEvidence(True, SOURCE_FIELD, "structured working-time field states Vollzeit")

    if full_time_offered is True and job_source == BA_SOURCE:
        if signal.mode == "FULL_TIME" or not _VOLLZEIT_WORD.search(text):
            return WorkTimeEvidence(True, SOURCE_BA_FLAG, "source flag arbeitszeitVollzeit=true")
        return _not("source flag is true but the text mentions Vollzeit in a non-current way")
    return _not("no structured full-time evidence (a heuristic FULL_TIME is not enough)")
