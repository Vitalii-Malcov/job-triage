"""Stage 22 T6: refresh Bundesagentur vacancies from the live search results.

    python scripts/stage22_refresh_ba.py --database DB [--dry-run]
    python scripts/stage22_refresh_ba.py --database DB --apply --snapshot DIR
        [--limit N] [--create-new]

Default is a DRY RUN: the configured BA searches run (search pages only, GET to
rest.arbeitsagentur.de) and are compared with the stored vacancies; the
database is opened read-only. The report shows how many postings were found,
exist, are new or would be updated, which structured fields would be added,
BA-reference conflicts (see below), results cut off by pagination and stored
vacancies that were not returned.

`--apply` (explicit, needs `--snapshot DIR` for every database) writes:
  * existing vacancies: only the BA structured facts (coordinates, work place,
    home office, Quereinstieg) and a still empty `source_reference`. No score,
    recommendation, status, description or `last_seen_at`.
  * `--create-new` additionally stores new vacancies through the collector run's
    own per-job path (detail description, skills, scoring). Without it new
    postings are only counted.
A posting whose BA reference already belongs to a stored row with another
fingerprint is never created or merged: it is listed as a conflict for manual
review. A vacancy missing from today's results is only counted -- a search
result is a window, not proof that a posting is closed.

Safety, independent of `.env`: every outbound feature flag and the Telegram /
mailbox credentials are forced off for this process, SMTP/IMAP are blocked and
HTTP is limited to the BA host. Before the first write: Alembic head,
integrity_check and foreign_key_check, no competing writer, one refresh at a
time (lock file) and a verified backup-API snapshot. After the write: the
integrity check again and a content digest of every table except `jobs` and
`job_reference_tokens` (so `applications`, `application_events`, Gmail and
Telegram tables are proven unchanged). Re-running is idempotent.

Exit codes: 0 done, 1 incomplete (search failed / row failed), 2 refused,
3 protected tables changed (restore the snapshot).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import imaplib
import json
import os
import smtplib
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

try:
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine
except ModuleNotFoundError:  # run as `python scripts/...`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.stage15_application_tracker import TargetRefusedError, resolve_engine

from sqlalchemy.exc import OperationalError

from app.core.database_target import DatabaseKind, classify_path
from scripts.stage21_assess_vacancies import (
    ATTEMPTS,
    BATCH_SIZE,
    SnapshotError,
    _backup,
    _connect_ro,
    _readonly_engine,
    _retryable,
    _verify_snapshot,
    _writer_engine,
)

BA_HOST = "rest.arbeitsagentur.de"
# Tables this tool may write; every other table is digest-checked.
WRITTEN_TABLES = frozenset({"jobs", "job_reference_tokens"})

# Forced for this process whatever `.env` or the shell says.
FORCED_OFF_FLAGS = (
    "OUTBOUND_SENDING_ENABLED",
    "AUTOMATION_SCHEDULER_ENABLED",
    "AUTOMATION_AUTO_PREPARE_ENABLED",
    "AUTOMATION_GMAIL_CYCLE_ENABLED",
    "AUTOMATION_FOLLOW_UP_CYCLE_ENABLED",
    "TELEGRAM_DAILY_DIGEST_ENABLED",
    "TELEGRAM_VACANCY_FEED_ENABLED",
    "TELEGRAM_BEWERBUNG_DRAFT_ENABLED",
    "TELEGRAM_BEWERBUNG_APPROVAL_ENABLED",
    "TELEGRAM_GMAIL_DRAFT_ENABLED",
    "TELEGRAM_OPERATOR_CONTROL_ENABLED",
    "COMPANY_RESEARCH_AUTO_ENABLED",
)
FORCED_EMPTY_SECRETS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GMAIL_APP_PASSWORD",
    "XING_MAILBOX_APP_PASSWORD",
)


class OutboundBlockedError(RuntimeError):
    """An outbound call outside the BA search/detail API was attempted."""


class RefusedError(Exception):
    """The run is refused before anything is written (exit 2)."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh Bundesagentur vacancies (Stage 22 T6)")
    parser.add_argument("--database", required=True, help="SQLite file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--apply", action="store_true", help="write (needs --snapshot)")
    parser.add_argument("--snapshot", default=None, help="directory for the verified backup")
    parser.add_argument("--limit", type=int, default=None, help="process at most N vacancies")
    parser.add_argument("--create-new", action="store_true", help="also store new vacancies")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument(
        "--discovery",
        action="store_true",
        help="Stage 24: run the opt-in junior discovery searches instead of the configured ones",
    )
    return parser


