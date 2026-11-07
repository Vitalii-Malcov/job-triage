"""FLOW-15: deterministic Telegram refusals never tell the operator to try again."""

import pytest

from app.services import telegram_bewerbung, telegram_bewerbung_approval, telegram_operator_control
from app.services.telegram_notice_policy import (
    APPROVAL_KINDS,
    BEWERBUNG_KINDS,
    DETERMINISTIC_KINDS,
    OPERATOR_KINDS,
    NoticeKind,
    suggests_retry,
)

OPERATOR_TEXTS = {str(code): text for code, text in telegram_operator_control._ANSWERS.items()}

TABLES = [
    ("bewerbung", telegram_bewerbung.NOTICES, BEWERBUNG_KINDS),
    ("approval", telegram_bewerbung_approval.NOTICES, APPROVAL_KINDS),
    ("operator", OPERATOR_TEXTS, OPERATOR_KINDS),
]


@pytest.mark.parametrize(("name", "texts", "kinds"), TABLES, ids=[t[0] for t in TABLES])
def test_every_notice_code_has_a_kind(name, texts, kinds):
    assert set(texts) == set(kinds)


@pytest.mark.parametrize(("name", "texts", "kinds"), TABLES, ids=[t[0] for t in TABLES])
def test_deterministic_notices_do_not_suggest_retry(name, texts, kinds):
    offenders = [
        code
        for code, kind in kinds.items()
        if kind in DETERMINISTIC_KINDS and suggests_retry(texts[code])
    ]
    assert offenders == []


@pytest.mark.parametrize(("name", "texts", "kinds"), TABLES, ids=[t[0] for t in TABLES])
def test_transient_notices_say_what_to_do_or_that_nothing_happened(name, texts, kinds):
    for code, kind in kinds.items():
        if kind is NoticeKind.TRANSIENT:
            assert texts[code].strip()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bitte erneut versuchen.", True),
        ("Try again later.", True),
        ("Gerade nicht möglich.", True),
        ("kein automatischer erneuter Versand", False),
        ("Nothing changed.", False),
        ("Bitte über die API prüfen.", False),
    ],
)
def test_retry_hint_detector(text, expected):
    assert suggests_retry(text) is expected


def test_vacancy_decision_refusal_is_not_phrased_as_temporary():
    from app.services.telegram_bot import VACANCY_NOT_DECIDABLE_ANSWER

    assert not suggests_retry(VACANCY_NOT_DECIDABLE_ANSWER)
