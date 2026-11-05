"""ORM models: common (import via `app.db.models`)."""


def _in_list(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"
