"""Stage 9D: hand the EXACT Stage 9C-approved Bewerbung package to Gmail as
ONE draft -- on an explicit operator press, never automatically.

**GMAIL DRAFT CREATED != APPLICATION SENT.** The only external mutation is
one IMAP APPEND of a recipient-less, attachment-less plain-text draft into
the verified Drafts mailbox. Nothing is ever sent; there is no SMTP, Gmail
API, OAuth, HTTP, employer contact or research. The only business table
written is `gmail_application_drafts`; `JobRecord.status` and every Stage
9A/9B/9C/6E row stay untouched.

Flow of one "📧 Gmail-Entwurf erstellen" press:

1a. claim -- fresh Stage 9C revalidation in lock order (profile -> job ->
    preparation -> review -> exact approved revision), deterministic
    render, ledger row LAST: INSERT / pre-fence takeover / FAILED retry;
    COMMIT (acknowledged, or no authority at all).
1b. arm -- full revalidation again, ledger LAST, `begin_append` freezes the
    attempt bundle (marker, hash, renderer, account, mailbox + wire,
    budget, fence). The APPEND is permitted ONLY if this `commit()`
    RETURNS; `rowcount == 1` alone is never permission.
2.  one provider call (no DB transaction open): LIST-verify the frozen
    target, ONE APPEND within the frozen absolute deadline.
3.  ledger-only CAS: CREATED | FAILED | UNCERTAIN, with DB-only recovery
    when the commit acknowledgment is lost -- never another APPEND.

UNCERTAIN is sticky: only positive evidence for the exact attempt (one
read-only Message-ID match, or THIS live operation's retained tagged OK)
turns it CREATED. Zero/many matches, errors and elapsed time never make it
FAILED or retryable.

CREATED is historical: "Stage 9D created this draft at least once" -- not
that it still exists, is unsent, unchanged or still recipient-less.

Logs carry only ledger/link ids, attempt counts, states, fixed codes,
exception types and durations.
"""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db import gmail_application_draft_repository as repo
from app.db.datetime_utils import ensure_utc
from app.db.telegram_bewerbung_approval_repository import (
    APPROVAL_CAPABILITY_PATTERN,
    get_link_by_capability,
)
from app.models.review_package import ReviewedBewerbungContent
from app.providers.email.base import MAX_ADDRESS_LENGTH, normalize_account_key
from app.providers.email.draft_base import (
    MESSAGE_ID_PATTERN,
    DraftCreateDefiniteError,
    DraftMessage,
    DraftMessageInvalidError,
    DraftProvider,
    DraftProviderError,
    DraftTarget,
    ReconcileTarget,
    build_draft_mime,
)
from app.providers.email.imap_draft import (
    GmailImapDraftProvider,
    MailboxNameError,
    encode_mailbox_wire,
)
from app.services.telegram_bewerbung import approval_flags_enabled
from app.services.telegram_bewerbung_approval import (
    ApprovedLinkSnapshot,
    revalidate_approved_link_locked,
)
from app.services.telegram_bewerbung_preview import normalize_scalar
from app.utils.config_flags import is_configured

logger = logging.getLogger(__name__)

RENDERER_VERSION = "9d-v1"
# An UNARMED claim older than this is abandoned (crash between claim and
# arm) and may be taken over by a later explicit, fully authorized press.
CLAIM_LEASE_SECONDS = 120
# An ARMED attempt older than its frozen deadline + this margin becomes
# UNCERTAIN (ledger-only); it is never transferred to another APPEND worker.
STALE_MARGIN_SECONDS = 30
_MAX_RECOVERY_READS = 3
_MAX_MARKER_TRIES = 3

PREFIX = "bm"
CREATE_ACTION = "c"
RECHECK_ACTION = "r"
_ACTIONS = frozenset({CREATE_ACTION, RECHECK_ACTION})

CREATE_BUTTON = "📧 Gmail-Entwurf erstellen"
RETRY_BUTTON = "📧 Erneut versuchen"
RECHECK_BUTTON = "🔄 Gmail-Status prüfen"

# Same shape rule as app.services.follow_up_recipient (not imported: Stage
# 9D must not depend on any send-path module).
_EMAIL_SHAPE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_HEADER_UNSAFE = ("\r", "\n", "\x00")

_NOTHING_SENT = "Es wurde nichts gesendet."
_NOTHING_DONE = "Es wurde nichts erstellt oder gesendet."
_CONFIG_NOTICE = "Die Gmail-Konfiguration ist unvollständig oder ungültig. " + _NOTHING_DONE

