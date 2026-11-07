"""Audit PERF-02: message text is normalised once per message, not per application pair."""

from datetime import UTC, datetime, timedelta

from app.models.application_lifecycle import Application
from app.services import application_correlation as corr
from app.services.application_correlation import InboundMessage

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _application(index: int, *, submitted_days_ago: int = 3) -> Application:
    return Application.model_construct(
        id=index,
        company_name=f"Firma{index} GmbH",
        role_title=f"Backend Developer {index} (m/w/d)",
        source_reference=f"REF-{index:05d}",
        external_job_id=None,
        employer_domains=(f"firma{index}.example",),
        submitted_at=NOW - timedelta(days=submitted_days_ago),
    )


def _message() -> InboundMessage:
    return InboundMessage(
        account_key="me@example.invalid",
        message_key="mid:perf@firma7.example",
        message_id_header="<perf@firma7.example>",
        reply_to_ids=(),
        from_address="hr@firma7.example",
        from_display_name="Firma7 GmbH Recruiting",
        to_addresses=("me@example.invalid",),
        subject="Ihre Bewerbung als Backend Developer 7",
        body="Guten Tag, vielen Dank fuer Ihre Bewerbung bei Firma7 GmbH. " * 20,
        has_html=False,
        occurred_at=NOW,
    )


def test_body_and_text_are_normalised_once_per_message(monkeypatch):
    calls = {"body": 0, "company_text": 0}
    real_body, real_company = corr.normalize_body, corr.normalize_company_name

    def counting_body(*args, **kwargs):
        calls["body"] += 1
        return real_body(*args, **kwargs)

    def counting_company(text):
        if len(text) > 200:  # only the (long) message text, never an application name
            calls["company_text"] += 1
        return real_company(text)

    monkeypatch.setattr(corr, "normalize_body", counting_body)
    monkeypatch.setattr(corr, "normalize_company_name", counting_company)
    applications = [_application(i) for i in range(60)]
    result = corr.correlate_message(_message(), applications)
    assert calls["body"] == 1  # was 60 (one per application)
    assert calls["company_text"] == 1
    assert result.status in {"MATCHED", "AMBIGUOUS", "UNMATCHED"}


def test_prepared_and_unprepared_evaluations_are_identical():
    message = _message()
    prepared = corr.prepare_message(message)
    for index in range(25):
        application = _application(index)
        assert corr.evaluate_candidate(message, application) == corr.evaluate_candidate(
            message, application, prepared=prepared
        )


def test_the_right_application_still_wins():
    result = corr.correlate_message(_message(), [_application(i) for i in range(30)])
    assert result.status == "MATCHED" and result.application_id == 7


def test_no_application_in_the_window_normalises_nothing(monkeypatch):
    calls = []
    real = corr.normalize_body

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(corr, "normalize_body", counting)
    result = corr.correlate_message(_message(), [_application(1, submitted_days_ago=400)])
    assert calls == [] and result.status == "UNMATCHED"


def test_expected_signals_for_the_matching_and_a_foreign_application():
    message = _message()
    own = corr.evaluate_candidate(message, _application(7))
    assert {"DOMAIN", "COMPANY", "ROLE"} <= set(own.signals)
    assert own.confidence in {"HIGH", "MEDIUM"}
    foreign = corr.evaluate_candidate(message, _application(8))
    assert "DOMAIN" not in foreign.signals and "COMPANY" not in foreign.signals
