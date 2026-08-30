import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    CompanyResearchIdentityAlias,
    CompanyResearchRecord,
    JobRecord,
    ProcessedEmailMessage,
    UserProfile,
)
from app.domain.status_transitions import validate_transition
from app.models.application_status import ApplicationStatus
from app.models.company_research import CompanyResearchData
from app.models.job import Job, JobScore

DEFAULT_PROFILE_SKILLS = [
    "python",
    "fastapi",
    "flask",
    "mysql",
    "mongodb",
    "git",
    "pytest",
]

# Which Job fields feed the dedup fingerprint, per source. Default (used by
# every source not listed here, e.g. "bundesagentur" and manual
# /jobs/score calls) is unchanged from the original formula:
# source+company+title+url. This must stay exactly as-is for existing
# sources — it's the identity of every JobRecord already persisted.
#
# "xing" is the one deliberate exception: XING digest emails embed a
# per-recipient tracking redirect as the job's URL (see
# app/collectors/xing_email.py's module docstring), and that URL has been
# confirmed to differ across separate emails advertising the exact same
# real posting. Including it in the fingerprint would make the same
# real-world job dedup-unstable (new JobRecord created every time XING
# rotates the tracking URL) — the same failure class as the
# Bundesagentur title-fallback bug fixed earlier, just in a different
# field. location is used as a substitute distinguishing field instead.
_DEFAULT_FINGERPRINT_FIELDS: tuple[str, ...] = ("source", "company", "title", "url")
_FINGERPRINT_FIELDS_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "xing": ("source", "company", "title", "location"),
}


