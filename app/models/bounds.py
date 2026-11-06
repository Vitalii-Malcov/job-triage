"""Integer range shared by request models and the API boundary (audit API-01)."""

from typing import Annotated

from pydantic import Field

# Signed 64-bit: SQLite INTEGER affinity and PostgreSQL bigint (the widest id column).
MAX_DB_INT = 2**63 - 1

MIN_DB_INT = -(2**63)

BigIntId = Annotated[int, Field(ge=MIN_DB_INT, le=MAX_DB_INT)]
"""Same as `int` for every in-range value; only unrepresentable ones are refused."""
