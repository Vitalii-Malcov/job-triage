"""Stage 14D: select the canonical database for one process, explicitly.

The normal `.env` is never modified. Two commands:

    python scripts/stage14d_canonical_runtime.py preflight \
        --database D:/AI_agent/stage14b_canonical_v12.db \
        [--expect-sha256 <hex>]

READ-ONLY: reports DATABASE TARGET, OUTBOUND STATE, ALEMBIC STATE,
MATCHER VERSION and MIGRATION HEAD; the database file's SHA-256 must be
identical before and after. Exit 0 = PASS, 1 = FAIL, 2 = refused input.

    python scripts/stage14d_canonical_runtime.py serve \
        --database D:/AI_agent/stage14b_canonical_v12.db [--port 8000]

Runs the same preflight and, only if it passes, starts the web app on
127.0.0.1 with the canonical overrides (DATABASE_URL, POSTGRES_HOST="", every
safety flag false) applied to THIS process's environment before the
application is imported. A running app writes to its database as normal
operation; use `preflight` (or a scratch copy) when the file must stay
byte-identical.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from app.core.canonical_runtime import (
    CanonicalRuntimeError,
    canonical_environment,
    render_preflight,
    run_preflight,
)

try:
    from scripts.stage14a_send_readiness import UnsafeInputPathError, validate_local_json_path
except ModuleNotFoundError:  # run as `python scripts/...`: scripts/ is sys.path[0]
    from stage14a_send_readiness import UnsafeInputPathError, validate_local_json_path

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _resolve(raw: str) -> Path:
    """Local, absolute path; never opens the file."""
    validate_local_json_path(raw)  # generic local-path rules (no UNC/URI/mapped drive)
    return Path(os.path.abspath(raw))


def _start_server(port: int) -> None:  # pragma: no cover - exercised manually
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=port)


def main(argv: list[str] | None = None, *, start_server=_start_server) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "serve"):
        command = sub.add_parser(name)
        command.add_argument("--database", required=True)
        command.add_argument("--expect-sha256")
        if name == "serve":
            command.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.expect_sha256 is not None and not _SHA256_RE.match(args.expect_sha256):
        print("refused: --expect-sha256 must be 64 hex characters", file=sys.stderr)
        return 2
    try:
        path = _resolve(args.database)
        report = run_preflight(path, expected_sha256=args.expect_sha256)
    except (UnsafeInputPathError, CanonicalRuntimeError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(render_preflight(report))
    if not report.ok:
        return 1
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            print("refused: --port must be 1..65535", file=sys.stderr)
            return 2
        os.environ.update(canonical_environment(report.database_path))
        start_server(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
