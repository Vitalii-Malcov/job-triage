"""Stage 15: one-shot bootstrap of the first real application (ALD pilot).

    python scripts/stage15_import_ald_application.py --database D:/path/app.db
        [--receipt-source imap|stored|none] [--submission-evidence DIR] [--apply]

DRY RUN unless `--apply`. Never runs on startup, never seeds anything else.

What it records (operator-confirmed facts, Stage 14H):

1. The application, created directly at SUBMITTED (event 1, origin
   PORTAL): ALD Vacuum Technologies GmbH, "Backend Developer (m/w/div.)",
   Hanau, BA reference 20385-2347902-S, employer job offer 962, submitted
   once through the ALD career portal at 2026-10-17T14:02:50Z (portal page
   "Ihre Bewerbung ist auf dem Weg zu uns!"). Its correlation key is
   deterministic, so a second run finds the existing application and
   writes nothing.
2. RECEIVED -- only from the REAL receipt e-mail, by running the normal
   Stage 15 response processor (restricted to this one application) over
   the read-only mailbox (`imap`) or the synced `gmail_messages` table
   (`stored`). The receipt event is keyed by that message, so later Gmail
   processing of the same mail is a duplicate. With `none`, the import
   stops at SUBMITTED.

`--submission-evidence DIR` optionally verifies the Stage 14H evidence
directory (SUBMISSION_RECORD.txt must contain the confirmation title and
the single POST) and records the file's SHA-256 in the event metadata --
only the hash, never the temporary path. Nothing is sent anywhere.
"""

import argparse
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.models.application_lifecycle import (
    ApplicationChannel,
    ApplicationEventOrigin,
    ApplicationLifecycleStatus,
)
from app.services.application_tracker import ApplicationTracker

try:
    from scripts.stage15_application_tracker import (
        TargetRefusedError,
        process_gmail,
        resolve_engine,
    )
except ModuleNotFoundError:  # run as `python scripts/...`
    from stage15_application_tracker import TargetRefusedError, process_gmail, resolve_engine

ALD = {
    "company_name": "ALD Vacuum Technologies GmbH",
    "role_title": "Backend Developer (m/w/div.)",
    "location": "Hanau",
    "source": "bundesagentur",
    "source_reference": "20385-2347902-S",
    "external_job_id": "962",
    "application_channel": ApplicationChannel.EMPLOYER_PORTAL,
    "destination_url": "https://karriere.ald-vt.com/Backend-Developer-mwdiv-de-f962.html",
    # karriere@ald-vt.de sent the real receipt; ald-vt.com is the career site.
    "employer_domains": ("ald-vt.de", "ald-vt.com"),
}
SUBMITTED_AT = datetime(2026, 10, 17, 14, 2, 50, tzinfo=UTC)
SUBMISSION_CORRELATION_KEY = "portal-submission:bundesagentur:20385-2347902-S"
SUBMISSION_EVIDENCE = (
    "ALD career portal form f962 (job offer 962) submitted once by the operator via "
    "'Senden'; single form POST answered HTTP 200; confirmation page title "
    "'Ihre Bewerbung ist auf dem Weg zu uns!'; text 'Vielen Dank fuer Ihre Bewerbung bei "
    "ALD Vacuum Technologies GmbH.'; 2026-10-17T14:02:50Z"
)
RECEIPT_SINCE = datetime(2026, 10, 17, tzinfo=UTC)
_EVIDENCE_MARKERS = ("Ihre Bewerbung ist auf dem Weg zu uns!", "Backend-Developer-mwdiv-de-f962")


def verify_submission_evidence(directory: Path) -> str:
    record = directory / "SUBMISSION_RECORD.txt"
    if not record.is_file():
        raise TargetRefusedError("SUBMISSION_RECORD.txt not found in the evidence directory")
    content = record.read_bytes()
    decoded = content.decode("utf-8", errors="replace")
    if not all(marker in decoded for marker in _EVIDENCE_MARKERS):
        raise TargetRefusedError("submission evidence does not describe the ALD submission")
    return hashlib.sha256(content).hexdigest()


def run(argv: list[str] | None = None, *, settings=None) -> int:
    parser = argparse.ArgumentParser(description="Stage 15 ALD application bootstrap")
    parser.add_argument("--database")
    parser.add_argument("--database-url")
    parser.add_argument("--receipt-source", choices=("imap", "stored", "none"), default="imap")
    parser.add_argument("--submission-evidence", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        engine = resolve_engine(
            args.database, args.database_url, writes=bool(getattr(args, "apply", False))
        )
        evidence_file_sha = (
            verify_submission_evidence(args.submission_evidence)
            if args.submission_evidence
            else None
        )
    except TargetRefusedError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    tracker = ApplicationTracker()
    db = sessionmaker(engine)()
    try:
        existing = tracker.find_by_source_reference(db, ALD["source"], ALD["source_reference"])
        print(f"MODE: {'APPLY' if args.apply else 'DRY RUN (nothing written)'}")
        if not args.apply:
            print(
                "application:",
                f"exists id={existing.id} status={existing.current_status.value}"
                if existing
                else "would be created at SUBMITTED (2026-10-17T14:02:50Z)",
            )
            print(f"receipt: would be read from {args.receipt_source} and correlated")
            return 0
        metadata = (
            {"submission_evidence_record_sha256": evidence_file_sha} if evidence_file_sha else {}
        )
        outcome = tracker.create_application(
            db,
            **ALD,
            initial_status=ApplicationLifecycleStatus.SUBMITTED,
            origin=ApplicationEventOrigin.PORTAL,
            actor="stage15_ald_bootstrap",
            occurred_at=SUBMITTED_AT,
            correlation_key=SUBMISSION_CORRELATION_KEY,
            evidence_hash=hashlib.sha256(SUBMISSION_EVIDENCE.encode("utf-8")).hexdigest(),
            evidence_summary=SUBMISSION_EVIDENCE,
            reason="Operator-confirmed single portal submission (Stage 14H)",
            metadata=metadata,
        )
        application = outcome.application
        print(
            f"application id={application.id}: "
            f"{'already present' if outcome.duplicate else 'created'} "
            f"status={application.current_status.value}"
        )
        if args.receipt_source != "none":
            if settings is None:
                from app.core.config import Settings

                settings = Settings()
            results = process_gmail(
                db,
                source=args.receipt_source,
                since=RECEIPT_SINCE,
                apply=True,
                settings=settings,
                only_application_ids=frozenset({application.id}),
            )
            for result in results:
                if result.application_id == application.id:
                    print(
                        f"receipt message {result.message_ref}: {result.classification} "
                        f"{result.previous_status} -> {result.new_status} "
                        f"transition={result.transition_applied} duplicate={result.duplicate} "
                        f"reason={result.reason}"
                    )
        final = tracker.get_application(db, application.id)
        events = tracker.get_application_history(db, application.id)
        print(f"final status: {final.current_status.value}; events: {len(events)}")
        return 0
    finally:
        db.close()
        engine.dispose()


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