NOTICES: dict[str, str] = {
    "CREATED": (
        "📧 Gmail-Entwurf erstellt. Empfänger noch nicht eingetragen. "
        "Lebenslauf nicht angehängt. NOCH NICHT GESENDET."
    ),
    "ALREADY_CREATED": (
        "Für diese Freigabe wurde bereits ein Gmail-Entwurf erstellt. Bei der Erstellung "
        "waren kein Empfänger und kein Lebenslauf enthalten. Dieser Schritt hat nichts gesendet."
    ),
    "IN_PROGRESS": "Gmail-Entwurf wird gerade erstellt … Es wird nichts gesendet.",
    "UNCERTAIN": (
        "⚠️ Gmail-Status unklar — ein Entwurf wurde möglicherweise erstellt. Er wird NICHT "
        "erneut automatisch erstellt. Bitte „Gmail-Status prüfen“ verwenden oder in Gmail "
        "nachsehen. " + _NOTHING_SENT
    ),
    "TOO_EARLY": "Bitte später erneut prüfen. " + _NOTHING_SENT,
    "STILL_UNCERTAIN": (
        "Gmail-Status weiterhin unklar. Es wird kein neuer Entwurf erstellt; bitte in Gmail "
        "nachsehen. " + _NOTHING_SENT
    ),
    "RECONCILED": (
        "Gmail-Entwurf wurde bei der Erstellung bestätigt. Dieser Schritt hat nichts gesendet."
    ),
    "FAILED": "❌ Gmail-Entwurf konnte nicht erstellt werden. " + _NOTHING_SENT,
    "PERSISTENCE_UNCERTAIN": (
        "Gmail-Entwurf wurde wahrscheinlich erstellt; der Status konnte nicht gespeichert "
        "werden. Nichts wurde gesendet."
    ),
    "STATUS_UNSAVED": (
        "Der Gmail-Status konnte nicht gespeichert werden. In diesem Vorgang wurde nichts an "
        "Gmail übertragen. " + _NOTHING_SENT
    ),
    "FINALIZE_UNSAVED": "Der Gmail-Status konnte nicht gespeichert werden. " + _NOTHING_SENT,
    "STATUS_UNAVAILABLE": (
        "Der Gmail-Status konnte gerade nicht gelesen werden. Es wurde nichts an Gmail "
        "übertragen. " + _NOTHING_SENT
    ),
    "ACCOUNT_CHANGED": (
        "Das konfigurierte Gmail-Konto hat sich geändert; für diese Freigabe ist keine Aktion "
        "möglich. " + _NOTHING_SENT
    ),
    "NO_DRAFT_STATUS": "Für diese Freigabe gibt es keinen offenen Gmail-Status. " + _NOTHING_SENT,
    "CONTENT_INCOMPLETE": (
        "Das freigegebene Anschreiben ist für einen Gmail-Entwurf unvollständig (Betreff oder "
        "Text fehlt). " + _NOTHING_DONE
    ),
    "CONTENT_INVALID": (
        "Das freigegebene Anschreiben enthält Zeichen, die in einem Gmail-Entwurf nicht "
        "zulässig sind. " + _NOTHING_DONE
    ),
    "CONFIG_INVALID": _CONFIG_NOTICE,
    "FROM_INVALID": _CONFIG_NOTICE,
    "DRAFTS_MAILBOX_INVALID": _CONFIG_NOTICE,
    "DRAFTS_MAILBOX_UNENCODABLE": _CONFIG_NOTICE,
    "GMAIL_DRAFT_DISABLED": "Gmail-Entwürfe sind deaktiviert. " + _NOTHING_DONE,
    "UNKNOWN_CAPABILITY": "Unbekannte oder abgelaufene Freigabe. " + _NOTHING_DONE,
    "NOT_APPROVED": "Diese Prüfung ist nicht freigegeben. " + _NOTHING_DONE,
    "ALREADY_REJECTED": "Diese Prüfung wurde abgelehnt. " + _NOTHING_DONE,
    "APPROVED_ELSEWHERE": (
        "Freigegeben wurde eine andere Revision als die in Telegram gezeigte; dafür wird kein "
        "Gmail-Entwurf erstellt. " + _NOTHING_DONE
    ),
    "PACKAGE_REPLACED": (
        "Das Paket wurde inzwischen ersetzt; für diese Freigabe wird kein Gmail-Entwurf "
        "erstellt. " + _NOTHING_DONE
    ),
    "PACKAGE_STALE": (
        "Profil oder Stelle haben sich geändert; für diese Freigabe wird kein Gmail-Entwurf "
        "erstellt. " + _NOTHING_DONE
    ),
    "PACKAGE_UNAVAILABLE": "Das Paket ist nicht mehr vollständig verfügbar. " + _NOTHING_DONE,
    "JOB_NOT_ELIGIBLE": (
        "Für diese Stelle ist kein Gmail-Entwurf möglich (Status nicht NEW/SAVED). " + _NOTHING_DONE
    ),
    "NO_PROFILE": "Kein Kandidatenprofil vorhanden. " + _NOTHING_DONE,
}

# Which follow-up button an outcome offers. NEVER a create/retry button for
# an UNCERTAIN family outcome, and never a "Senden" button anywhere.
_ACTION_FOR: dict[str, str] = {
    "FAILED": CREATE_ACTION,
    "UNCERTAIN": RECHECK_ACTION,
    "TOO_EARLY": RECHECK_ACTION,
    "STILL_UNCERTAIN": RECHECK_ACTION,
}