# --- outbound lock-down ---------------------------------------------------------


@contextmanager
def hardened_environment() -> Iterator[None]:
    """Force every outbound flag/credential off in the environment, which
    outranks `.env` for the settings loaded inside; restored afterwards."""
    from app.core.config import get_settings

    saved = {name: os.environ.get(name) for name in (*FORCED_OFF_FLAGS, *FORCED_EMPTY_SECRETS)}
    for name in FORCED_OFF_FLAGS:
        os.environ[name] = "false"
    for name in FORCED_EMPTY_SECRETS:
        os.environ[name] = ""
    get_settings.cache_clear()
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()


def assert_outbound_disabled(settings) -> None:
    flags = {name: getattr(settings, name.lower()) for name in FORCED_OFF_FLAGS}
    secrets = {name: getattr(settings, name.lower()) for name in FORCED_EMPTY_SECRETS}
    if any(flags.values()) or any(secrets.values()):
        raise RefusedError("an outbound flag or credential is still set after lock-down")


def _blocked_class(original: type) -> type:
    """A subclass that cannot connect (still a class, so code that imports or
    subclasses it later keeps working)."""

    class Blocked(original):  # type: ignore[misc, valid-type]
        def __init__(self, *_args, **_kwargs) -> None:
            raise OutboundBlockedError("outbound channel blocked by the BA refresh tool")

    Blocked.__name__ = original.__name__
    return Blocked


def _checked(send: Callable) -> Callable:
    def guarded(self, request, *args, **kwargs):
        if request.url.host != BA_HOST:
            raise OutboundBlockedError(f"HTTP to {request.url.host} blocked by the BA refresh tool")
        return send(self, request, *args, **kwargs)

    return guarded


@contextmanager
def outbound_guard() -> Iterator[None]:
    """HTTP only to the BA host; SMTP and IMAP unavailable. Process-wide."""
    saved = {
        (httpx.AsyncClient, "send"): httpx.AsyncClient.send,
        (httpx.Client, "send"): httpx.Client.send,
        (smtplib, "SMTP"): smtplib.SMTP,
        (smtplib, "SMTP_SSL"): smtplib.SMTP_SSL,
        (imaplib, "IMAP4"): imaplib.IMAP4,
        (imaplib, "IMAP4_SSL"): imaplib.IMAP4_SSL,
    }
    httpx.AsyncClient.send = _checked(saved[(httpx.AsyncClient, "send")])
    httpx.Client.send = _checked(saved[(httpx.Client, "send")])
    for owner, name in (
        (smtplib, "SMTP"),
        (smtplib, "SMTP_SSL"),
        (imaplib, "IMAP4"),
        (imaplib, "IMAP4_SSL"),
    ):
        setattr(owner, name, _blocked_class(saved[(owner, name)]))
    try:
        yield
    finally:
        for (owner, name), original in saved.items():
            setattr(owner, name, original)


# --- database protection ----------------------------------------------------------


def check_integrity(path: Path) -> None:
    connection = _connect_ro(path)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if integrity != [("ok",)]:
        raise RefusedError("integrity_check failed")
    if foreign:
        raise RefusedError("foreign_key_check reports violations")


