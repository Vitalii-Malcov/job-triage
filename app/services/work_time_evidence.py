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
_VOLLZEIT_WORD = re.compile(r"\bvoll[\s-]?zeit|\bfull[\s-]?time|\bvoll-\s*(?:/|oder|und|&)", re.I)


@dataclass(frozen=True)
class WorkTimeEvidence:
    current_full_time_confirmed: bool
    source: str
    reason: str


def _not(reason: str) -> WorkTimeEvidence:
    return WorkTimeEvidence(False, SOURCE_NONE, reason)


def work_time_evidence(text: str, job_source: str | None, full_time_offered: bool | None):
    """The strict, positive answer to "does this vacancy offer full time right now?"."""
    text = text or ""
    if full_time_offered is False:
        return _not("source flag says full time is not offered")
    signal = classify_work_time(text)
    if signal.mode in {"PART_TIME"}:
        return _not("the text states part time")
    if full_time_denied(text):
        return _not("the text denies or reduces full time")

    fields = [m.group(0) for m in _ANY_FIELD.finditer(text)]
    good = [m for m in _FIELD.finditer(text)]
    if len(good) != len(fields):
        return _not("a working-time field is not a plain Vollzeit/Teilzeit value")
    states_full = any("vollzeit" in m.group("value").lower() for m in good)
    if good and signal.mode == "FULL_TIME" and states_full:
        return WorkTimeEvidence(True, SOURCE_FIELD, "structured working-time field states Vollzeit")

    if full_time_offered is True and job_source == BA_SOURCE:
        if signal.mode == "FULL_TIME" or not _VOLLZEIT_WORD.search(text):
            return WorkTimeEvidence(True, SOURCE_BA_FLAG, "source flag arbeitszeitVollzeit=true")
        return _not("source flag is true but the text mentions Vollzeit in a non-current way")
    return _not("no structured full-time evidence (a heuristic FULL_TIME is not enough)")