@dataclass(frozen=True)
class GmailDraftOutcome:
    """What happened, a short notice (no candidate data) and, optionally,
    the single follow-up action button it offers."""

    code: str
    notice: str | None = None
    action: str | None = None


def _outcome(code: str) -> GmailDraftOutcome:
    return GmailDraftOutcome(code, NOTICES.get(code), _ACTION_FOR.get(code))


# --- callback data ----------------------------------------------------------------


def build_callback_data(capability: str, action: str) -> str:
    return f"{PREFIX}:{capability}:{action}"


def parse_callback_data(data: str | None) -> tuple[str, str] | None:
    """Strictly parse `bm:<capability>:c|r`; anything else is None."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != PREFIX:
        return None
    if not APPROVAL_CAPABILITY_PATTERN.fullmatch(parts[1]) or parts[2] not in _ACTIONS:
        return None
    return parts[1], parts[2]


_BUTTON_TEXT = {CREATE_ACTION: CREATE_BUTTON, RECHECK_ACTION: RECHECK_BUTTON}


def action_keyboard(capability: str, action: str, *, retry: bool = False) -> dict:
    text = RETRY_BUTTON if retry else _BUTTON_TEXT[action]
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": build_callback_data(capability, action)}]
        ]
    }


def outcome_keyboard(outcome: GmailDraftOutcome, capability: str) -> dict | None:
    if outcome.action is None:
        return None
    return action_keyboard(capability, outcome.action, retry=outcome.code == "FAILED")


def gmail_draft_flags_enabled(settings) -> bool:
    """Stage 9D needs the 9B, 9C AND 9D flags. Never consults
    `outbound_sending_enabled`."""
    return approval_flags_enabled(settings) and bool(
        getattr(settings, "telegram_gmail_draft_enabled", False)
    )


# --- pure: From, content, hash, marker ------------------------------------------------


def validate_from_address(raw: str | None) -> str | None:
    """The exact configured address as ONE bare mailbox, or None. Validated
    independently of `normalize_account_key` (identity, not validation)."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_ADDRESS_LENGTH:
        return None
    if any(char in raw for char in _HEADER_UNSAFE) or raw != raw.strip() or not raw.isascii():
        return None
    pairs = getaddresses([raw])
    if len(pairs) != 1 or pairs[0][0] or pairs[0][1] != raw:
        return None
    return raw if _EMAIL_SHAPE.fullmatch(raw) else None


@dataclass(frozen=True, repr=False)
class RenderedDraft:
    subject: str
    body_lf: str

    def __repr__(self) -> str:
        return "RenderedDraft(<redacted>)"


