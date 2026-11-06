"""One place for "domain exception -> HTTP response" in the API routers.

Many endpoints repeated the same shape: call a service inside `try`, map each known domain
exception to a fixed status and detail with its own `except ... raise HTTPException(...) from exc`,
and optionally turn anything unexpected into a sanitized 500 (rollback, one log line with the
exception TYPE only, never its text). `http_errors` states that mapping declaratively:

    with http_errors(
        ((NotFoundError, 404, "Thing not found"), (ConflictError, 409, "Thing conflicts")),
        db=db, log_event="thing_failed", failure_detail="Thing failed",
    ):
        record = service(...)

Rules (identical to the hand-written blocks it replaces):
* mappings are tried IN ORDER, `isinstance` semantics, so a subclass listed first wins;
* a callable `detail` is called with the caught exception (`str` for `str(exc)`, a lambda for a
  payload built from its fields); anything else is used as is;
* the original exception is chained (`from exc`);
* an `HTTPException` raised inside the block passes through unchanged;
* with `failure_detail`, any other `Exception` rolls the session back, logs
  `"<log_event> error_type=<ExceptionType>"` and answers 500 with that detail; without it the
  exception propagates.

Privacy (GMAIL-003): the unexpected-error path never interpolates the caught exception into the
response or the log line - a driver-level database error's own message could embed row content
(subject, body, addresses). Only the exception's TYPE is logged and the response detail is the fixed
generic `failure_detail`.
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

logger = logging.getLogger("app.api.routes")

# (exception type or tuple of types, HTTP status, detail). `detail` is the response detail itself,
# or a callable receiving the caught exception (for `str(exc)` or a payload built from its fields).
ErrorMapping = Sequence[
    tuple[type[Exception] | tuple[type[Exception], ...], int, "str | dict | Callable[..., object]"]
]


@contextmanager
def http_errors(
    mapping: ErrorMapping,
    *,
    db: Session | None = None,
    log_event: str | None = None,
    failure_detail: str | None = None,
) -> Iterator[None]:
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        for exc_type, status_code, detail in mapping:
            if isinstance(exc, exc_type):
                resolved = detail(exc) if callable(detail) else detail
                raise HTTPException(status_code=status_code, detail=resolved) from exc
        if failure_detail is None:
            raise
        if db is not None:
            db.rollback()
        logger.warning("%s error_type=%s", log_event, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=failure_detail
        ) from exc
