"""Reject out-of-range integer identifiers at the request boundary (audit API-01).

A path/query/body id outside the signed 64-bit range reached the database driver
and escaped as an unhandled `OverflowError` (SQLite) or a `DataError`
(PostgreSQL): a text/plain 500 on 24 API routes and 3 UI pages. No such row can
exist, so the answer is a JSON 422 -- and nothing else is reinterpreted: an
unrelated server-side exception still surfaces as the 500 it is.

* path / query parameters: `reject_out_of_range_ids`, called by the authentication
  dependencies AFTER they succeed (an unauthenticated caller still gets 401 / the
  login redirect, never a 422 that reveals route shapes). It looks only at parameter
  *names* ending in `id` (a free-text `q=1234...` is not an id) and understands the
  integer spellings the endpoints themselves accept (whitespace, `+`/`-`, `N.0`);
* request bodies: the `BigIntId` annotated type on the id / version fields.
"""

import re

from fastapi import HTTPException, Request

from app.models.bounds import MAX_DB_INT, MIN_DB_INT

# Same grammar as the endpoints' own int parsing: sign, digits with single underscores
# between them ("9_223_372_036_854_775_808"), optional ".0".
_INTEGRAL = re.compile(r"[+-]?(\d+(?:_\d+)*)(?:\.0*)?")
_MAX_DIGITS = len(str(MAX_DB_INT))


def _is_id_name(name: str) -> bool:
    return name == "id" or name.endswith("_id") or name.endswith("Id")


def _out_of_range(value: str) -> bool:
    match = _INTEGRAL.fullmatch(value.strip())
    if match is None or not value.strip().isascii():
        return False  # not an integer literal: the normal int validation reports it
    digits = match.group(1).replace("_", "").lstrip("0") or "0"
    if len(digits) > _MAX_DIGITS:
        return True
    number = -int(digits) if value.strip().startswith("-") else int(digits)
    return not (MIN_DB_INT <= number <= MAX_DB_INT)


def reject_out_of_range_ids(request: Request) -> None:
    # getattr: callers (and unit-test stubs) may hand over a request-like object without them
    for source in (
        getattr(request, "path_params", None) or {},
        getattr(request, "query_params", None) or {},
    ):
        for name, value in source.items():
            if _is_id_name(name) and isinstance(value, str) and _out_of_range(value):
                raise HTTPException(
                    status_code=422,
                    detail=f"{name}: identifier out of the supported range",
                )
