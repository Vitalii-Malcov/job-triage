"""Stage 60: automatic preparation needs CONFIRMED current full-time evidence.

A heuristic FULL_TIME from the text classifier never opens the gate by itself.
"""

from types import SimpleNamespace

import pytest

from app.services.job_fit_assessment import Criterion, work_time_criterion
from app.services.job_fit_assessment import work_time_evidence_criterion as evidence_criterion
from app.services.shortlist_gate import (
    BLOCK_FULL_TIME_NOT_CONFIRMED,
    automatic_block_reason,
)
from app.services.work_time_evidence import work_time_evidence
from tests.test_remote_work_eligibility import _assessment

BA = "bundesagentur"


def _confirmed(text, source=BA, flag=None):
    # description == text: the posting's own text, a field line leads it
    return work_time_evidence(text, source, flag, text).current_full_time_confirmed


@pytest.mark.parametrize(
    ("text", "source", "flag", "expected"),
    [
        ("Python Entwickler, Berlin.", BA, True, True),  # BA flag, nothing contradicts
        ("Python Entwickler, Berlin.", BA, False, False),
        ("Python Entwickler, Berlin.", BA, None, False),  # flag absent, text unknown
        ("Python Entwickler, Berlin.", "xing_email", True, False),  # flag only from BA
        ("Arbeitszeit: Vollzeit", "xing_email", None, True),
        ("Beschäftigungsart: Vollzeit (40 Std.)", "stepstone", None, True),
        ("Arbeitszeit: Teilzeit", BA, True, False),
        ("Arbeitszeit: Vollzeit oder Teilzeit", None, None, True),
        ("Teilzeit, später Vollzeit möglich", BA, True, False),
        ("Vollzeit perspektivisch möglich", BA, True, False),
        ("Vollzeit ab 2027, aktuell Teilzeit", BA, True, False),
        ("Unsere Mitarbeiter arbeiten Vollzeit", BA, True, False),
        ("Teilzeit, Vollzeit nach Einarbeitung", BA, True, False),
        ("Teilzeitmöglichkeiten", None, None, False),
        ("Teilzeitmöglichkeiten", BA, True, True),  # an option, flag says full time
        ("Wir bieten Vollzeit.", None, None, False),  # prose Vollzeit: heuristic only
        ("Vollzeit oder Teilzeit", None, None, False),  # prose: heuristic only
        ("Arbeitszeit: Vollzeit. Teilzeit, später Vollzeit", None, None, False),
        ("Arbeitszeit: Vollzeit\nArbeitszeit: Teilzeit nach Absprache", None, None, False),
        ("Keine Vollzeit möglich", BA, True, False),
        ("Vollzeitnah", BA, True, False),
        ("Arbeitszeit: Vollzeit", BA, False, False),  # text never beats an explicit False
        ("", BA, True, True),
        (None, BA, None, False),
    ],
)
def test_evidence_table(text, source, flag, expected):
    assert _confirmed(text, source, flag) is expected


def _job(flag=None, source=BA, description=""):
    return SimpleNamespace(source=source, full_time_offered=flag, description=description)


def _gate_assessment(text, job):
    a = _assessment(text, hours="")
    a.criteria = [c for c in a.criteria if c.name not in {"work_time", "work_time_evidence"}]
    a.criteria.append(work_time_criterion(text, job))
    a.criteria.append(evidence_criterion(text, job))
    return a


def test_heuristic_full_time_alone_does_not_open_the_gate():
    a = _gate_assessment("Vollzeit, 100% Remote.", _job(None))
    assert next(c.status for c in a.criteria if c.name == "work_time") == "FULL_TIME"
    assert automatic_block_reason(a) == BLOCK_FULL_TIME_NOT_CONFIRMED


def test_ba_flag_opens_the_gate_when_everything_else_passes():
    assert automatic_block_reason(_gate_assessment("100% Remote.", _job(True))) is None


def test_structured_field_opens_the_gate():
    text = "Arbeitszeit: Vollzeit\n100% Remote."
    a = _gate_assessment(text, _job(None, "xing_email", text))
    assert automatic_block_reason(a) is None


def test_contradiction_with_the_flag_is_held():
    a = _gate_assessment("Teilzeit, später Vollzeit möglich. 100% Remote.", _job(True))
    assert automatic_block_reason(a) is not None


def test_old_cached_assessment_without_the_criterion_fails_closed():
    a = _gate_assessment("100% Remote.", _job(True))
    a.criteria = [c for c in a.criteria if c.name != "work_time_evidence"]
    assert automatic_block_reason(a) == BLOCK_FULL_TIME_NOT_CONFIRMED


def test_a_forged_unconfirmed_criterion_stays_blocked():
    a = _gate_assessment("100% Remote.", _job(True))
    a.criteria = [
        Criterion("work_time_evidence", "NOT_CONFIRMED", "x")
        if c.name == "work_time_evidence"
        else c
        for c in a.criteria
    ]
    assert automatic_block_reason(a) == BLOCK_FULL_TIME_NOT_CONFIRMED


def test_a_field_line_inside_employer_prose_is_not_evidence():
    # Codex P2: sample data in the description says nothing about this vacancy's hours
    text = "Du entwickelst Formulare, Beispieldaten:\nArbeitszeit: Vollzeit\n100% Remote."
    assert not work_time_evidence(text, "xing_email", None, text).current_full_time_confirmed
    a = _gate_assessment(text, _job(None, "xing_email", text))
    assert automatic_block_reason(a) == BLOCK_FULL_TIME_NOT_CONFIRMED


def test_a_leading_metadata_block_is_evidence():
    text = "Firma: Example GmbH\nOrt: Berlin\nArbeitszeit: Vollzeit\n\nDu entwickelst Python."
    assert work_time_evidence(text, None, None, text).current_full_time_confirmed
