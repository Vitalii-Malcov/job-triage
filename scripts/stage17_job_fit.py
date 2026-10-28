"""Stage 17C: operator CLI for the per-vacancy fit criteria and car routes.

    python scripts/stage17_job_fit.py --database DB assess JOB_ID [--json]
    python scripts/stage17_job_fit.py --database DB record-commute JOB_ID --car-km 85 \
        --destination "Darmstadt, Hilpertstr. 1" --source-note "Google Maps 27.10." \
        [--minutes 62] [--region Hessen] [--apply]
    python scripts/stage17_job_fit.py --database DB check-commute JOB_ID \
        --dest-lat 49.87 --dest-lon 8.65 --destination "Darmstadt, ..." \
        [--precision ADDRESS|STREET|POSTCODE|CITY] [--region Hessen] [--apply]
    python scripts/stage17_job_fit.py --database DB check-commute JOB_ID \
        --address "Hilpertstr. 1, 64295 Darmstadt" [--region Hessen] [--apply]

`assess` is read-only: one line per independent criterion (skills,
experience, language, location, remote, salary, entry level, start date,
duplicate) and a triage decision -- never one overall score, never an
instruction to apply.

`record-commute` stores a car route the operator measured. `check-commute`
asks the OSRM routing service for the car route between the configured
commute origin and the given workplace coordinates (only the two coordinate
pairs are sent); it is the only command here that makes a network request,
and only when invoked. Both are DRY RUNS unless `--apply`. A straight-line
distance is never stored as a car route.

Night audit 2026-10-27: `--address` geocodes the employer's workplace
address (OpenStreetMap Nominatim: 1 request/s, identifying User-Agent,
results cached in runtime/geocode_cache.local.json; only that address text
is sent). The coordinates' precision decides the method: a building-level
result is OSRM, anything coarser (street, postcode, city centre) is stored
as OSRM_APPROX / `record-commute --approximate` as MANUAL_APPROX and is
never treated as the verified workplace route. A route between coordinates
already measured is reused from the database, without a network request.

Same database target guard as the Stage 15 tracker. Exit codes: 0 ok, 1 failure, 2 refused input.
"""

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from app.agents.workplace_address import address_words
from app.db.candidate_search_repository import load_search_config
from app.db.job_commute_repository import (
    CommuteCheckInputError,
    destination_marker,
    find_cached_route,
    record_commute_check,
)
from app.models.candidate_search import GeoPoint
from app.providers.geocoding.nominatim import (
    Geocoder,
    GeocodingUnavailableError,
    NominatimGeocoder,
)
from app.providers.routing.osrm import (
    CarRouteProvider,
    OsrmRouteProvider,
    RouteUnavailableError,
    straight_line_km,
)
from app.services.job_fit_assessment import assess_job