class _ContentError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _encodable(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _ContentError("CONTENT_INVALID") from exc
    return text


def _component(value: str | None) -> str | None:
    """None/empty/whitespace-only -> absent. CRLF and lone CR -> LF; outer
    whitespace stripped; inner text, whitespace and Unicode form kept."""
    if value is None:
        return None
    if "\x00" in value:
        raise _ContentError("CONTENT_INVALID")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return _encodable(text) if text else None


def render_draft_content(reviewed: ReviewedBewerbungContent) -> RenderedDraft | str:
    """Deterministic Stage 9D rendering of the approved revision's
    reviewed letter ONLY. No AI, no provider, no fallback, no placeholder,
    no truncation. Returns the rendering or CONTENT_INVALID /
    CONTENT_INCOMPLETE (a Stage 9D eligibility rule; Stage 6E approval is
    not affected)."""
    try:
        raw_subject = reviewed.subject.value
        if raw_subject is not None and any(char in raw_subject for char in _HEADER_UNSAFE):
            return "CONTENT_INVALID"  # checked on the RAW value, before normalizing
        salutation = _component(reviewed.salutation.value)
        opening = _component(reviewed.opening.value)
        paragraphs = [
            text
            for text in (_component(paragraph.text) for paragraph in reviewed.body_paragraphs)
            if text
        ]
        closing = _component(reviewed.closing.value)
        signature = _component(reviewed.signature_name.value)
        subject = _encodable(normalize_scalar(raw_subject))
    except _ContentError as exc:
        return exc.code
    if not subject:
        return "CONTENT_INCOMPLETE"
    if not (opening or paragraphs or closing):
        return "CONTENT_INCOMPLETE"  # salutation/signature alone is not a letter
    blocks = [block for block in (salutation, opening) if block] + paragraphs
    closing_block = "\n".join(part for part in (closing, signature) if part)
    if closing_block:
        blocks.append(closing_block)
    return RenderedDraft(subject, "\n\n".join(blocks) + "\n")


def content_hash(renderer_version: str, from_address: str, subject: str, body_lf: str) -> str:
    """Logical content hash: renderer, From, To/Cc/Bcc (always null),
    subject, LF body. Date, Message-ID, UID/UIDVALIDITY and mailbox are
    attempt/transport metadata and excluded."""
    payload = json.dumps(
        [renderer_version, from_address, None, None, None, subject, body_lf],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_marker() -> str:
    marker = f"<s9d.{secrets.token_hex(16)}@ai-job-search.invalid>"
    if not MESSAGE_ID_PATTERN.fullmatch(marker):  # pragma: no cover - defensive
        raise RuntimeError("marker generation produced an invalid value")
    return marker


def _now() -> datetime:
    return datetime.now(UTC)


# --- static configuration ----------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class _Config:
    from_address: str
    account_key: str
    mailbox: str
    mailbox_wire: str
    budget_seconds: int


def _static_config(settings) -> "_Config | GmailDraftOutcome":
    """Everything checkable without the DB or network -- refused BEFORE
    any ledger claim."""
    from_address = validate_from_address(getattr(settings, "gmail_username", None))
    if from_address is None:
        return _outcome("FROM_INVALID")
    if not is_configured(getattr(settings, "gmail_app_password", "")):
        return _outcome("CONFIG_INVALID")
    try:
        wire = encode_mailbox_wire(settings.gmail_drafts_mailbox)
    except MailboxNameError as exc:
        return _outcome(exc.code)
    return _Config(
        from_address=from_address,
        account_key=normalize_account_key(from_address),
        mailbox=settings.gmail_drafts_mailbox,
        mailbox_wire=wire,
        budget_seconds=int(settings.gmail_draft_attempt_budget_seconds),
    )


def build_provider(settings) -> GmailImapDraftProvider:
    return GmailImapDraftProvider(
        settings.gmail_imap_host,
        settings.gmail_imap_port,
        settings.gmail_username,
        settings.gmail_app_password,
        drafts_enabled=gmail_draft_flags_enabled(settings),
    )


# --- shared DB helpers ----------------------------------------------------------------------


def _handoff(snapshot: ApprovedLinkSnapshot) -> repo.FrozenHandoff:
    return repo.FrozenHandoff(
        review_id=snapshot.review_id,
        approved_revision_id=snapshot.approved_revision_id,
        preparation_id=snapshot.preparation_id,
        generation=snapshot.generation,
        match_id=snapshot.match_id,
        cv_draft_id=snapshot.cv_draft_id,
        bewerbung_draft_id=snapshot.bewerbung_draft_id,
        package_token=snapshot.package_token,
        input_identity=snapshot.input_identity,
    )


def _render(snapshot: ApprovedLinkSnapshot, config: _Config) -> "RenderedDraft | str":
    try:
        reviewed = snapshot.reviewed_bewerbung()
    except ValueError:
        return "PACKAGE_UNAVAILABLE"
    rendered = render_draft_content(reviewed)
    if isinstance(rendered, str):
        return rendered
    try:  # dry run of the exact MIME contract before any claim
        build_draft_mime(
            DraftMessage(config.from_address, rendered.subject, rendered.body_lf, new_marker()),
            date=_now(),
        )
    except DraftMessageInvalidError:
        return "CONTENT_INVALID"
    return rendered


def _report(snapshot: repo.LedgerSnapshot | None) -> GmailDraftOutcome:
    """Authoritative report of a ledger state -- never authority."""
    if snapshot is None:
        return _outcome("NO_DRAFT_STATUS")
    return _outcome(
        {
            repo.CREATED: "ALREADY_CREATED",
            repo.UNCERTAIN: "UNCERTAIN",
            repo.FAILED: "FAILED",
        }.get(snapshot.state, "IN_PROGRESS")
    )


def _lease_expired(snapshot: repo.LedgerSnapshot, now: datetime) -> bool:
    started = snapshot.claim_started_at
    lease = timedelta(seconds=CLAIM_LEASE_SECONDS)
    return started is not None and ensure_utc(started) + lease < now


def _is_stale_armed(snapshot: repo.LedgerSnapshot, now: datetime) -> bool:
    deadline = snapshot.attempt_deadline_at
    return (
        snapshot.state == repo.CREATING
        and deadline is not None
        and now > deadline + timedelta(seconds=STALE_MARGIN_SECONDS)
    )


def _classify_stale(db: Session, snapshot: repo.LedgerSnapshot) -> repo.LedgerSnapshot | None:
    """Ledger-only `armed CREATING -> UNCERTAIN` for an aged attempt.
    Grants nothing. Returns the fresh row (or None if the DB is
    unavailable)."""
    try:
        if repo.classify_stale_armed(
            db, snapshot, stale_margin_seconds=STALE_MARGIN_SECONDS, now=_now()
        ):
            db.commit()
            logger.info(
                "gmail_draft_stale_armed ledger_id=%s attempt=%s state=UNCERTAIN",
                snapshot.id,
                snapshot.attempt_count,
            )
        else:
            db.rollback()
        fresh = repo.get_snapshot_by_id(db, snapshot.id)
        db.rollback()
        return fresh
    except SQLAlchemyError as exc:
        logger.warning("gmail_draft_stale_classify_failed error_type=%s", type(exc).__name__)
        return None


def _fresh_snapshot(session_factory, ledger_id: int) -> "repo.LedgerSnapshot | None | Exception":
    db = session_factory()
    try:
        snapshot = repo.get_snapshot_by_id(db, ledger_id)
        db.rollback()
        return snapshot
    except SQLAlchemyError as exc:
        return exc
    finally:
        db.close()


def _run_db(session_factory: Callable[[], Session], operation):
    """Run `operation(db)` in its own session. A DB error raised before any
    commit is a definite rollback: no authority, nothing transmitted. An
    unrecognized IntegrityError is re-raised, never read as a lost race."""
    db = session_factory()
    try:
        return operation(db)
    except IntegrityError:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        logger.warning("gmail_draft_db_error error_type=%s", type(exc).__name__)
        try:
            db.rollback()
        except SQLAlchemyError:
            pass
        return _outcome("STATUS_UNAVAILABLE")
    finally:
        db.close()


# --- phase 1a: claim ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Claimed:
    claim: repo.Claim


def _commit_authority(db: Session, claim: repo.Claim, step: str) -> "_Claimed | GmailDraftOutcome":
    """Authority exists only if the commit is ACKNOWLEDGED. A raising commit
    may have succeeded server-side -- it still grants nothing here."""
    try:
        db.commit()
    except SQLAlchemyError as exc:
        logger.warning(
            "gmail_draft_commit_unacknowledged step=%s ledger_id=%s attempt=%s error_type=%s",
            step,
            claim.ledger_id,
            claim.attempt_count,
            type(exc).__name__,
        )
        return _outcome("STATUS_UNSAVED")
    return _Claimed(claim)


def _report_existing(db: Session, snapshot: repo.LedgerSnapshot) -> GmailDraftOutcome | None:
    """The duplicate/status path on an UNLOCKED snapshot (its transaction
    already ended). None means: a full-freshness path may proceed (FAILED
    retry, or an abandoned unarmed claim)."""
    now = _now()
    if snapshot.state in (repo.CREATED, repo.UNCERTAIN):
        return _report(snapshot)
    if snapshot.state == repo.CREATING:
        if snapshot.armed:
            if _is_stale_armed(snapshot, now):
                return _report(_classify_stale(db, snapshot) or snapshot)
            return _report(snapshot)
        if not _lease_expired(snapshot, now):
            return _report(snapshot)
    return None


def _phase_claim(capability: str, config: _Config):
    def operation(db: Session):
        link = get_link_by_capability(db, capability)
        if link is None:
            return _outcome("UNKNOWN_CAPABILITY")
        link_id = link.id
        existing = repo.get_snapshot_by_link(db, link_id)
        db.rollback()  # END the unlocked reporting read before any lock is taken
        if existing is not None:
            report = _report_existing(db, existing)
            if report is not None:
                return report

        fresh = revalidate_approved_link_locked(db, link_id)
        if isinstance(fresh, str):
            db.rollback()
            return _outcome(fresh)
        rendered = _render(fresh, config)
        if isinstance(rendered, str):
            db.rollback()
            return _outcome(rendered)
        handoff = _handoff(fresh)
        locked = repo.lock_by_link(db, link_id)  # ledger LAST
        now = _now()
        if locked is None:
            try:
                claim = repo.insert_claim(
                    db, link_id=link_id, account_key=config.account_key, handoff=handoff, now=now
                )
            except IntegrityError as exc:
                db.rollback()
                if repo.classify_ledger_conflict(exc) != repo.LINK_CONFLICT:
                    raise
                winner = repo.get_snapshot_by_link(db, link_id)  # another worker won
                db.rollback()
                return _report(winner)
            return _commit_authority(db, claim, "claim")
        if locked.handoff != handoff:
            db.rollback()
            return _outcome("PACKAGE_REPLACED")
        if locked.state == repo.FAILED:
            claim = repo.retry_failed(db, locked, now=now)
            step = "retry"
        elif locked.state == repo.CREATING and not locked.armed and _lease_expired(locked, now):
            cutoff = now - timedelta(seconds=CLAIM_LEASE_SECONDS)
            claim = repo.takeover_pre_fence(db, locked, lease_cutoff=cutoff, now=now)
            step = "takeover"
        else:
            db.rollback()
            if _is_stale_armed(locked, now):
                return _report(_classify_stale(db, locked) or locked)
            return _report(locked)
        if claim is None:
            db.rollback()
            return _report(repo.get_snapshot_by_link(db, link_id))
        return _commit_authority(db, claim, step)

    return operation


# --- phase 1b: arm (the network permit) ----------------------------------------------------


@dataclass(frozen=True, repr=False)
class _Permit:
    claim: repo.Claim
    marker: str
    message: DraftMessage
    target: DraftTarget
    deadline_at: datetime

    def __repr__(self) -> str:
        return f"_Permit(ledger_id={self.claim.ledger_id}, attempt={self.claim.attempt_count})"


_RETRY_MARKER = object()


def _release(db: Session, claim: repo.Claim, code: str) -> GmailDraftOutcome:
    """The owner gives up an UNARMED claim with a definite refusal (nothing
    was ever transmitted): CREATING -> FAILED. Best effort; a lost write
    only delays a later explicit press until the lease expires."""
    try:
        if repo.finalize_failed(db, claim, marker=None, error_code=code, now=_now()):
            db.commit()
        else:
            db.rollback()
    except SQLAlchemyError as exc:
        logger.warning("gmail_draft_release_failed error_type=%s", type(exc).__name__)
    return _outcome(code)


def _arm_once(db: Session, claimed: _Claimed, config: _Config):
    claim = claimed.claim
    fresh = revalidate_approved_link_locked(db, claim.link_id)
    if isinstance(fresh, str):
        db.rollback()
        return _release(db, claim, fresh)
    rendered = _render(fresh, config)
    if isinstance(rendered, str):
        db.rollback()
        return _release(db, claim, rendered)
    locked = repo.lock_by_link(db, claim.link_id)  # ledger LAST
    owned = (
        locked is not None
        and locked.id == claim.ledger_id
        and locked.state == repo.CREATING
        and locked.claim_token == claim.claim_token
        and locked.attempt_count == claim.attempt_count
        and not locked.armed
    )
    if not owned:
        db.rollback()
        return _report(locked)
    if locked.handoff != _handoff(fresh):
        db.rollback()
        return _release(db, claim, "PACKAGE_REPLACED")

    marker = new_marker()
    now = _now()
    bundle = repo.AttemptBundle(
        marker_message_id=marker,
        content_sha256=content_hash(
            RENDERER_VERSION, config.from_address, rendered.subject, rendered.body_lf
        ),
        renderer_version=RENDERER_VERSION,
        drafts_mailbox=config.mailbox,
        drafts_mailbox_wire=config.mailbox_wire,
        attempt_budget_seconds=config.budget_seconds,
    )
    try:
        armed = repo.begin_append(db, claim, account_key=config.account_key, bundle=bundle, now=now)
    except IntegrityError as exc:
        db.rollback()  # raised by execute, BEFORE any COMMIT: a definite rollback
        if repo.classify_ledger_conflict(exc) != repo.MARKER_CONFLICT:
            raise
        return _RETRY_MARKER
    if not armed:
        db.rollback()
        return _report(repo.get_snapshot_by_link(db, claim.link_id))
    try:
        db.commit()
    except SQLAlchemyError as exc:
        # §10 A: the server may have committed the fence -- but without the
        # acknowledgment this operation NEVER invokes the APPEND.
        logger.warning(
            "gmail_draft_commit_unacknowledged step=arm ledger_id=%s attempt=%s error_type=%s",
            claim.ledger_id,
            claim.attempt_count,
            type(exc).__name__,
        )
        return _outcome("STATUS_UNSAVED")
    return _Permit(
        claim=claim,
        marker=marker,
        message=DraftMessage(config.from_address, rendered.subject, rendered.body_lf, marker),
        target=DraftTarget(config.account_key, config.mailbox, config.mailbox_wire),
        deadline_at=now + timedelta(seconds=config.budget_seconds),
    )


def _phase_arm(claimed: _Claimed, config: _Config):
    def operation(db: Session):
        for _ in range(_MAX_MARKER_TRIES):
            result = _arm_once(db, claimed, config)
            if result is not _RETRY_MARKER:
                return result
        return _outcome("STATUS_UNAVAILABLE")

    return operation


# --- phase 3: finalize + DB-only recovery ---------------------------------------------------


@dataclass(frozen=True, repr=False)
class _Evidence:
    """Immutable in-memory evidence of THIS live operation's provider call.
    It is never reconstructed from logs, ledger state, a replay or another
    attempt."""

    kind: str  # CREATED | FAILED | UNCERTAIN
    ledger_id: int
    attempt_count: int
    claim_token: str
    marker: str
    error_code: str | None = None
    uid_validity: int | None = None
    draft_uid: int | None = None

    def __repr__(self) -> str:
        return f"_Evidence(kind={self.kind}, ledger_id={self.ledger_id})"


def _finalize_cas(db: Session, permit: _Permit, evidence: _Evidence) -> bool:
    now = _now()
    if evidence.kind == repo.CREATED:
        return repo.finalize_created(
            db,
            permit.claim,
            marker=permit.marker,
            uid_validity=evidence.uid_validity,
            draft_uid=evidence.draft_uid,
            now=now,
        )
    if evidence.kind == repo.FAILED:
        return repo.finalize_failed(
            db, permit.claim, marker=permit.marker, error_code=evidence.error_code, now=now
        )
    return repo.finalize_uncertain(
        db, permit.claim, marker=permit.marker, error_code=evidence.error_code, now=now
    )


def _try_cas(session_factory, operation) -> bool:
    """Run one CAS + commit. True only on rowcount 1 AND an acknowledged
    commit; any exception or lost race is False (the caller re-reads)."""
    db = session_factory()
    try:
        if not operation(db):
            db.rollback()
            return False
        db.commit()
        return True
    except SQLAlchemyError as exc:
        logger.warning("gmail_draft_finalize_unacknowledged error_type=%s", type(exc).__name__)
        return False
    finally:
        db.close()


def _finalize(session_factory, permit: _Permit, evidence: _Evidence) -> GmailDraftOutcome:
    """Persist the provider outcome; on a lost race or a lost commit
    acknowledgment, recover with fresh reads and DB-ONLY CAS (§10 B/C).
    Never a second APPEND."""
    target = evidence.kind
    if _try_cas(session_factory, lambda db: _finalize_cas(db, permit, evidence)):
        return _outcome(target)
    for _ in range(_MAX_RECOVERY_READS):
        snapshot = _fresh_snapshot(session_factory, permit.claim.ledger_id)
        if isinstance(snapshot, Exception):
            continue
        if snapshot is None:
            return _outcome("STATUS_UNAVAILABLE")
        same_attempt = (
            snapshot.attempt_count == evidence.attempt_count
            and snapshot.marker_message_id == evidence.marker
        )
        if not same_attempt:
            return _report(snapshot)  # respect another attempt's authority
        if snapshot.state == repo.CREATED:
            return _outcome("CREATED" if target == repo.CREATED else "ALREADY_CREATED")
        if snapshot.state == repo.CREATING and snapshot.claim_token == evidence.claim_token:
            if _try_cas(session_factory, lambda db: _finalize_cas(db, permit, evidence)):
                return _outcome(target)
            continue
        if snapshot.state == repo.UNCERTAIN:
            if target != repo.CREATED:
                return _outcome("UNCERTAIN")  # never UNCERTAIN -> FAILED
            # §10 B: the stale classifier won; this live operation's retained
            # tagged OK is positive evidence for exactly this attempt.
            if _try_cas(
                session_factory,
                lambda db: repo.retained_ok_created(
                    db,
                    ledger_id=evidence.ledger_id,
                    attempt_count=evidence.attempt_count,
                    marker=evidence.marker,
                    uid_validity=evidence.uid_validity,
                    draft_uid=evidence.draft_uid,
                    now=_now(),
                ),
            ):
                logger.info(
                    "gmail_draft_retained_ok ledger_id=%s attempt=%s state=CREATED",
                    evidence.ledger_id,
                    evidence.attempt_count,
                )
                return _outcome("CREATED")
            continue
        if snapshot.state == repo.FAILED and target == repo.FAILED:
            return _outcome("FAILED")
        return _report(snapshot)
    # The database stayed unavailable: no network retry, ever.
    return _outcome("PERSISTENCE_UNCERTAIN" if target == repo.CREATED else "FINALIZE_UNSAVED")


def _evidence(permit: _Permit, kind: str, **values) -> _Evidence:
    return _Evidence(
        kind=kind,
        ledger_id=permit.claim.ledger_id,
        attempt_count=permit.claim.attempt_count,
        claim_token=permit.claim.claim_token,
        marker=permit.marker,
        **values,
    )


# --- public entry points ------------------------------------------------------------------


def _log(event: str, outcome: GmailDraftOutcome, started: float, **ids) -> None:
    fields = " ".join(f"{name}={value}" for name, value in ids.items())
    logger.info(
        "%s %s result=%s duration_ms=%d",
        event,
        fields,
        outcome.code,
        int((time.monotonic() - started) * 1000),
    )


async def handle_create(
    session_factory: Callable[[], Session],
    settings,
    capability: str,
    *,
    provider: DraftProvider | None = None,
) -> GmailDraftOutcome:
    """ "📧 Gmail-Entwurf erstellen" (already authorized, private chat, all
    three flags on). At most ONE APPEND per invocation, and only after the
    acknowledged `begin_append` commit."""
    started = time.monotonic()
    if not gmail_draft_flags_enabled(settings):
        return _outcome("GMAIL_DRAFT_DISABLED")
    if not APPROVAL_CAPABILITY_PATTERN.fullmatch(capability or ""):
        return _outcome("UNKNOWN_CAPABILITY")
    config = _static_config(settings)
    if isinstance(config, GmailDraftOutcome):
        _log("gmail_draft_create", config, started)
        return config

    claimed = _run_db(session_factory, _phase_claim(capability, config))
    if isinstance(claimed, GmailDraftOutcome):
        _log("gmail_draft_create", claimed, started)
        return claimed
    permit = _run_db(session_factory, _phase_arm(claimed, config))
    if isinstance(permit, GmailDraftOutcome):
        _log("gmail_draft_create", permit, started, ledger_id=claimed.claim.ledger_id)
        return permit

    provider = provider or build_provider(settings)
    try:
        result = await asyncio.to_thread(
            provider.create_draft, permit.message, permit.target, permit.deadline_at
        )
    except DraftCreateDefiniteError as exc:
        evidence = _evidence(permit, repo.FAILED, error_code=exc.code)
    except asyncio.CancelledError:
        # The worker thread may still be running: the attempt can only be
        # UNCERTAIN, never retryable.
        _finalize(
            session_factory, permit, _evidence(permit, repo.UNCERTAIN, error_code="CANCELLED")
        )
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, DraftProviderError) else "OUTCOME_UNKNOWN"
        logger.warning(
            "gmail_draft_append_ambiguous ledger_id=%s error_type=%s",
            permit.claim.ledger_id,
            type(exc).__name__,
        )
        evidence = _evidence(permit, repo.UNCERTAIN, error_code=code)
    else:
        evidence = _evidence(
            permit, repo.CREATED, uid_validity=result.uid_validity, draft_uid=result.uid
        )
    outcome = _finalize(session_factory, permit, evidence)
    _log(
        "gmail_draft_create",
        outcome,
        started,
        ledger_id=permit.claim.ledger_id,
        link_id=permit.claim.link_id,
        attempt=permit.claim.attempt_count,
    )
    return outcome


def _reconcile_snapshot(capability: str):
    def operation(db: Session):
        link = get_link_by_capability(db, capability)
        if link is None:
            return _outcome("UNKNOWN_CAPABILITY")
        snapshot = repo.get_snapshot_by_link(db, link.id)
        db.rollback()
        if snapshot is not None and _is_stale_armed(snapshot, _now()):
            snapshot = _classify_stale(db, snapshot) or snapshot
        return snapshot if snapshot is not None else _outcome("NO_DRAFT_STATUS")

    return operation


async def handle_reconcile(
    session_factory: Callable[[], Session],
    settings,
    capability: str,
    *,
    provider: DraftProvider | None = None,
) -> GmailDraftOutcome:
    """ "🔄 Gmail-Status prüfen" for an UNCERTAIN attempt: a read-only lookup
    of the ORIGINAL frozen target and marker. Exactly one match -> CREATED;
    anything else leaves it UNCERTAIN. Never grants a claim or an APPEND."""
    started = time.monotonic()
    if not gmail_draft_flags_enabled(settings):
        return _outcome("GMAIL_DRAFT_DISABLED")
    if not APPROVAL_CAPABILITY_PATTERN.fullmatch(capability or ""):
        return _outcome("UNKNOWN_CAPABILITY")
    username = getattr(settings, "gmail_username", "")
    password = getattr(settings, "gmail_app_password", "")
    if not is_configured(username) or not is_configured(password):
        return _outcome("CONFIG_INVALID")

    snapshot = _run_db(session_factory, _reconcile_snapshot(capability))
    if isinstance(snapshot, GmailDraftOutcome):
        return snapshot
    if snapshot.state != repo.UNCERTAIN:
        return _report(snapshot)
    if snapshot.account_key != normalize_account_key(username):
        return _outcome("ACCOUNT_CHANGED")
    now = _now()
    min_age = timedelta(seconds=int(settings.gmail_draft_reconcile_min_age_seconds))
    ready_at = max(ensure_utc(snapshot.append_started_at) + min_age, snapshot.attempt_deadline_at)
    if now < ready_at:
        return _outcome("TOO_EARLY")
    marker = snapshot.marker_message_id or ""
    if not MESSAGE_ID_PATTERN.fullmatch(marker):
        return _outcome("STILL_UNCERTAIN")
    target = ReconcileTarget(
        DraftTarget(snapshot.account_key, snapshot.drafts_mailbox, snapshot.drafts_mailbox_wire),
        marker,
    )
    provider = provider or build_provider(settings)
    deadline_at = now + timedelta(seconds=int(settings.gmail_draft_attempt_budget_seconds))
    try:
        result = await asyncio.to_thread(provider.find_by_message_id, target, deadline_at)
    except Exception as exc:
        logger.warning(
            "gmail_draft_reconcile_lookup_failed ledger_id=%s error_type=%s",
            snapshot.id,
            type(exc).__name__,
        )
        outcome = _outcome("STILL_UNCERTAIN")
        _log("gmail_draft_reconcile", outcome, started, ledger_id=snapshot.id)
        return outcome
    if len(result.uids) != 1:
        outcome = _outcome("STILL_UNCERTAIN")
        _log("gmail_draft_reconcile", outcome, started, ledger_id=snapshot.id)
        return outcome

    uid = result.uids[0]
    if _try_cas(
        session_factory,
        lambda db: repo.reconcile_created(
            db,
            ledger_id=snapshot.id,
            attempt_count=snapshot.attempt_count,
            marker=marker,
            uid_validity=result.uid_validity,
            draft_uid=uid,
            now=_now(),
        ),
    ):
        outcome = _outcome("RECONCILED")
    else:
        fresh = _fresh_snapshot(session_factory, snapshot.id)
        if isinstance(fresh, Exception):
            outcome = _outcome("FINALIZE_UNSAVED")
        elif fresh is not None and fresh.state == repo.UNCERTAIN:
            outcome = _outcome("STILL_UNCERTAIN")
        elif fresh is not None and fresh.reconciled and fresh.draft_uid == uid:
            outcome = _outcome("RECONCILED")
        else:
            outcome = _report(fresh)
    _log("gmail_draft_reconcile", outcome, started, ledger_id=snapshot.id)
    return outcome
