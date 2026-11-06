"""`app.api.http_errors.http_errors`: the declarative twin of the hand-written try/except blocks."""

import logging

import pytest
from fastapi import HTTPException

from app.api.http_errors import http_errors


class Base(Exception):
    pass


class Child(Base):
    pass


class Other(Exception):
    pass


class FakeDb:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _raise(exc):
    with http_errors(
        ((Child, 409, "child"), (Base, 404, "base"), (Other, 422, str)),
        db=(db := FakeDb()),
        log_event="thing_failed",
        failure_detail="Thing failed",
    ):
        raise exc
    return db


def test_the_first_matching_entry_wins_so_a_subclass_listed_first_is_used():
    with pytest.raises(HTTPException) as caught:
        _raise(Child("x"))
    assert (caught.value.status_code, caught.value.detail) == (409, "child")
    with pytest.raises(HTTPException) as caught:
        _raise(Base("x"))
    assert (caught.value.status_code, caught.value.detail) == (404, "base")


def test_a_callable_detail_receives_the_exception_and_the_cause_is_chained():
    with pytest.raises(HTTPException) as caught:
        _raise(Other("explained"))
    assert caught.value.status_code == 422 and caught.value.detail == "explained"
    assert isinstance(caught.value.__cause__, Other)


def test_a_payload_built_from_the_exception_is_supported():
    with pytest.raises(HTTPException) as caught:
        with http_errors(((Other, 409, lambda exc: {"message": str(exc), "n": 3}),)):
            raise Other("boom")
    assert caught.value.detail == {"message": "boom", "n": 3}


def test_a_mapped_error_neither_rolls_back_nor_logs(caplog):
    db = FakeDb()
    with caplog.at_level(logging.WARNING, logger="app.api.routes"):
        with pytest.raises(HTTPException):
            with http_errors(
                ((Other, 409, "x"),), db=db, log_event="thing_failed", failure_detail="Thing failed"
            ):
                raise Other("x")
    assert db.rollbacks == 0 and not caplog.records


def test_an_unexpected_error_rolls_back_logs_only_the_type_and_answers_500(caplog):
    db = FakeDb()
    with caplog.at_level(logging.WARNING, logger="app.api.routes"):
        with pytest.raises(HTTPException) as caught:
            with http_errors((), db=db, log_event="thing_failed", failure_detail="Thing failed"):
                raise RuntimeError("secret row content: subject=Offer")
    assert (caught.value.status_code, caught.value.detail) == (500, "Thing failed")
    assert db.rollbacks == 1
    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["thing_failed error_type=RuntimeError"]
    assert "secret" not in caught.value.detail and "secret" not in messages[0]


def test_without_a_failure_detail_an_unexpected_error_propagates_unchanged():
    with pytest.raises(RuntimeError, match="kept"):
        with http_errors(((Other, 409, "x"),)):
            raise RuntimeError("kept")


def test_an_http_exception_inside_the_block_passes_through():
    with pytest.raises(HTTPException) as caught:
        with http_errors((), db=FakeDb(), log_event="e", failure_detail="failed"):
            raise HTTPException(status_code=418, detail="teapot")
    assert (caught.value.status_code, caught.value.detail) == (418, "teapot")


def test_a_tuple_of_types_is_one_entry():
    with pytest.raises(HTTPException) as caught:
        with http_errors((((Child, Other), 409, "either"),)):
            raise Other("x")
    assert caught.value.detail == "either"