def _fingerprint(job: Job) -> str:
    fields = _FINGERPRINT_FIELDS_BY_SOURCE.get(job.source, _DEFAULT_FINGERPRINT_FIELDS)
    values = []
    for field in fields:
        raw = getattr(job, field)
        text = str(raw).rstrip("/") if field == "url" else str(raw)
        values.append(text.strip().casefold())
    canonical = "|".join(values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_job_by_fingerprint(db: Session, job: Job) -> JobRecord | None:
    """Return the persisted record representing ``job``, if one exists."""
    return db.scalar(select(JobRecord).where(JobRecord.fingerprint == _fingerprint(job)))


def get_or_create_default_profile(db: Session) -> UserProfile:
    profile = db.scalar(select(UserProfile).where(UserProfile.name == "default"))
    if profile:
        return profile
    profile = UserProfile(name="default", skills_json=json.dumps(DEFAULT_PROFILE_SKILLS))
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def profile_skills(profile: UserProfile) -> set[str]:
    return set(json.loads(profile.skills_json))


def upsert_job(db: Session, job: Job, score: JobScore) -> tuple[JobRecord, bool]:
    fingerprint = _fingerprint(job)
    existing = get_job_by_fingerprint(db, job)
    now = datetime.now(UTC)
    if existing:
        existing.last_seen_at = now
        existing.score = score.score
        existing.recommendation = score.recommendation
        existing.skills_json = json.dumps(job.skills)
        existing.data_confidence = score.data_confidence
        existing.skill_source = job.skill_source
        existing.must_have_skills_json = json.dumps(job.must_have_skills)
        existing.nice_to_have_skills_json = json.dumps(job.nice_to_have_skills)
        if job.description.strip():
            existing.description = job.description
        db.commit()
        db.refresh(existing)
        return existing, False

    record = JobRecord(
        fingerprint=fingerprint,
        source=job.source,
        title=job.title,
        company=job.company,
        location=job.location,
        url=str(job.url),
        description=job.description,
        skills_json=json.dumps(job.skills),
        data_confidence=score.data_confidence,
        skill_source=job.skill_source,
        must_have_skills_json=json.dumps(job.must_have_skills),
        nice_to_have_skills_json=json.dumps(job.nice_to_have_skills),
        score=score.score,
        recommendation=score.recommendation,
        status="NEW",
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record, True


def list_jobs(
    db: Session,
    status: ApplicationStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[JobRecord]:
    stmt = select(JobRecord).order_by(JobRecord.last_seen_at.desc())
    if status is not None:
        stmt = stmt.where(JobRecord.status == status.value)
    stmt = stmt.limit(limit).offset(offset)
    return list(db.scalars(stmt).all())


def get_job_by_id(db: Session, job_id: int) -> JobRecord | None:
    return db.get(JobRecord, job_id)


def update_job_status(db: Session, job_id: int, new_status: ApplicationStatus) -> JobRecord | None:
    """Update a job's status after validating the transition.

    Returns None if the job does not exist. Raises InvalidStatusTransitionError
    (from app.domain.status_transitions) if the transition is not allowed.
    """
    record = db.get(JobRecord, job_id)
    if record is None:
        return None

    current_status = ApplicationStatus(record.status)
    validate_transition(current_status, new_status)

    record.status = new_status.value
    db.commit()
    db.refresh(record)
    return record


def is_message_processed(db: Session, source: str, message_id: str) -> bool:
    """True if this source has already processed an email with this Message-ID.

    Used by email-based collectors (e.g. XingEmailCollector) to avoid
    re-parsing the same message on every fetch() without mutating the
    mailbox itself (no read/unread flag changes) — see
    app/collectors/xing_email.py.
    """
    return (
        db.scalar(
            select(ProcessedEmailMessage.id).where(
                ProcessedEmailMessage.source == source,
                ProcessedEmailMessage.message_id == message_id,
            )
        )
        is not None
    )


def mark_message_processed(db: Session, source: str, message_id: str) -> None:
    """Record that this source has processed an email with this Message-ID.

    Idempotent: safe to call even if already marked (e.g. a duplicate
    Message-ID seen twice in the same run), so it never raises a unique
    constraint violation.
    """
    if is_message_processed(db, source, message_id):
        return
    db.add(ProcessedEmailMessage(source=source, message_id=message_id))
    db.commit()


_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    """Canonical form used for company-research identity lookups.

    NFKC-normalizes (folds Unicode compatibility/width/ligature variants —
    e.g. fullwidth or fraktur lookalikes — to a common form before
    comparison, so two visually-distinct-but-equivalent spellings of the
    same name identify as the same company), collapses internal whitespace
    runs to a single space, strips, then casefolds. Returns "" for a
    blank/whitespace-only name — callers (CompanyResearchService) must treat
    an empty result as "no usable company identity", never as a valid,
    mergeable identity of its own (a shared empty-string bucket would
    silently merge every company with a missing/garbled name into one
    record).
    """
    normalized = unicodedata.normalize("NFKC", name)
    normalized = _WHITESPACE_RUN.sub(" ", normalized)
    return normalized.strip().casefold()


def normalize_domain(url_or_domain: str) -> str | None:
    """Extract a canonical hostname from a URL or bare domain, or None if
    one can't be determined or is unsafe to treat as a hostname.

    Reserved for a future provider with a genuine, trusted company-domain
    source — Company Research v1's service layer never derives a domain
    from `Job.url` (a job-posting/job-board URL is not the company's own
    website; see app/services/company_research.py) and its only provider
    makes no network calls at all, so this function has no v1 call site
    that reaches it with untrusted input. Kept deliberately strict anyway:

    - Only bare hostnames ("example.com", "www.example.com") or explicit
      http(s) URLs are accepted; any other scheme (javascript:, file:,
      ftp:, ...) or a colon-before-the-first-slash with no "//" (a
      malformed/foreign scheme prefix like "http:example.com") is rejected
      rather than guessed at.
    - Whitespace and control characters anywhere in the input are rejected.
    - Userinfo tricks (`https://example.com@evil.com`) canonicalize to the
      real hostname after the last "@" (`evil.com`), matching how
      browsers/HTTP clients actually resolve the authority — never the
      misleading userinfo-looking prefix.
    - An invalid port (`https://example.com:abc/`) is rejected.
    - Output is always lowercase with a leading "www." stripped, never
      including scheme/path/port.
    """
    # Only plain leading/trailing spaces are trimmed here — a tab, newline,
    # or other whitespace/control char anywhere (including the edges) is
    # rejected outright by the check below rather than silently stripped,
    # since a legitimate domain never contains one.
    candidate = url_or_domain.strip(" ")
    if not candidate or any(ch.isspace() or ord(ch) < 0x20 for ch in candidate):
        return None

    if "://" in candidate:
        scheme = candidate.partition("://")[0].lower()
        if scheme not in ("http", "https"):
            return None
    elif ":" in candidate.split("/", 1)[0]:
        # A colon before the first "/" with no "//" is a malformed/foreign
        # scheme prefix ("http:example.com", "javascript:alert(1)"), not a
        # bare hostname — reject rather than guess.
        return None
    else:
        candidate = f"https://{candidate}"

    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
        _ = parsed.port  # Accessed only to trigger ValueError on a bad port.
    except ValueError:
        return None

    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    return host or None


class CompanyResearchWriteOutcome(StrEnum):
    """What actually happened to a caller's data in upsert_company_research
    / the internal helpers it delegates to — replaces an earlier ambiguous
    `created: bool` that couldn't distinguish "my write became canonical"
    from "a concurrent write already won and mine was discarded" (Codex
    re-review finding RR-M-03).
    """

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    SUPERSEDED = "SUPERSEDED"


class CompanyResearchConsistencyError(Exception):
    """Raised when a persistence invariant that must always hold is violated
    anyway (Codex re-review finding, section 5): e.g. reloading the row a
    UNIQUE constraint IntegrityError implies must exist comes back None. No
    code path in this project deletes CompanyResearchRecord or
    CompanyResearchIdentityAlias rows, so this should be unreachable —
    raised instead of silently returning None and letting a caller crash
    later with an unrelated AttributeError.
    """


def _identity_key(normalized_domain: str | None, normalized_company_name: str) -> str:
    """The single, DB-enforced-unique identity a CompanyResearchRecord is
    stored/looked-up under — domain-based when a domain is known (the
    stronger signal: two differently-named legal entities essentially never
    share a domain), name-based otherwise. See
    get_company_research_by_identity for how a domain-bearing lookup can
    still fall back to a name-only record, and why it never falls back to a
    record that already has a *different* known domain.
    """
    if normalized_domain:
        return f"domain:{normalized_domain}"
    return f"name:{normalized_company_name}"


def get_company_research_by_identity(
    db: Session, normalized_domain: str | None, normalized_company_name: str
) -> CompanyResearchRecord | None:
    """Look up a cached research record by exact identity.

    normalized_domain present: try the exact "domain:<x>" identity first.
    If that's not found, a name-only record (one that has never had a known
    domain of its own — `normalized_domain IS NULL`) with a matching name is
    an acceptable fallback: it's the same record a domain-less lookup for
    this company would already have found, now being enriched with a domain
    for the first time. A record that already carries a *different* known
    domain is never matched this way — "Acme GmbH" at acme.de and "Acme
    GmbH" at acme.com are two distinct companies as far as this store is
    concerned, never merged just because their display names collide.

    normalized_domain absent: only the exact "name:<x>" identity applies.
    """
    if normalized_domain:
        exact = db.scalar(
            select(CompanyResearchRecord).where(
                CompanyResearchRecord.identity_key
                == _identity_key(normalized_domain, normalized_company_name)
            )
        )
        if exact is not None:
            return exact
        candidate = db.scalar(
            select(CompanyResearchRecord).where(
                CompanyResearchRecord.identity_key == _identity_key(None, normalized_company_name)
            )
        )
        if candidate is not None and candidate.normalized_domain is None:
            return candidate
        return None

    return db.scalar(
        select(CompanyResearchRecord).where(
            CompanyResearchRecord.identity_key == _identity_key(None, normalized_company_name)
        )
    )


def _company_research_content_fields(data: CompanyResearchData) -> dict:
    return {
        "company_name": data.company_name,
        "company_domain": data.company_domain,
        "industry": data.industry,
        "headquarters": data.headquarters,
        "company_size": data.company_size,
        "short_summary": data.short_summary,
        "products_or_services_json": json.dumps(data.products_or_services),
        "technologies_json": json.dumps(data.technologies),
        "hiring_signals_json": json.dumps(data.hiring_signals),
        "relevant_facts_json": json.dumps(data.relevant_facts),
        "positive_signals_json": json.dumps(data.positive_signals),
        "risk_signals_json": json.dumps(data.risk_signals),
        "source_urls_json": json.dumps(data.source_urls),
        "evidence_json": json.dumps([evidence.model_dump() for evidence in data.evidence]),
        "confidence": data.confidence,
        "research_status": data.research_status,
        "provider_name": data.provider_name,
    }


def _reload_by_identity_key(db: Session, identity_key: str) -> CompanyResearchRecord:
    """Reload the row a UNIQUE(identity_key) IntegrityError implies must
    exist. Raises CompanyResearchConsistencyError instead of returning None
    if it somehow doesn't (see that class's docstring).
    """
    record = db.scalar(
        select(CompanyResearchRecord).where(CompanyResearchRecord.identity_key == identity_key)
    )
    if record is None:
        raise CompanyResearchConsistencyError(
            f"Expected a company_research row for identity_key={identity_key!r} after a "
            "UNIQUE constraint collision, but none was found."
        )
    return record


def is_usable_company_research(record: CompanyResearchRecord) -> bool:
    """True if `record` holds genuine, previously-successful research
    content — as opposed to a FAILED/PENDING diagnostic row
    (record_failed_attempt's minimal row when no prior research ever
    succeeded). research_status only ever reaches "PARTIAL" on an actual
    successful run, and researched_at is only ever set on success (see
    app/models/company_research.py's ResearchStatus).

    Used both here (FR-M-02: a successful concurrent create/update must
    never be discarded as SUPERSEDED just because it collided with an
    *unusable* FAILED row — see _reload_and_resolve_after_identity_collision)
    and by CompanyResearchService (RR-M-02: a FAILED row must never be
    reported as "stale but usable" prior research).
    """
    return record.research_status == "PARTIAL" and record.researched_at is not None


def get_known_domains_for_company_name(db: Session, normalized_company_name: str) -> list[str]:
    """All distinct known (non-null) domains among CompanyResearchRecord
    rows sharing this normalized_company_name — i.e. how many genuinely
    distinct, identifiable companies this display name could refer to.

    Used by CompanyResearchService (FR-M-01) to detect when a name-only
    resolution (no domain_hint — the only kind Company Research v1 ever
    performs, see H-02) would be unsafe to resolve automatically: if 2+
    known domains share this normalized name (e.g. "Acme GmbH" at acme.de
    *and* acme.com — two distinct companies, never merged, see H-01 /
    get_company_research_by_identity), silently picking one via the
    CompanyResearchIdentityAlias coordination mechanism would be an
    arbitrary, unverifiable guess between them.
    """
    return list(
        db.scalars(
            select(CompanyResearchRecord.normalized_domain)
            .where(
                CompanyResearchRecord.normalized_company_name == normalized_company_name,
                CompanyResearchRecord.normalized_domain.is_not(None),
            )
            .distinct()
        ).all()
    )


class AmbiguousCompanyIdentityError(Exception):
    """Raised by resolve_name_only_company_research when a name-only
    identity resolution (domain_hint is None — the only kind Company
    Research v1 ever performs, see H-02) cannot be resolved safely because
    2+ distinct known-domain companies already share this normalized
    company name (FR-M-01) — e.g. "Acme GmbH" has separate, legitimate
    research on file for both acme.de and acme.com (H-01: two different
    known non-null domains are always two different companies, never
    merged).

    Without a domain of its own to disambiguate with, there is no
    principled way to pick between them — CompanyResearchIdentityAlias is a
    race-safe *creation* coordination mechanism, not a source of truth for
    "which of several known companies does this name refer to," and must
    never be used to silently guess. Defined here (not in
    app.services.company_research, which re-exports it) since it's raised
    directly by this repository-layer resolution helper, and
    record_failed_attempt needs to be able to hit the same guard.
    app/api/routes.py maps it to 409.
    """


def resolve_name_only_company_research(
    db: Session, normalized_company_name: str
) -> CompanyResearchRecord | None:
    """The single identity-resolution routine for a name-only request
    (domain_hint is None — the only kind Company Research v1 ever has, see
    H-02) — shared by CompanyResearchService.get_cached, .get_or_run, and
    record_failed_attempt, so POST/GET/failure-recording can never diverge
    on what "the" record for a given company name is (FR-M-03: GET and a
    fresh-cache POST used to disagree with each other, and with what a
    force-refresh would resolve to, because each read identity a slightly
    different way).

    - Zero known (non-null-domain) records share this name: falls back to
      the exact "name:<x>" identity row if one exists (a genuine identity
      that has never had a domain attributed to it), else None (cache
      miss / create-eligible).
    - Exactly one known domain shares this name: that domain-bearing row
      IS the record. A name-only caller has no domain of its own to
      disambiguate with, but there is also nothing to disambiguate *from*
      — it is the sole safe canonical candidate, and is returned even
      though it doesn't match the exact "name:<x>" identity_key a naive
      domain-less lookup would use.
    - 2+ known domains share this name: raises AmbiguousCompanyIdentityError
      (FR-M-01) — unchanged.

    An exact "name:<x>" row and a known-domain row for the same
    normalized_company_name should never coexist once every writer that
    could create one goes through this same routine first (which, as of
    FR-M-03, includes record_failed_attempt) — but if they nonetheless do
    (a state only reachable via record_failed_attempt's alias-bypassing
    insert *before* this fix), the known-domain row wins deterministically:
    it carries strictly more identity information than a domainless row,
    and — having no known domain of its own — the stray name-only row can
    never be one side of an FR-M-01 domain-vs-domain ambiguity, so silently
    preferring the known-domain row is a safe, deterministic choice rather
    than an arbitrary guess between two live candidates.
    """
    known_domains = get_known_domains_for_company_name(db, normalized_company_name)
    if len(known_domains) >= 2:
        raise AmbiguousCompanyIdentityError(
            "Company identity is ambiguous: multiple known companies share this normalized name."
        )
    if len(known_domains) == 1:
        return get_company_research_by_identity(db, known_domains[0], normalized_company_name)
    return get_company_research_by_identity(db, None, normalized_company_name)


def _ensure_company_research_alias(
    db: Session, record: CompanyResearchRecord, normalized_company_name: str
) -> None:
    """Invariant (FR-M-02 Fix B): a successful canonical CompanyResearchRecord
    that is safe to reach via name-only resolution must have exactly one
    CompanyResearchIdentityAlias row for its normalized_company_name,
    pointing at it — unless a *different* record already legitimately owns
    that alias (Case C: a distinct company with an already-known, different
    domain — see _join_or_diverge_after_alias_conflict), in which case this
    record deliberately has none.

    _create_company_research already claims the alias atomically as part of
    a brand-new row's own insert transaction. This closes the remaining
    gap: _update_existing_company_research's in-place UPDATE (recovering a
    FAILED/PENDING row via record_failed_attempt's identity_key collision,
    or promoting a domainless row to a known domain) does NOT touch the
    alias table itself, since it isn't creating a new row — without this,
    a record that reached "successful, name-coordination-eligible" status
    purely via an in-place update could stay permanently unclaimed, and a
    *later* name-only create request would then "rediscover" the identity
    as unclaimed and create a duplicate row (see
    tests/test_company_research_repository.py's
    test_failed_then_success_then_domain_promotion_then_name_only_stays_one_record).

    Best-effort/self-healing, not part of the caller's own atomic write:
    called only after the record's own content is already durably
    committed, so a lost race here (someone else claims the alias first)
    just means that other, already-successful record is the rightful
    coordination target instead — never a correctness problem, only ever a
    no-op either way.
    """
    existing_alias = db.scalar(
        select(CompanyResearchIdentityAlias).where(
            CompanyResearchIdentityAlias.normalized_company_name == normalized_company_name
        )
    )
    if existing_alias is not None:
        # Either it already points at `record`, or a different record
        # legitimately owns this name's coordination slot (Case C) — never
        # reassign it.
        return

    db.add(
        CompanyResearchIdentityAlias(
            normalized_company_name=normalized_company_name,
            company_research_id=record.id,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Lost a race to claim it — some other record now legitimately owns
        # this name's coordination slot.
        db.rollback()


def _resolve_stale_write(
    db: Session,
    canonical: CompanyResearchRecord,
    data: CompanyResearchData,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
) -> tuple[CompanyResearchRecord, CompanyResearchWriteOutcome]:
    """FR-H-01: a caller's optimistic-concurrency write lost to a concurrent
    writer that already committed a newer version of the same row.

    If the concurrent winner represents the *same* company identity (our
    attempt has no domain of its own, or the winner's domain matches ours,
    or the winner is still domainless), this is an ordinary lost race —
    discard our payload, SUPERSEDED, exactly as before.

    But if our attempt carries a *different already-known* domain than the
    winner's, our payload represents a genuinely distinct company (the H-01
    hard identity rule: two different known non-null domains are two
    different companies — "Acme GmbH" at acme.com and "Acme GmbH" at
    acme.de are never the same row). It must never be silently dropped just
    because it lost an *unrelated* optimistic-concurrency race on someone
    else's row — persisted as its own standalone record instead, exactly
    the same Case C outcome as the non-concurrent "different known domain"
    path. The alias (if any) stays exactly where it already was; a Case C
    standalone record never claims or moves it.
    """
    if (
        normalized_domain is not None
        and canonical.normalized_domain is not None
        and canonical.normalized_domain != normalized_domain
    ):
        return _insert_standalone_company_research(
            db,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )
    return canonical, CompanyResearchWriteOutcome.SUPERSEDED


def _reload_and_resolve_after_identity_collision(
    db: Session,
    identity_key: str,
    data: CompanyResearchData,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
) -> tuple[CompanyResearchRecord, CompanyResearchWriteOutcome]:
    """Shared resolution for every "our INSERT/UPDATE collided with an
    already-existing row at this exact identity_key" IntegrityError (FR-M-02
    Fix A): reload the row the collision implies exists, then decide.

    If that row already holds usable research content
    (is_usable_company_research), it's a genuine concurrent-success-vs-
    success race — our payload is discarded, SUPERSEDED, same as before.

    But if it does NOT (a FAILED/PENDING diagnostic row — e.g.
    record_failed_attempt's minimal row from an earlier failed attempt,
    created without ever claiming a CompanyResearchIdentityAlias), a
    *successful* payload colliding with it must never be discarded as
    "superseded" by a row that was never actually usable research in the
    first place — that would silently lose the only good result anyone has
    ever produced for this identity. Instead, atomically upgrade that row
    in place via the normal update path (which also claims/ensures its
    alias — see _ensure_company_research_alias), keeping the same row id.
    """
    canonical = _reload_by_identity_key(db, identity_key)
    if is_usable_company_research(canonical):
        return canonical, CompanyResearchWriteOutcome.SUPERSEDED
    return _update_existing_company_research(
        db,
        canonical,
        data,
        normalized_domain=normalized_domain,
        normalized_company_name=normalized_company_name,
        identity_key=identity_key,
        expected_version=None,
    )


def upsert_company_research(
    db: Session,
    data: CompanyResearchData,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
    expected_version: int | None = None,
) -> tuple[CompanyResearchRecord, CompanyResearchWriteOutcome]:
    """Persist a *successful* provider result, reusing the same identity's
    row if one exists. Always marks the attempt as SUCCESS and bumps
    researched_at — for a failed attempt, see record_failed_attempt instead.

    Returns the row this identity now canonically maps to, plus a typed
    CompanyResearchWriteOutcome — CREATED/UPDATED if the caller's own `data`
    became (part of) that row's content, or SUPERSEDED if a concurrent
    writer's result won instead and the caller's data was discarded (never
    silently pretended to have been applied). See
    CompanyResearchService.get_or_run for how SUPERSEDED is surfaced to
    callers via CompanyResearchRunResponse.refresh_superseded.

    Race conditions handled, not just the happy path:

    1. Concurrent create, same identity_key — two callers both see "no
       existing record" and both try to insert the identical identity_key.
       The DB's UNIQUE constraint rejects the loser with IntegrityError;
       caught, rolled back, resolved by reloading the winner's row.
    2. Concurrent create, *different* identity_key for the same company
       (RR-M-01) — one caller resolves "name:<x>" (no domain known yet),
       another resolves "domain:<y>" for the same normalized company name.
       identity_key's UNIQUE constraint does nothing here since the two
       strings differ; see _create_company_research /
       _join_or_diverge_after_alias_conflict for the
       CompanyResearchIdentityAlias-based coordination that catches this.
    3. Concurrent refresh (stale write) — `expected_version`, when passed,
       must match the row's current `version` for the UPDATE to take
       effect (a version-checked conditional UPDATE, not a plain
       SELECT-then-blind-UPDATE). If a concurrent refresh already committed
       a newer version in between, this caller's UPDATE matches zero rows;
       the newer (winning) row is reloaded and returned as SUPERSEDED —
       this caller's now-stale result is discarded, never clobbering the
       newer one. See tests/test_company_research_service.py's
       concurrent-refresh test.
    """
    identity_key = _identity_key(normalized_domain, normalized_company_name)
    existing = get_company_research_by_identity(db, normalized_domain, normalized_company_name)

    if existing is not None:
        return _update_existing_company_research(
            db,
            existing,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
            identity_key=identity_key,
            expected_version=expected_version,
        )

    return _create_company_research(
        db,
        data,
        normalized_domain=normalized_domain,
        normalized_company_name=normalized_company_name,
        identity_key=identity_key,
    )


def _update_existing_company_research(
    db: Session,
    existing: CompanyResearchRecord,
    data: CompanyResearchData,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
    identity_key: str,
    expected_version: int | None,
) -> tuple[CompanyResearchRecord, CompanyResearchWriteOutcome]:
    if expected_version is not None and existing.version != expected_version:
        # In practice this is the branch that actually fires for a
        # concurrent-refresh loser: upsert_company_research always re-reads
        # `existing` fresh at call time, so by the time a caller that awaited
        # a slow provider call gets here, its own fresh lookup already
        # reflects any concurrent winner's committed version — the UPDATE
        # below's own rowcount==0 branch only covers the much narrower
        # window of a race landing between *this* function's own read and
        # its own UPDATE statement. Both branches must apply the same
        # FR-H-01 domain-divergence check, so both delegate to
        # _resolve_stale_write.
        return _resolve_stale_write(
            db,
            existing,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )

    now = datetime.now(UTC)
    content_fields = _company_research_content_fields(data)
    stmt = (
        update(CompanyResearchRecord)
        .where(
            CompanyResearchRecord.id == existing.id,
            CompanyResearchRecord.version == existing.version,
        )
        .values(
            identity_key=identity_key,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
            researched_at=now,
            last_attempt_at=now,
            last_attempt_status="SUCCESS",
            last_error=None,
            version=CompanyResearchRecord.version + 1,
            updated_at=now,
            **content_fields,
        )
    )
    try:
        result = db.execute(stmt)
        db.commit()
    except IntegrityError:
        db.rollback()
        return _reload_and_resolve_after_identity_collision(
            db,
            identity_key,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )

    if result.rowcount == 0:
        # Lost a race against a concurrent writer between our read and this
        # UPDATE — reload the canonical (winning) row instead of pretending
        # our now-stale write took effect. See the expected_version branch
        # above for why this specific window is narrow in practice.
        db.expire_all()
        canonical = db.get(CompanyResearchRecord, existing.id)
        if canonical is None:
            raise CompanyResearchConsistencyError(
                f"company_research row id={existing.id} disappeared during an update."
            )
        return _resolve_stale_write(
            db,
            canonical,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )

    db.refresh(existing)
    _ensure_company_research_alias(db, existing, normalized_company_name)
    return existing, CompanyResearchWriteOutcome.UPDATED


def _insert_standalone_company_research(
    db: Session,
    data: CompanyResearchData,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
) -> tuple[CompanyResearchRecord, CompanyResearchWriteOutcome]:
    """Insert a brand-new row under its own identity_key, deliberately not
    claiming a CompanyResearchIdentityAlias row for its name — used only for
    Case C (a same-named company that already carries a *different* known
    domain than the alias currently on file; see
    _join_or_diverge_after_alias_conflict). The alias must keep pointing at
    the record it already names; this company is a distinct entity.
    """
    identity_key = _identity_key(normalized_domain, normalized_company_name)
    now = datetime.now(UTC)
    content_fields = _company_research_content_fields(data)
    record = CompanyResearchRecord(
        identity_key=identity_key,
        normalized_domain=normalized_domain,
        normalized_company_name=normalized_company_name,
        researched_at=now,
        last_attempt_at=now,
        last_attempt_status="SUCCESS",
        last_error=None,
        version=1,
        created_at=now,
        updated_at=now,
        **content_fields,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _reload_and_resolve_after_identity_collision(
            db,
            identity_key,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )
    db.refresh(record)
    return record, CompanyResearchWriteOutcome.CREATED


def _join_or_diverge_after_alias_conflict(
    db: Session,
    data: CompanyResearchData,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
) -> tuple[CompanyResearchRecord, CompanyResearchWriteOutcome]:
    """Resolve the RR-M-01 race after losing the CompanyResearchIdentityAlias
    UNIQUE(normalized_company_name) race: another concurrent creator
    committed an alias for this name first, under a possibly-different
    identity_key than ours, between our own flush and our own commit.

    - Our attempt carries a domain and the alias's canonical record is
      still domainless: this is the same company, and a domain just became
      known for the first time — always promote the canonical record to
      carry it (a structural identity upgrade, applied regardless of which
      caller "wins", the same as the non-concurrent promotion path in
      get_company_research_by_identity/_update_existing_company_research).
    - Our attempt carries a domain that differs from the canonical record's
      already-known domain: Case C — a distinct company that happens to
      share a display name. Never merge; insert our own separate row
      without touching the alias.
    - Otherwise (we have no domain of our own, or it matches the canonical
      record's domain exactly): no new identity information — the alias's
      canonical record already *is* the record for this identity; our
      write is superseded and discarded, the same "first commit wins"
      convention already used for a plain identity_key collision.
    """
    alias = db.scalar(
        select(CompanyResearchIdentityAlias).where(
            CompanyResearchIdentityAlias.normalized_company_name == normalized_company_name
        )
    )
    if alias is None:
        raise CompanyResearchConsistencyError(
            f"Claiming the identity alias for {normalized_company_name!r} raised "
            "IntegrityError, but no alias row is visible afterward."
        )
    canonical = db.get(CompanyResearchRecord, alias.company_research_id)
    if canonical is None:
        raise CompanyResearchConsistencyError(
            f"Identity alias for {normalized_company_name!r} points to a missing "
            f"company_research row id={alias.company_research_id}."
        )

    if normalized_domain is not None and canonical.normalized_domain is None:
        identity_key = _identity_key(normalized_domain, normalized_company_name)
        return _update_existing_company_research(
            db,
            canonical,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
            identity_key=identity_key,
            expected_version=None,
        )

    if normalized_domain is not None and canonical.normalized_domain != normalized_domain:
        return _insert_standalone_company_research(
            db,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )

    return canonical, CompanyResearchWriteOutcome.SUPERSEDED


def _create_company_research(
    db: Session,
    data: CompanyResearchData,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
    identity_key: str,
) -> tuple[CompanyResearchRecord, CompanyResearchWriteOutcome]:
    """Create a brand-new CompanyResearchRecord for an identity that
    get_company_research_by_identity found nothing for.

    A plain INSERT + IntegrityError-on-identity_key catch (as used
    everywhere else in this module) is NOT sufficient on its own here
    (RR-M-01): two concurrent creators for the *same* company can
    legitimately resolve to two *different* identity_key values — one
    "name:<x>" (no domain known yet), one "domain:<y>" (domain known) — and
    identity_key's UNIQUE constraint does nothing to stop both inserts from
    succeeding as two separate rows, since the strings differ.
    CompanyResearchIdentityAlias's UNIQUE normalized_company_name column is
    the real coordination point across that name/domain boundary, claimed
    atomically alongside the new row in one transaction.
    """
    now = datetime.now(UTC)
    content_fields = _company_research_content_fields(data)
    record = CompanyResearchRecord(
        identity_key=identity_key,
        normalized_domain=normalized_domain,
        normalized_company_name=normalized_company_name,
        researched_at=now,
        last_attempt_at=now,
        last_attempt_status="SUCCESS",
        last_error=None,
        version=1,
        created_at=now,
        updated_at=now,
        **content_fields,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # Another row already exists at this exact identity_key — either a
        # concurrent creator racing for the same domain (ordinary,
        # already-race-safe case), or a FAILED/PENDING diagnostic row left
        # by an earlier failed attempt at this same identity (FR-M-02: must
        # be upgraded in place, never treated as if it superseded our
        # successful payload) — see
        # _reload_and_resolve_after_identity_collision.
        db.rollback()
        return _reload_and_resolve_after_identity_collision(
            db,
            identity_key,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )

    db.add(
        CompanyResearchIdentityAlias(
            normalized_company_name=normalized_company_name,
            company_research_id=record.id,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _join_or_diverge_after_alias_conflict(
            db,
            data,
            normalized_domain=normalized_domain,
            normalized_company_name=normalized_company_name,
        )

    db.refresh(record)
    return record, CompanyResearchWriteOutcome.CREATED


def record_failed_attempt(
    db: Session,
    *,
    normalized_domain: str | None,
    normalized_company_name: str,
    company_name: str,
    provider_name: str,
    error_message: str,
) -> CompanyResearchRecord:
    """Record a failed research attempt without disturbing any existing
    good research content (failure isolation — see
    CompanyResearchService.get_or_run).

    If a record already exists for this identity, only its attempt
    metadata (last_attempt_at/last_attempt_status/last_error) changes —
    research_status, researched_at, confidence, and all content fields are
    left exactly as they were. If no record exists at all, a minimal FAILED
    row is created so the failure is visible/diagnosable and so the next
    call retries automatically (a FAILED record is never "fresh" — see
    CompanyResearchService._is_fresh).

    `error_message` is bounded to CompanyResearchRecord.last_error's column
    length (500 chars) and must already be a plain message, never a raw
    traceback or anything that could contain secrets — callers pass
    `str(exc)`, not `traceback.format_exc()`.

    FR-M-03: when `normalized_domain` is None (v1's only live case),
    existence is resolved via resolve_name_only_company_research — the same
    routine CompanyResearchService.get_cached/get_or_run use — rather than
    a raw exact "name:<x>" lookup. Without this, a failed refresh attempt
    against an already-resolved sole known-domain record (e.g.
    "domain:acme.de") would miss it and create a stray, alias-less
    "name:<x>" duplicate instead of attaching failure metadata to the real
    canonical row.
    """
    identity_key = _identity_key(normalized_domain, normalized_company_name)
    bounded_error = error_message.strip()[:500]
    now = datetime.now(UTC)

    if normalized_domain is None:
        existing = resolve_name_only_company_research(db, normalized_company_name)
    else:
        existing = get_company_research_by_identity(db, normalized_domain, normalized_company_name)
    if existing is not None:
        existing.last_attempt_at = now
        existing.last_attempt_status = "FAILED"
        existing.last_error = bounded_error
        existing.updated_at = now
        db.commit()
        db.refresh(existing)
        return existing

    record = CompanyResearchRecord(
        identity_key=identity_key,
        normalized_domain=normalized_domain,
        normalized_company_name=normalized_company_name,
        company_name=company_name,
        provider_name=provider_name,
        research_status="FAILED",
        confidence=0.0,
        researched_at=None,
        last_attempt_at=now,
        last_attempt_status="FAILED",
        last_error=bounded_error,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        canonical = _reload_by_identity_key(db, identity_key)
        canonical.last_attempt_at = now
        canonical.last_attempt_status = "FAILED"
        canonical.last_error = bounded_error
        canonical.updated_at = now
        db.commit()
        db.refresh(canonical)
        return canonical
    db.refresh(record)
    return record
