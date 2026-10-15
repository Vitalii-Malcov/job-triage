"""Stage 14A: print profile readiness + READY_FOR_SEND_PACKAGE for one job.

READ-ONLY. Never commits, never sends, never touches Telegram/Gmail/SMTP,
makes no network request. The session is always rolled back. For SQLite
prefer a read-only URI, e.g.

    python scripts/stage14a_send_readiness.py \
        --database-url "sqlite:///file:stage11_pilot.db?mode=ro&uri=true" \
        --job-id 27 \
        --destination-json research/e362b_destination.json \
        --location-json research/e362b_locations.json

`--destination-json` holds one `ApplicationDestination` object and
`--location-json` a list of `LocationEvidence` objects (see
app/models/application_destination.py and app/services/send_readiness.py).
Both are operator research artifacts; omitting them yields UNKNOWN /
"no authoritative evidence", i.e. NOT ready.
"""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.profile_readiness import render_profile_readiness
from app.core.config import Settings
from app.db.models import JobRecord
from app.models.application_destination import ApplicationDestination
from app.services.send_readiness import (
    LocationEvidence,
    evaluate_send_readiness,
    load_send_readiness_inputs,
    render_send_readiness,
)


def _load_json(path: str | None):
    if path is None:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--destination-json")
    parser.add_argument("--location-json")
    args = parser.parse_args(argv)

    raw_destination = _load_json(args.destination_json)
    destination = (
        ApplicationDestination.model_validate(raw_destination) if raw_destination else None
    )
    evidence = [LocationEvidence.model_validate(e) for e in _load_json(args.location_json) or []]

    engine = create_engine(args.database_url)
    try:
        with Session(engine) as db:
            try:
                job = db.get(JobRecord, args.job_id)
                if job is None:
                    print(f"job {args.job_id} not found", file=sys.stderr)
                    return 2
                inputs = load_send_readiness_inputs(
                    db,
                    job,
                    settings=Settings(),
                    destination=destination,
                    location_evidence=evidence,
                )
                print(render_profile_readiness(inputs.profile_report))
                print()
                result = evaluate_send_readiness(inputs)
                print(render_send_readiness(result))
                return 0 if result.ready else 1
            finally:
                db.rollback()
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
