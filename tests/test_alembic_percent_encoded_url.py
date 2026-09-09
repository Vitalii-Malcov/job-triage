"""AUD-006 (configuration-only regression test): alembic.config.Config
stores options in a configparser.ConfigParser using BasicInterpolation,
which treats "%" as the start of an interpolation token. A percent-encoded
credential in a PostgreSQL URL (e.g. "%40" standing in for a literal "@"
in a password) makes `Config.set_main_option("sqlalchemy.url", ...)` raise
`ValueError` (configparser.InterpolationSyntaxError) before a single
migration runs -- see alembic/env.py's fix (escaping "%" -> "%%" before
`set_main_option`).

No real database connection is opened anywhere in this module -- these
tests only exercise Alembic's Config object and SQLAlchemy's URL parser.
"""

from pathlib import Path

from alembic.config import Config
from sqlalchemy.engine import make_url

# Read the project's own alembic/env.py by file path rather than
# `import alembic.env` -- this project's alembic/ directory is not a
# Python package (no __init__.py) and is never meant to be imported: it
# is a script executed by Alembic's own ScriptDirectory/EnvironmentContext
# machinery, and its module-level code runs migrations as a side effect
# of being loaded (see tests/test_migrations.py's convention of driving
# it only through `alembic.command.upgrade`). `import alembic.env` would
# also collide with the installed third-party `alembic` package on
# sys.path, which has no `env` submodule at all.
_ENV_PY_SOURCE = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text(
    encoding="utf-8"
)

# Realistic percent-encoded PostgreSQL URLs -- each embeds a credential
# character that is NOT valid unescaped inside a URL's userinfo component
# per RFC 3986, so percent-encoding it is the correct/expected way such a
# password reaches DATABASE_URL.
PERCENT_ENCODED_URLS = [
    # "@" (%40) inside the password.
    ("postgresql+psycopg://user:p%40ssword@localhost:5432/job_search", "p@ssword"),
    # "#" (%23) inside the password.
    ("postgresql+psycopg://user:sec%23ret@localhost:5432/job_search", "sec#ret"),
    # A literal "%" itself, percent-encoded as %25.
    ("postgresql+psycopg://user:has%25percent@localhost:5432/job_search", "has%percent"),
    # Multiple encoded characters in the same credential.
    ("postgresql+psycopg://user:a%40b%23c%25d@localhost:5432/job_search", "a@b#c%d"),
]


def _escape_for_alembic_config(db_url: str) -> str:
    """Mirrors alembic/env.py's own fix exactly -- see that module's
    AUD-006 comment for the full rationale."""
    return db_url.replace("%", "%%")


class TestConfigAcceptsPercentEncodedCredentials:
    def test_set_main_option_does_not_raise_for_percent_encoded_url(self):
        for raw_url, _expected_password in PERCENT_ENCODED_URLS:
            cfg = Config()
            # Before the fix, this line itself raised ValueError
            # (configparser.InterpolationSyntaxError) for every URL in
            # PERCENT_ENCODED_URLS.
            cfg.set_main_option("sqlalchemy.url", _escape_for_alembic_config(raw_url))

    def test_round_trip_through_config_preserves_the_exact_original_url(self):
        for raw_url, _expected_password in PERCENT_ENCODED_URLS:
            cfg = Config()
            cfg.set_main_option("sqlalchemy.url", _escape_for_alembic_config(raw_url))
            assert cfg.get_main_option("sqlalchemy.url") == raw_url

    def test_get_section_also_round_trips_correctly(self):
        """engine_from_config (used by alembic/env.py's
        run_migrations_online) reads the URL via config.get_section(...),
        a separate ConfigParser code path from get_main_option -- both
        must agree on the decoded value."""
        for raw_url, _expected_password in PERCENT_ENCODED_URLS:
            cfg = Config()
            cfg.set_main_option("sqlalchemy.url", _escape_for_alembic_config(raw_url))
            section = cfg.get_section(cfg.config_ini_section, {})
            assert section["sqlalchemy.url"] == raw_url

    def test_sqlalchemy_decodes_the_credential_correctly_after_round_trip(self):
        """The end-to-end proof: after surviving Alembic's Config, the
        URL SQLAlchemy's own parser sees is byte-for-byte the original
        percent-encoded string, which it decodes to the real credential
        -- not a doubled "%%" artifact of the escaping fix."""
        for raw_url, expected_password in PERCENT_ENCODED_URLS:
            cfg = Config()
            cfg.set_main_option("sqlalchemy.url", _escape_for_alembic_config(raw_url))
            resolved = cfg.get_main_option("sqlalchemy.url")
            parsed = make_url(resolved)
            assert parsed.password == expected_password

    def test_unescaped_percent_encoded_url_reproduces_the_original_bug(self):
        """Sanity check that this test module actually detects the bug:
        without the fix's escaping, set_main_option raises for every URL
        in PERCENT_ENCODED_URLS."""
        for raw_url, _expected_password in PERCENT_ENCODED_URLS:
            cfg = Config()
            try:
                cfg.set_main_option("sqlalchemy.url", raw_url)  # no escaping
            except ValueError:
                continue
            raise AssertionError(f"expected set_main_option to raise for {raw_url!r}")


class TestEnvPyContainsTheFix:
    def test_env_py_escapes_percent_before_set_main_option(self):
        """Locks the fix into alembic/env.py itself -- not just proves
        the escaping approach works in the abstract."""
        assert 'db_url.replace("%", "%%")' in _ENV_PY_SOURCE

    def test_env_py_never_logs_the_raw_database_url(self):
        """AUD-006: db_url may contain a real password (percent-encoded
        or not) -- no line that references the db_url/config-url
        variables may also call print(...) or a logger.*(...) method."""
        offending_lines = [
            line
            for line in _ENV_PY_SOURCE.splitlines()
            if "db_url" in line and ("print(" in line or "logger." in line)
        ]
        assert offending_lines == []