def protected_digest(path: Path) -> dict[str, str]:
    """SHA-256 over the content of every table this tool must not write."""
    connection = _connect_ro(path)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if row[0] not in WRITTEN_TABLES
        ]
        digests = {}
        for name in names:
            digest = hashlib.sha256()
            quoted = '"' + name.replace('"', '""') + '"'
            for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY rowid"):  # noqa: S608
                digest.update(repr(row).encode("utf-8"))
            digests[name] = digest.hexdigest()
        return digests
    finally:
        connection.close()


def require_no_competing_writer(path: Path) -> None:
    connection = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError:
        raise RefusedError("another process holds the database write lock") from None
    finally:
        connection.close()


@contextmanager
def exclusive_run_lock(database: Path) -> Iterator[None]:
    """One refresh at a time (non-blocking OS file lock next to the database)."""
    lock_path = database.with_name(database.name + ".refresh-ba.lock")
    handle = lock_path.open("a+b")
    try:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RefusedError("another refresh is already running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        # The file stays: removing it would let a later run lock a new file
        # while this one still holds the old one. Closing releases the lock.
        handle.close()


def _stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def take_snapshot(database: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    target = directory / f"{database.stem}.pre-stage22-refresh-ba-{stamp}{database.suffix}"
    if target.exists():
        raise SnapshotError(f"snapshot {target.name} already exists; never overwritten")
    _backup(database, target)
    _verify_snapshot(database, target)
    return target


# --- report -------------------------------------------------------------------------


def summarize(plan, fetched) -> dict:
    from app.services.ba_refresh import MAX_REPORTED

    return {
        "found": plan.fetched,
        "existing": plan.existing,
        "unchanged": plan.unchanged,
        "new": len(plan.creates) + plan.deferred_creates,
        "to_update": len(plan.updates) + plan.deferred_updates,
        "fields_added": dict(sorted(plan.facts_added.items())),
        "fields_changed": dict(sorted(plan.facts_changed.items())),
        "reference_conflicts": len(plan.conflicts),
        "conflicts": [
            {
                "kind": c.kind,
                "reference": c.reference,
                "title": c.title,
                "company": c.company,
                "existing_job_ids": c.existing_job_ids,
            }
            for c in plan.conflicts[:MAX_REPORTED]
        ],
        "queries": [
            {
                "label": q.label,
                "keywords": q.keywords,
                "fetched": q.fetched,
                "skipped_invalid": q.skipped_invalid,
                "skipped_not_home_office": q.skipped_not_home_office,
                "truncated": q.truncated,
                "total_results": q.total_results,
                "left_out_by_pagination": q.left_out,
                "error": q.error,
            }
            for q in fetched.queries
        ],
        "truncated_queries": sum(1 for q in fetched.queries if q.truncated),
        "left_out_by_pagination": sum(q.left_out or 0 for q in fetched.queries),
        "failed_queries": fetched.failed_queries,
        "stored_ba_rows": plan.stored_ba_rows,
        "not_returned": plan.not_returned,
        "not_returned_with_application": plan.not_returned_with_application,
        "limit": plan.limited_to,
        "deferred_updates": plan.deferred_updates,
        "deferred_creates": plan.deferred_creates,
    }


def print_report(summary: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
        return
    s = summary
    print(
        f"found={s['found']} existing={s['existing']} unchanged={s['unchanged']} "
        f"new={s['new']} to_update={s['to_update']}"
    )
    print(f"fields to add (empty today): {s['fields_added'] or '-'}")
    if s["fields_changed"]:
        print(f"fields with a changed value: {s['fields_changed']}")
    print(f"BA-reference conflicts (not created, for manual review): {s['reference_conflicts']}")
    for c in s["conflicts"]:
        print(
            f"  {c['kind']} ref={c['reference']} existing_job_ids={c['existing_job_ids']} "
            f"| {c['company']} | {c['title']}"
        )
    for q in s["queries"]:
        extra = f" ERROR={q['error']}" if q["error"] else ""
        cut = f" left_out={q['left_out_by_pagination']}" if q["truncated"] else ""
        print(
            f"  query {q['label']}/{q['keywords']}: fetched={q['fetched']} "
            f"invalid={q['skipped_invalid']} not_home_office={q['skipped_not_home_office']} "
            f"truncated={q['truncated']}{cut}{extra}"
        )
    print(
        f"pagination: {s['truncated_queries']} search(es) truncated, "
        f"{s['left_out_by_pagination']} results not fetched; failed searches: {s['failed_queries']}"
    )
    print(
        f"not returned today: {s['not_returned']} of {s['stored_ba_rows']} stored BA vacancies "
        f"({s['not_returned_with_application']} with an application). This is NOT evidence "
        "that they are closed; nothing is changed because of it."
    )
    if s["limit"] is not None:
        print(
            f"limit={s['limit']}: deferred updates={s['deferred_updates']} "
            f"creates={s['deferred_creates']}"
        )


# --- apply ------------------------------------------------------------------------------


def write_updates(engine, updates) -> tuple[int, int]:
    """Batches of BATCH_SIZE, one BEGIN IMMEDIATE transaction each. Returns
    (rows changed, rows left by conflicts)."""
    from app.services.ba_refresh import apply_updates

    writer = _writer_engine(engine)
    factory = sessionmaker(bind=writer)
    written = conflicted = 0
    try:
        for start in range(0, len(updates), BATCH_SIZE):
            batch = updates[start : start + BATCH_SIZE]
            for _attempt in range(ATTEMPTS):
                try:
                    with factory() as session, session.begin():
                        written += apply_updates(session, batch)
                    break
                except OperationalError as exc:
                    if not _retryable(exc):
                        raise
            else:
                conflicted += len(batch)
    finally:
        writer.dispose()
    return written, conflicted


async def create_jobs(engine, creates, detail_collector) -> dict[str, int]:
    """Store new vacancies. Detail descriptions are fetched first, outside any
    database lock; each vacancy is then re-checked and persisted in ONE
    `BEGIN IMMEDIATE` transaction, so a writer that appears meanwhile can
    neither be overwritten nor duplicated."""
    from app.db.candidate_profile_repository import get_candidate_skills_for_scoring
    from app.services.ba_refresh import reference_taken
    from app.services.collector_runner import enrich_and_persist_ba_job

    counts = {"created": 0, "skipped_conflict": 0, "failed": 0}
    described: list = []
    for job in creates:
        try:
            description = await detail_collector.fetch_detail(job.source_reference)
        except Exception:  # noqa: BLE001 -- one failed detail must not stop the others
            counts["failed"] += 1
            continue
        if description is not None:
            job = job.model_copy(update={"description": description})
        described.append(job)

    writer = _writer_engine(engine)
    factory = sessionmaker(bind=writer)
    try:
        with factory() as session, session.begin():
            skills = get_candidate_skills_for_scoring(session)
        for job in described:
            with factory() as session:
                try:
                    # the first statement takes the write reservation; the check
                    # and the insert (which commits) run inside it
                    if reference_taken(session, job):
                        counts["skipped_conflict"] += 1
                        session.rollback()
                        continue
                    if not session.execute(text("SELECT 1 FROM candidate_profiles")).first():
                        # deleted since the preflight: scoring would re-create it
                        counts["failed"] += 1
                        session.rollback()
                        continue
                    _record, _score, created = await enrich_and_persist_ba_job(
                        session, detail_collector, skills, job, fetch_detail=False
                    )
                    counts["created" if created else "skipped_conflict"] += 1
                except Exception:  # noqa: BLE001 -- one vacancy must not stop the others
                    session.rollback()
                    counts["failed"] += 1
    finally:
        writer.dispose()
    return counts


def require_candidate_profile(path: Path) -> None:
    """Scoring a new apprenticeship/internship posting would create a missing
    candidate profile; this tool never writes that table."""
    connection = _connect_ro(path)
    try:
        present = connection.execute("SELECT count(*) FROM candidate_profiles").fetchone()[0]
    finally:
        connection.close()
    if not present:
        raise RefusedError("--create-new needs an existing candidate profile")


def run(
    argv: list[str] | None = None,
    *,
    collector_factory: Callable | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        print("REFUSED: --limit must be at least 1", file=sys.stderr)
        return 2
    if args.apply and not args.snapshot:
        print("REFUSED: --apply needs --snapshot DIR (verified backup)", file=sys.stderr)
        return 2
    if args.create_new and not args.apply:
        print("REFUSED: --create-new only makes sense with --apply", file=sys.stderr)
        return 2
    database = Path(os.path.abspath(args.database))
    try:
        with hardened_environment(), outbound_guard():
            return _run(args, database, collector_factory)
    except (RefusedError, TargetRefusedError, SnapshotError) as exc:
        print(f"REFUSED: {exc}; nothing written", file=sys.stderr)
        return 2


def _run(args, database: Path, collector_factory: Callable | None) -> int:
    from app.core.config import get_settings
    from app.services.ba_refresh import fetch_postings, plan_refresh
    from app.services.collector_runner import _candidate_search_settings, ba_query_collector
    from app.services.search_strategy import bundesagentur_queries, discovery_queries
    from app.utils.config_flags import is_configured

    get_settings.cache_clear()
    settings = get_settings()
    assert_outbound_disabled(settings)
    if not is_configured(settings.bundesagentur_api_key):
        raise RefusedError("BUNDESAGENTUR_API_KEY is not configured")
    if args.apply and classify_path(database) is DatabaseKind.BACKUP:
        raise RefusedError("refusing to write into a backup database")

    engine = resolve_engine(str(database), None, writes=args.apply)
    try:
        check_integrity(database)
        reader = _readonly_engine(engine)
        try:
            with Session(reader) as session:
                search = _candidate_search_settings(session)
                queries = (
                    discovery_queries(settings, search)
                    if args.discovery
                    else bundesagentur_queries(settings, search)
                )
                if args.discovery and not queries:
                    raise RefusedError("discovery needs a configured commute search")
        finally:
            reader.dispose()
        factory = collector_factory or (lambda query: ba_query_collector(settings, query))
        fetched = asyncio.run(fetch_postings(queries, factory))

        reader = _readonly_engine(engine)
        try:
            with Session(reader) as session:
                plan = plan_refresh(session, fetched, limit=args.limit)
        finally:
            reader.dispose()
        print_report(summarize(plan, fetched), args.json)
        if not args.apply:
            print("DRY RUN: nothing written")
            return 0
        if fetched.failed_queries:
            print("INCOMPLETE: a search failed; nothing written", file=sys.stderr)
            return 1
        with exclusive_run_lock(database):
            require_no_competing_writer(database)
            if args.create_new:
                require_candidate_profile(database)
            before = protected_digest(database)
            copy = take_snapshot(database, Path(args.snapshot))
            print(f"snapshot: {copy} (verified)")
            written, conflicted = write_updates(engine, plan.updates)
            print(f"updated: {written} vacancies ({conflicted} left by write conflicts)")
            failed = conflicted
            if args.create_new:
                detail = factory(queries[0])
                counts = asyncio.run(create_jobs(engine, plan.creates, detail))
                print(
                    f"created: {counts['created']} (skipped_conflict={counts['skipped_conflict']}"
                    f", failed={counts['failed']})"
                )
                failed += counts["failed"]
            else:
                print(
                    "new vacancies not stored (no --create-new): "
                    f"{len(plan.creates) + plan.deferred_creates}"
                )
            check_integrity(database)
            after = protected_digest(database)
            if after != before:
                changed = sorted(name for name in before if before.get(name) != after.get(name))
                print(
                    f"ALERT: protected tables changed: {changed}; restore {copy}",
                    file=sys.stderr,
                )
                return 3
            print(f"protected tables unchanged ({len(before)} tables digest-checked)")
        if failed:
            print(f"INCOMPLETE: {failed} vacancies not written; re-run to continue")
            return 1
        print("APPLIED")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(run())