def _assess(session: Session, args) -> int:
    try:
        assessment = assess_job(session, args.job_id)
    except LookupError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(
            json.dumps(
                {
                    "job_id": assessment.job_id,
                    "company": assessment.company,
                    "title": assessment.title,
                    "decision": assessment.decision,
                    "criteria": [c.__dict__ for c in assessment.criteria],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"JOB {assessment.job_id}: {assessment.company} | {assessment.title}")
    for criterion in assessment.criteria:
        print(f"  {criterion.name:<12} {criterion.status:<20} {criterion.reason}")
        if criterion.evidence:
            print(f"  {'':<12} {'':<20} evidence: {criterion.evidence}")
    print(f"DECISION: {assessment.decision}")
    for blocker in assessment.blockers:
        print(f"  BLOCKER {blocker.name}: {blocker.status}")
    for check in assessment.verifications:
        print(f"  VERIFY  {check.name}: {check.status}")
    return 0


def _origin(session: Session) -> tuple[str, GeoPoint | None] | None:
    config = load_search_config(session)
    if config is None or not config.search.commute_origin_label:
        return None
    return config.search.commute_origin_label, config.search.commute_origin


def _store(session: Session, args, *, km: float, minutes, method: str, note: str) -> int:
    origin = _origin(session)
    if origin is None:
        print(
            "REFUSED: no commute origin configured (stage17_candidate_config.py)", file=sys.stderr
        )
        return 2
    print(
        f"{'APPLY' if args.apply else 'DRY RUN'}: job {args.job_id} car route {km:.1f} km "
        f"from {origin[0]} to {args.destination} ({method}; {note})"
    )
    if not args.apply:
        print("dry run only; re-run with --apply to store this car route")
        return 0
    try:
        record = record_commute_check(
            session,
            job_id=args.job_id,
            origin_label=origin[0],
            destination_label=args.destination,
            destination_region=args.region,
            origin_lat=origin[1].lat if origin[1] else None,
            origin_lon=origin[1].lon if origin[1] else None,
            car_route_km=km,
            car_route_minutes=minutes,
            method=method,
            source_note=note,
        )
    except CommuteCheckInputError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(f"STORED: commute check {record.id}")
    return 0


def _record_commute(session: Session, args) -> int:
    return _store(
        session,
        args,
        km=args.car_km,
        minutes=args.minutes,
        method="MANUAL_APPROX" if args.approximate else "MANUAL",
        note=args.source_note,
    )


GEOCODE_CACHE = Path(__file__).resolve().parents[1] / "runtime" / "geocode_cache.local.json"


def _destination(args, geocoder: Geocoder | None) -> tuple[GeoPoint, str, str] | int:
    """(point, precision, provenance) from --address or --dest-lat/--dest-lon."""
    if args.address:
        try:
            result = (geocoder or NominatimGeocoder(cache_path=GEOCODE_CACHE)).geocode(args.address)
        except GeocodingUnavailableError as exc:
            print(f"FAILED: {exc}; distance stays unverified", file=sys.stderr)
            return 1
        print(f"geocoded ({result.precision}, {result.provenance}): {result.display_name}")
        return result.point, result.precision, result.provenance
    if args.dest_lat is None or args.dest_lon is None:
        print("REFUSED: give --address or both --dest-lat and --dest-lon", file=sys.stderr)
        return 2
    try:
        point = GeoPoint(lat=args.dest_lat, lon=args.dest_lon)
    except ValueError:
        print("REFUSED: destination coordinates are out of range", file=sys.stderr)
        return 2
    return point, args.precision, "operator-supplied coordinates"


def _check_commute(
    session: Session, args, provider: CarRouteProvider, geocoder: Geocoder | None = None
) -> int:
    origin = _origin(session)
    if origin is None or origin[1] is None:
        print("REFUSED: no commute origin coordinates configured", file=sys.stderr)
        return 2
    resolved = _destination(args, geocoder)
    if isinstance(resolved, int):
        return resolved
    destination, precision, provenance = resolved
    method = provider.name if precision == "ADDRESS" else f"{provider.name}_APPROX"
    marker = destination_marker(destination.lat, destination.lon)
    cached = find_cached_route(
        session,
        origin_lat=origin[1].lat,
        origin_lon=origin[1].lon,
        dest_lat=destination.lat,
        dest_lon=destination.lon,
    )
    if cached is not None:
        km, minutes = cached.car_route_km, cached.car_route_minutes
        source = f"reused route of commute check {cached.id}"
        print(f"route cache hit: {source} (no network request)")
    else:
        try:
            route = provider.car_route(origin[1], destination)
        except RouteUnavailableError as exc:
            print(f"FAILED: {exc}; distance stays unverified", file=sys.stderr)
            return 1
        km, minutes, source = route.km, route.minutes, "free-flow travel time"
    # Informational only: printed, never stored with the car route.
    print(f"straight line {straight_line_km(origin[1], destination):.1f} km (not a car route)")
    if precision != "ADDRESS":
        print(f"approximate destination ({precision}): stored as {method}, never as verified")
    note = (
        f"{provider.name} car route, {source}; destination {precision} via {provenance}; {marker}"
    )
    if args.address and address_words(args.destination) != address_words(args.address):
        # Codex round 10: the stored destination is the address actually
        # routed to, so a later posting-address check compares the real
        # route; the operator's own label is kept in the note.
        print(f"destination stored as the geocoded address {args.address!r}")
        note = f"{note}; operator label {args.destination!r}"
        args.destination = args.address
    return _store(session, args, km=km, minutes=minutes, method=method, note=note)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage 17 job fit and commute checks")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database")
    target.add_argument("--database-url")
    sub = parser.add_subparsers(dest="command", required=True)
    assess = sub.add_parser("assess")
    assess.add_argument("job_id", type=int)
    assess.add_argument("--json", action="store_true")
    for name in ("record-commute", "check-commute"):
        command = sub.add_parser(name)
        command.add_argument("job_id", type=int)
        command.add_argument("--destination", required=True)
        command.add_argument("--region")
        command.add_argument("--apply", action="store_true")
        if name == "record-commute":
            command.add_argument("--car-km", type=float, required=True)
            command.add_argument("--minutes", type=float)
            command.add_argument("--source-note", required=True)
            command.add_argument(
                "--approximate",
                action="store_true",
                help="measured only to a city centre / postcode / street, not the building",
            )
        else:
            command.add_argument("--dest-lat", type=float)
            command.add_argument("--dest-lon", type=float)
            command.add_argument(
                "--precision",
                choices=("ADDRESS", "STREET", "POSTCODE", "CITY"),
                default="ADDRESS",
                help="what --dest-lat/--dest-lon stand for (default: the building)",
            )
            command.add_argument("--address", help="workplace address to geocode instead")
    return parser


def run(
    argv: list[str] | None = None,
    *,
    route_provider: CarRouteProvider | None = None,
    geocoder: Geocoder | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        engine = resolve_engine(
            args.database, args.database_url, writes=bool(getattr(args, "apply", False))
        )
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        with sessionmaker(bind=engine)() as session:
            if args.command == "assess":
                return _assess(session, args)
            if args.command == "record-commute":
                return _record_commute(session, args)
            return _check_commute(session, args, route_provider or OsrmRouteProvider(), geocoder)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(run())
