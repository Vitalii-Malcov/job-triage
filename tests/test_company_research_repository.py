import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.models import CompanyResearchIdentityAlias, CompanyResearchRecord
from app.db.repositories import (
    AmbiguousCompanyIdentityError,
    CompanyResearchWriteOutcome,
    get_company_research_by_identity,
    get_known_domains_for_company_name,
    is_usable_company_research,
    normalize_company_name,
    normalize_domain,
    record_failed_attempt,
    resolve_name_only_company_research,
    upsert_company_research,
)
from app.models.company_research import CompanyResearchData, Evidence

CREATED = CompanyResearchWriteOutcome.CREATED
UPDATED = CompanyResearchWriteOutcome.UPDATED
SUPERSEDED = CompanyResearchWriteOutcome.SUPERSEDED


def _data(**overrides) -> CompanyResearchData:
    fields = {
        "company_name": "Acme GmbH",
        "provider_name": "job_data",
        "research_status": "PARTIAL",
        "evidence": [Evidence(type="FACT", claim="test claim", source_url="https://example.com")],
    }
    fields.update(overrides)
    return CompanyResearchData(**fields)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _file_session_factory(tmp_path, name: str):
    db_path = tmp_path / name
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


# --- normalize_company_name -------------------------------------------------


def test_normalize_company_name_strips_and_casefolds():
    assert normalize_company_name("  Acme GmbH  ") == "acme gmbh"


def test_normalize_company_name_collapses_internal_whitespace():
    assert normalize_company_name("Acme   GmbH\t\nHoldings") == "acme gmbh holdings"


def test_normalize_company_name_blank_and_whitespace_only_returns_empty():
    assert normalize_company_name("") == ""
    assert normalize_company_name("   ") == ""
    assert normalize_company_name("\t\n") == ""


def test_normalize_company_name_unicode_equivalent_forms_match():
    # U+FF21 FULLWIDTH LATIN CAPITAL LETTER A vs plain "A" — NFKC folds the
    # fullwidth form to its ASCII equivalent before casefolding.
    fullwidth = "Ａcme GmbH"
    assert normalize_company_name(fullwidth) == normalize_company_name("Acme GmbH")


def test_normalize_company_name_mixed_case_matches():
    assert normalize_company_name("ACME gmbh") == normalize_company_name("Acme GmbH")


# --- normalize_domain (adversarial matrix) ----------------------------------


def test_normalize_domain_accepts_bare_and_prefixed_forms():
    assert normalize_domain("example.com") == "example.com"
    assert normalize_domain("www.example.com") == "example.com"
    assert normalize_domain("https://example.com") == "example.com"
    assert normalize_domain("https://www.Example.com/path") == "example.com"
    assert normalize_domain("https://example.com:443/path") == "example.com"


def test_normalize_domain_rejects_dangerous_schemes():
    assert normalize_domain("javascript:alert(1)") is None
    assert normalize_domain("file:///etc/passwd") is None
    assert normalize_domain("ftp://example.com") is None


def test_normalize_domain_rejects_malformed_authority():
    assert normalize_domain("http:example.com/path") is None


def test_normalize_domain_rejects_control_and_whitespace_chars():
    assert normalize_domain("not a url") is None
    assert normalize_domain("example.com\n") is None
    assert normalize_domain("exa\tmple.com") is None
    assert normalize_domain("example.com\x00") is None


def test_normalize_domain_rejects_invalid_port():
    assert normalize_domain("https://example.com:abc/path") is None


def test_normalize_domain_userinfo_resolves_to_real_hostname():
    assert normalize_domain("https://example.com@evil.com") == "evil.com"


def test_normalize_domain_rejects_blank():
    assert normalize_domain("") is None
    assert normalize_domain("   ") is None


# --- H-01: identity collision (same name, different known domains) --------


def test_same_name_different_known_domains_are_two_separate_records():
    db = _db()

    first, outcome_first = upsert_company_research(
        db,
        _data(),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )
    second, outcome_second = upsert_company_research(
        db,
        _data(short_summary="different company"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )

    assert outcome_first == CREATED
    assert outcome_second == CREATED
    assert first.id != second.id
    assert first.normalized_domain == "acme.de"
    assert second.normalized_domain == "acme.com"

    # The first record's own content/evidence must be untouched by the
    # second, distinct company's upsert.
    reloaded_first = get_company_research_by_identity(db, "acme.de", "acme gmbh")
    assert reloaded_first.id == first.id
    assert reloaded_first.short_summary == ""

    total = db.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 2


def test_domain_lookup_falls_back_only_to_a_nameless_record():
    db = _db()

    # A record created without any known domain yet.
    nameless, _ = upsert_company_research(
        db, _data(), normalized_domain=None, normalized_company_name="acme gmbh"
    )

    # A domain-bearing lookup for the same name should "claim"/promote the
    # nameless record rather than create a duplicate.
    promoted, outcome = upsert_company_research(
        db,
        _data(short_summary="promoted"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )
    assert outcome == UPDATED
    assert promoted.id == nameless.id
    assert promoted.normalized_domain == "acme.com"
    assert promoted.identity_key == "domain:acme.com"

    # Now that the record has a known domain, a *different* domain with the
    # same name must never merge with it again.
    other, outcome_other = upsert_company_research(
        db,
        _data(short_summary="different"),
        normalized_domain="acme.io",
        normalized_company_name="acme gmbh",
    )
    assert outcome_other == CREATED
    assert other.id != promoted.id


def test_get_company_research_by_identity_domain_first_then_name_fallback():
    db = _db()
    upsert_company_research(
        db, _data(), normalized_domain="acme.com", normalized_company_name="acme gmbh"
    )

    by_domain = get_company_research_by_identity(db, "acme.com", "some other name")
    assert by_domain is not None
    assert by_domain.normalized_domain == "acme.com"

    # A domain-less lookup only ever searches the "name:<x>" identity — per
    # the H-01 fix, it must never cross over to match a record that already
    # carries a known (different) domain, even when the name matches. That
    # record is only reachable via a domain-bearing lookup (above) or the
    # domain-then-nameless-fallback path (see
    # test_domain_lookup_falls_back_only_to_a_nameless_record).
    assert get_company_research_by_identity(db, None, "acme gmbh") is None

    # Domain given but unmatched, and the only same-named record already has
    # a *different* known domain — must not merge.
    assert get_company_research_by_identity(db, "different-domain.com", "acme gmbh") is None
    assert get_company_research_by_identity(db, None, "unknown company") is None


# --- FR-M-01: known-domain ambiguity detection ------------------------------


def test_get_known_domains_for_company_name_zero_one_two():
    db = _db()
    assert get_known_domains_for_company_name(db, "acme gmbh") == []

    upsert_company_research(
        db, _data(), normalized_domain="acme.de", normalized_company_name="acme gmbh"
    )
    assert get_known_domains_for_company_name(db, "acme gmbh") == ["acme.de"]

    upsert_company_research(
        db, _data(), normalized_domain="acme.com", normalized_company_name="acme gmbh"
    )
    assert sorted(get_known_domains_for_company_name(db, "acme gmbh")) == ["acme.com", "acme.de"]

    # A domainless record for an unrelated name must never count.
    upsert_company_research(db, _data(), normalized_domain=None, normalized_company_name="other co")
    assert get_known_domains_for_company_name(db, "other co") == []


def test_is_usable_company_research():
    db = _db()
    partial, _ = upsert_company_research(
        db, _data(), normalized_domain=None, normalized_company_name="acme gmbh"
    )
    assert is_usable_company_research(partial) is True

    failed = record_failed_attempt(
        db,
        normalized_domain=None,
        normalized_company_name="other co",
        company_name="Other Co",
        provider_name="job_data",
        error_message="boom",
    )
    assert is_usable_company_research(failed) is False


# --- FR-M-03: sole known-domain identity resolution -------------------------


def test_resolve_name_only_zero_known_domains_falls_back_to_exact_name_row():
    db = _db()
    assert resolve_name_only_company_research(db, "acme gmbh") is None

    nameless, _ = upsert_company_research(
        db,
        _data(short_summary="nameless"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    resolved = resolve_name_only_company_research(db, "acme gmbh")
    assert resolved is not None
    assert resolved.id == nameless.id


def test_resolve_name_only_exactly_one_known_domain_resolves_to_it():
    db = _db()
    known, outcome = upsert_company_research(
        db,
        _data(short_summary="the-one"),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )
    assert outcome == CREATED

    resolved = resolve_name_only_company_research(db, "acme gmbh")

    assert resolved is not None
    assert resolved.id == known.id
    assert resolved.identity_key == "domain:acme.de"
    assert resolved.short_summary == "the-one"


def test_resolve_name_only_two_known_domains_raises_ambiguous():
    db = _db()
    upsert_company_research(
        db, _data(), normalized_domain="acme.de", normalized_company_name="acme gmbh"
    )
    upsert_company_research(
        db, _data(), normalized_domain="acme.com", normalized_company_name="acme gmbh"
    )

    with pytest.raises(AmbiguousCompanyIdentityError):
        resolve_name_only_company_research(db, "acme gmbh")


def test_resolve_name_only_prefers_known_domain_over_coexisting_stray_name_row():
    """Defensive: if a stray exact "name:<x>" row and a known-domain row for
    the same normalized_company_name ever coexist (a historical/pre-fix
    state — see resolve_name_only_company_research's docstring), the
    known-domain row must win deterministically rather than either being
    picked arbitrarily.
    """
    db = _db()
    known, _ = upsert_company_research(
        db,
        _data(short_summary="known-domain"),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )
    # Directly construct the coexisting stray row — no normal write path
    # (including record_failed_attempt, after this fix) can produce one.
    stray = CompanyResearchRecord(
        identity_key="name:acme gmbh",
        normalized_company_name="acme gmbh",
        normalized_domain=None,
        company_name="Acme GmbH",
        provider_name="job_data",
        research_status="PARTIAL",
        version=1,
    )
    db.add(stray)
    db.commit()

    resolved = resolve_name_only_company_research(db, "acme gmbh")

    assert resolved is not None
    assert resolved.id == known.id
    assert resolved.short_summary == "known-domain"


def test_record_failed_attempt_attaches_to_sole_known_domain_not_a_duplicate():
    """FR-M-03: a failed refresh attempt against an already-resolved sole
    known-domain record must attach its attempt metadata to that record
    (via resolve_name_only_company_research), not create a stray
    domain-less "name:<x>" duplicate.
    """
    db = _db()
    known, _ = upsert_company_research(
        db,
        _data(short_summary="good content"),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )

    updated = record_failed_attempt(
        db,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        company_name="Acme GmbH",
        provider_name="job_data",
        error_message="transient",
    )

    assert updated.id == known.id
    assert updated.identity_key == "domain:acme.de"
    assert updated.short_summary == "good content"
    assert updated.last_attempt_status == "FAILED"
    assert updated.last_error == "transient"

    total = db.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    assert get_company_research_by_identity(db, None, "acme gmbh") is None


# --- M-01: DB-level unique identity_key + concurrency -----------------------


def test_identity_key_is_unique_at_the_db_level():
    db = _db()
    record = CompanyResearchRecord(
        identity_key="name:acme gmbh",
        normalized_company_name="acme gmbh",
        normalized_domain=None,
        company_name="Acme GmbH",
        provider_name="job_data",
        research_status="PARTIAL",
        version=1,
    )
    db.add(record)
    db.commit()

    duplicate = CompanyResearchRecord(
        identity_key="name:acme gmbh",
        normalized_company_name="acme gmbh",
        normalized_domain=None,
        company_name="Acme GmbH Duplicate",
        provider_name="job_data",
        research_status="PARTIAL",
        version=1,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()


def test_concurrent_domain_identity_create_deduplicates(tmp_path, monkeypatch):
    factory = _file_session_factory(tmp_path, "concurrent_domain.db")
    db1 = factory()
    db2 = factory()

    record1, outcome1 = upsert_company_research(
        db1,
        _data(short_summary="winner"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )
    assert outcome1 == CREATED

    # Simulate db2 racing in with a stale read that missed db1's already-
    # committed row (e.g. its own existence check ran before db1's commit
    # became visible): its INSERT attempt must collide on the identity_key
    # UNIQUE constraint, and upsert_company_research must recover by
    # reloading the canonical (winning) row rather than raising or
    # duplicating.
    monkeypatch.setattr(
        "app.db.repositories.get_company_research_by_identity", lambda *a, **k: None
    )
    record2, outcome2 = upsert_company_research(
        db2,
        _data(short_summary="loser"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )

    assert outcome2 == SUPERSEDED
    assert record2.id == record1.id
    assert record2.short_summary == "winner"

    total = db1.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    db1.close()
    db2.close()


def test_concurrent_name_identity_create_deduplicates(tmp_path, monkeypatch):
    factory = _file_session_factory(tmp_path, "concurrent_name.db")
    db1 = factory()
    db2 = factory()

    record1, outcome1 = upsert_company_research(
        db1,
        _data(short_summary="winner"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    assert outcome1 == CREATED

    monkeypatch.setattr(
        "app.db.repositories.get_company_research_by_identity", lambda *a, **k: None
    )
    record2, outcome2 = upsert_company_research(
        db2,
        _data(short_summary="loser"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )

    assert outcome2 == SUPERSEDED
    assert record2.id == record1.id
    assert record2.short_summary == "winner"

    total = db1.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    db1.close()
    db2.close()


# --- RR-M-01: mixed name/domain creation race (Codex second re-review) -----


def _create_concurrently(db, *, normalized_domain, normalized_company_name, short_summary):
    """Call the repository's real internal create step directly, on an
    independent Session, exactly at the point two real concurrent callers
    would reach it after each had already determined (via its own, real,
    unmonkeypatched get_company_research_by_identity call) that no record
    exists yet for its identity. Two sequential Python calls into
    upsert_company_research can't otherwise land two real SQLite sessions
    at that same instant — the first call's own commit would already be
    visible to the second call's own lookup. This drives the exact
    interleaving the RR-M-01 race requires without monkeypatching the
    lookup function itself, using only real Sessions against the real
    (temp-file-backed) database.
    """
    from app.db import repositories

    identity_key = repositories._identity_key(normalized_domain, normalized_company_name)
    return repositories._create_company_research(
        db,
        _data(short_summary=short_summary),
        normalized_domain=normalized_domain,
        normalized_company_name=normalized_company_name,
        identity_key=identity_key,
    )


def test_concurrent_mixed_name_and_domain_create_converges_on_domain_identity(tmp_path):
    """Genuine race: nothing has been committed yet, so both sessions'
    get_company_research_by_identity lookups (verified explicitly below)
    naturally return None. Session A resolves "name:acme gmbh" (no domain),
    session B resolves "domain:acme.com" for the same normalized name — two
    different identity_key strings, so the identity_key UNIQUE constraint
    alone can't catch the collision (that's the RR-M-01 bug). The
    CompanyResearchIdentityAlias coordination must still converge both
    callers onto exactly one row, with the domain-bearing identity winning.
    """
    factory = _file_session_factory(tmp_path, "mixed_race.db")
    db_a = factory()
    db_b = factory()

    assert get_company_research_by_identity(db_a, None, "acme gmbh") is None
    assert get_company_research_by_identity(db_b, "acme.com", "acme gmbh") is None

    record_a, outcome_a = _create_concurrently(
        db_a, normalized_domain=None, normalized_company_name="acme gmbh", short_summary="from-A"
    )
    record_b, outcome_b = _create_concurrently(
        db_b,
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
        short_summary="from-B",
    )

    assert outcome_a == CREATED
    assert outcome_b == UPDATED
    assert record_a.id == record_b.id
    assert record_b.identity_key == "domain:acme.com"
    assert record_b.normalized_domain == "acme.com"

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    canonical = get_company_research_by_identity(final, "acme.com", "acme gmbh")
    assert canonical.identity_key == "domain:acme.com"
    db_a.close()
    db_b.close()
    final.close()


def test_concurrent_mixed_name_and_domain_create_converges_regardless_of_order(tmp_path):
    """Same race as above, opposite commit order: the domain-bearing
    creator commits (and claims the alias) first, and the name-only creator
    loses the alias race second. Final canonical identity must still be the
    domain identity either way (order-independent outcome).
    """
    factory = _file_session_factory(tmp_path, "mixed_race_reverse.db")
    db_domain = factory()
    db_name = factory()

    assert get_company_research_by_identity(db_domain, "acme.com", "acme gmbh") is None
    assert get_company_research_by_identity(db_name, None, "acme gmbh") is None

    record_domain, outcome_domain = _create_concurrently(
        db_domain,
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
        short_summary="from-domain",
    )
    record_name, outcome_name = _create_concurrently(
        db_name,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        short_summary="from-name",
    )

    assert outcome_domain == CREATED
    assert outcome_name == SUPERSEDED
    assert record_domain.id == record_name.id
    assert record_name.identity_key == "domain:acme.com"

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    db_domain.close()
    db_name.close()
    final.close()


def test_concurrent_promotion_of_existing_domainless_record_does_not_duplicate(tmp_path):
    """A domainless record already exists (sequential, pre-race state).
    Two sessions concurrently try to promote it to the *same* domain — this
    must resolve via the existing version-checked UPDATE path (whoever
    commits first wins; the other is reloaded as SUPERSEDED), never via a
    second INSERT, so no duplicate/orphan row is left behind.
    """
    factory = _file_session_factory(tmp_path, "promote_race.db")
    seed_db = factory()
    seeded, seed_outcome = upsert_company_research(
        seed_db,
        _data(short_summary="domainless"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    assert seed_outcome == CREATED
    seed_db.close()

    reader_a = factory()
    reader_b = factory()
    existing_a = get_company_research_by_identity(reader_a, None, "acme gmbh")
    existing_b = get_company_research_by_identity(reader_b, None, "acme gmbh")
    assert existing_a.version == existing_b.version == 1

    record_b, outcome_b = upsert_company_research(
        reader_b,
        _data(short_summary="promoted-by-B"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
        expected_version=existing_b.version,
    )
    assert outcome_b == UPDATED
    assert record_b.identity_key == "domain:acme.com"

    record_a, outcome_a = upsert_company_research(
        reader_a,
        _data(short_summary="promoted-by-A"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
        expected_version=existing_a.version,
    )
    assert outcome_a == SUPERSEDED
    assert record_a.id == record_b.id
    assert record_a.short_summary == "promoted-by-B"

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    reader_a.close()
    reader_b.close()
    final.close()


# --- FR-H-01: concurrent promotion to different known domains -------------


def _promote_with_stale_snapshot(db, existing_snapshot, *, normalized_domain, short_summary):
    """Call the repository's real internal update step directly, passing a
    deliberately stale `existing` object captured *before* a concurrent
    writer committed — exactly reproducing Codex's exact repro (a versioned
    UPDATE whose WHERE id=... AND version=<stale> matches zero rows, i.e.
    rowcount==0) without needing real threads. upsert_company_research's
    own top-level entry always re-reads `existing` fresh at call time (see
    its docstring), so a plain sequential call can't land this specific
    sub-statement race — this mirrors the RR-M-01 tests'
    _create_concurrently helper for the same reason.
    """
    from app.db import repositories

    identity_key = repositories._identity_key(normalized_domain, "acme gmbh")
    return repositories._update_existing_company_research(
        db,
        existing_snapshot,
        _data(short_summary=short_summary),
        normalized_domain=normalized_domain,
        normalized_company_name="acme gmbh",
        identity_key=identity_key,
        expected_version=existing_snapshot.version,
    )


def test_concurrent_promotion_to_different_known_domains_preserves_both_payloads(tmp_path):
    """FR-H-01: two sessions read the same domainless v1 record and try to
    promote it to two *different* known domains. A commits first (acme.com,
    version 2). B's versioned UPDATE (still holding the stale version-1
    snapshot, promoting to acme.de) then matches zero rows — the old code
    just reloaded A's row and reported B's payload as SUPERSEDED, silently
    losing acme.de's identity/content entirely. Per the H-01 hard identity
    rule (two different known non-null domains are two different
    companies), B's payload must survive as its own standalone record
    instead.
    """
    factory = _file_session_factory(tmp_path, "diverge_race.db")
    seed_db = factory()
    seeded, seed_outcome = upsert_company_research(
        seed_db,
        _data(short_summary="domainless"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    assert seed_outcome == CREATED
    seed_db.close()

    reader_a = factory()
    reader_b = factory()
    existing_a = get_company_research_by_identity(reader_a, None, "acme gmbh")
    existing_b = get_company_research_by_identity(reader_b, None, "acme gmbh")
    assert existing_a.version == existing_b.version == 1

    record_a, outcome_a = _promote_with_stale_snapshot(
        reader_a, existing_a, normalized_domain="acme.com", short_summary="promoted-to-acme.com"
    )
    assert outcome_a == UPDATED
    assert record_a.identity_key == "domain:acme.com"

    record_b, outcome_b = _promote_with_stale_snapshot(
        reader_b, existing_b, normalized_domain="acme.de", short_summary="promoted-to-acme.de"
    )
    assert outcome_b == CREATED
    assert record_b.identity_key == "domain:acme.de"
    assert record_b.short_summary == "promoted-to-acme.de"
    assert record_b.id != record_a.id
    assert record_b.id == seeded.id or record_a.id == seeded.id  # one of them IS the original row

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 2
    acme_com = get_company_research_by_identity(final, "acme.com", "acme gmbh")
    acme_de = get_company_research_by_identity(final, "acme.de", "acme gmbh")
    assert acme_com is not None and acme_com.id == record_a.id
    assert acme_de is not None and acme_de.id == record_b.id
    assert acme_com.short_summary == "promoted-to-acme.com"
    assert acme_de.short_summary == "promoted-to-acme.de"

    # The alias stays valid, FK-consistent, and pointing at the record that
    # was already canonical for name coordination (the original seeded/
    # promoted row) — never reassigned to the divergent standalone record.
    alias = final.scalar(
        select(CompanyResearchIdentityAlias).where(
            CompanyResearchIdentityAlias.normalized_company_name == "acme gmbh"
        )
    )
    assert alias is not None
    assert alias.company_research_id == seeded.id == record_a.id
    reader_a.close()
    reader_b.close()
    final.close()


def test_concurrent_promotion_to_different_known_domains_reverse_order(tmp_path):
    """Same FR-H-01 race, opposite commit order (acme.de's stale-snapshot
    UPDATE is attempted first and wins, acme.com's loses) — must still end
    with 2 distinct known-domain records and neither payload lost, order
    independent.
    """
    factory = _file_session_factory(tmp_path, "diverge_race_reverse.db")
    seed_db = factory()
    seeded, seed_outcome = upsert_company_research(
        seed_db,
        _data(short_summary="domainless"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    assert seed_outcome == CREATED
    seed_db.close()

    reader_a = factory()
    reader_b = factory()
    existing_a = get_company_research_by_identity(reader_a, None, "acme gmbh")
    existing_b = get_company_research_by_identity(reader_b, None, "acme gmbh")
    assert existing_a.version == existing_b.version == 1

    record_b, outcome_b = _promote_with_stale_snapshot(
        reader_b, existing_b, normalized_domain="acme.de", short_summary="promoted-to-acme.de"
    )
    assert outcome_b == UPDATED
    assert record_b.identity_key == "domain:acme.de"

    record_a, outcome_a = _promote_with_stale_snapshot(
        reader_a, existing_a, normalized_domain="acme.com", short_summary="promoted-to-acme.com"
    )
    assert outcome_a == CREATED
    assert record_a.identity_key == "domain:acme.com"
    assert record_a.id != record_b.id

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 2
    acme_com = get_company_research_by_identity(final, "acme.com", "acme gmbh")
    acme_de = get_company_research_by_identity(final, "acme.de", "acme gmbh")
    assert acme_com is not None and acme_com.id == record_a.id
    assert acme_de is not None and acme_de.id == record_b.id
    reader_a.close()
    reader_b.close()
    final.close()


def test_concurrent_create_with_different_known_domains_stays_two_records(tmp_path):
    """Case C, genuinely concurrent: two sessions, no prior committed row,
    each resolving a *different already-known* domain for the same display
    name. This must never merge — RR-M-01's alias coordination is only a
    dedup mechanism for the "same company, domain not yet known by one
    side" case, never a merge trigger across two distinct known domains.
    """
    factory = _file_session_factory(tmp_path, "case_c_race.db")
    db_a = factory()
    db_b = factory()

    record_a, outcome_a = upsert_company_research(
        db_a,
        _data(short_summary="acme-de"),
        normalized_domain="acme.de",
        normalized_company_name="acme gmbh",
    )
    record_b, outcome_b = upsert_company_research(
        db_b,
        _data(short_summary="acme-com"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )

    assert outcome_a == CREATED
    assert outcome_b == CREATED
    assert record_a.id != record_b.id
    assert record_a.normalized_domain == "acme.de"
    assert record_b.normalized_domain == "acme.com"

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 2
    # Only the first committer's name claims the coordination alias — this
    # is expected (an implementation detail of how the race was resolved,
    # not a merge), and does not affect either record's own identity.
    alias_count = final.scalar(select(func.count()).select_from(CompanyResearchIdentityAlias))
    assert alias_count == 1
    db_a.close()
    db_b.close()
    final.close()


def test_concurrent_create_after_alias_conflict_raises_on_missing_canonical(tmp_path, monkeypatch):
    """Defensive check (Codex re-review section 5): if the alias-conflict
    resolver ever finds an alias row whose target company_research row is
    missing — an invariant violation that should be unreachable since no
    code path deletes either table — it must raise a controlled
    repository-level error rather than silently return None and let a
    caller crash later with an unrelated AttributeError.
    """
    from app.db import repositories

    db = _db()
    db.add(
        CompanyResearchIdentityAlias(normalized_company_name="acme gmbh", company_research_id=999)
    )
    db.commit()

    with pytest.raises(repositories.CompanyResearchConsistencyError):
        repositories._join_or_diverge_after_alias_conflict(
            db, _data(), normalized_domain="acme.com", normalized_company_name="acme gmbh"
        )


def test_concurrent_refresh_stale_write_does_not_clobber_newer_result(tmp_path):
    factory = _file_session_factory(tmp_path, "concurrent_refresh.db")
    db1 = factory()

    seed, _ = upsert_company_research(
        db1, _data(short_summary="v1"), normalized_domain=None, normalized_company_name="acme gmbh"
    )
    db1.close()

    # Both A and B read the row at version 1 before either writes back.
    reader_a = factory()
    reader_b = factory()
    existing_a = get_company_research_by_identity(reader_a, None, "acme gmbh")
    existing_b = get_company_research_by_identity(reader_b, None, "acme gmbh")
    assert existing_a.version == existing_b.version == 1

    # B finishes first.
    result_b, _ = upsert_company_research(
        reader_b,
        _data(short_summary="v2-from-B"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        expected_version=existing_b.version,
    )
    assert result_b.short_summary == "v2-from-B"
    assert result_b.version == 2

    # A finishes later, still holding the stale expected_version=1 — its
    # write must be discarded in favor of B's newer result, not clobber it.
    result_a, _ = upsert_company_research(
        reader_a,
        _data(short_summary="v2-from-A"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        expected_version=existing_a.version,
    )
    assert result_a.short_summary == "v2-from-B"
    assert result_a.id == result_b.id

    final = factory()
    canonical = get_company_research_by_identity(final, None, "acme gmbh")
    assert canonical.short_summary == "v2-from-B"
    reader_a.close()
    reader_b.close()
    final.close()


# --- upsert/create behavior --------------------------------------------------


def test_upsert_creates_then_reuses_by_domain():
    db = _db()

    first, outcome_first = upsert_company_research(
        db, _data(), normalized_domain="acme.com", normalized_company_name="acme gmbh"
    )
    second, outcome_second = upsert_company_research(
        db,
        _data(short_summary="updated"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )

    assert outcome_first == CREATED
    assert outcome_second == UPDATED
    assert first.id == second.id
    assert second.short_summary == "updated"
    assert second.last_attempt_status == "SUCCESS"
    assert second.last_error is None


def test_upsert_without_domain_falls_back_to_name_identity():
    db = _db()

    first, outcome_first = upsert_company_research(
        db, _data(), normalized_domain=None, normalized_company_name="acme gmbh"
    )
    second, outcome_second = upsert_company_research(
        db, _data(), normalized_domain=None, normalized_company_name="acme gmbh"
    )

    assert outcome_first == CREATED
    assert outcome_second == UPDATED
    assert first.id == second.id


# --- record_failed_attempt ---------------------------------------------------


def test_failed_attempt_with_no_prior_record_creates_minimal_failed_row():
    db = _db()

    record = record_failed_attempt(
        db,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        company_name="Acme GmbH",
        provider_name="job_data",
        error_message="boom",
    )

    assert record.research_status == "FAILED"
    assert record.researched_at is None
    assert record.last_attempt_status == "FAILED"
    assert record.last_error == "boom"
    assert record.confidence == 0.0


def test_failed_attempt_with_existing_good_record_leaves_content_untouched():
    db = _db()
    good, _ = upsert_company_research(
        db,
        _data(short_summary="good content", confidence=0.5),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    original_researched_at = good.researched_at

    updated = record_failed_attempt(
        db,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        company_name="Acme GmbH",
        provider_name="job_data",
        error_message="transient failure",
    )

    assert updated.id == good.id
    assert updated.short_summary == "good content"
    assert updated.confidence == 0.5
    assert updated.research_status == "PARTIAL"
    assert updated.researched_at == original_researched_at
    assert updated.last_attempt_status == "FAILED"
    assert updated.last_error == "transient failure"


def test_failed_attempt_error_message_is_bounded_in_length():
    db = _db()
    record = record_failed_attempt(
        db,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        company_name="Acme GmbH",
        provider_name="job_data",
        error_message="x" * 1000,
    )
    assert len(record.last_error) <= 500


# --- FR-M-02: FAILED-vs-success write ordering ------------------------------


def test_failed_commits_first_then_concurrent_success_upgrades_it(tmp_path):
    """Scenario A: a FAILED diagnostic row commits first (record_failed_attempt
    never claims an alias). A genuinely concurrent successful create — its
    own existing-lookup, done before the FAILED row committed, also saw
    nothing — then collides on identity_key at flush time. The old code
    treated that collision as an ordinary already-race-safe case and
    reloaded/returned the FAILED row as SUPERSEDED, silently discarding the
    successful payload. It must instead upgrade the FAILED row in place:
    same row id, research_status becomes PARTIAL, researched_at set,
    content persisted, and (FR-M-02 Fix B) an alias claimed for it.
    """
    from app.db import repositories

    factory = _file_session_factory(tmp_path, "failed_then_success.db")
    db_failed = factory()
    db_success = factory()

    assert get_company_research_by_identity(db_failed, None, "acme gmbh") is None
    assert get_company_research_by_identity(db_success, None, "acme gmbh") is None

    failed_record = record_failed_attempt(
        db_failed,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        company_name="Acme GmbH",
        provider_name="job_data",
        error_message="boom",
    )
    assert failed_record.research_status == "FAILED"
    assert failed_record.researched_at is None

    identity_key = repositories._identity_key(None, "acme gmbh")
    success_record, outcome = repositories._create_company_research(
        db_success,
        _data(short_summary="recovered"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        identity_key=identity_key,
    )

    assert outcome == UPDATED
    assert success_record.id == failed_record.id
    assert success_record.research_status == "PARTIAL"
    assert success_record.researched_at is not None
    assert success_record.short_summary == "recovered"
    assert success_record.last_attempt_status == "SUCCESS"

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    canonical = get_company_research_by_identity(final, None, "acme gmbh")
    assert canonical.id == failed_record.id
    assert canonical.research_status == "PARTIAL"

    alias = final.scalar(
        select(CompanyResearchIdentityAlias).where(
            CompanyResearchIdentityAlias.normalized_company_name == "acme gmbh"
        )
    )
    assert alias is not None
    assert alias.company_research_id == canonical.id
    db_failed.close()
    db_success.close()
    final.close()


def test_success_commits_first_then_concurrent_failure_preserves_content(tmp_path):
    """Scenario B (opposite order): a successful create commits first (and
    claims its alias). A failure recorded afterward for the same identity
    must only update attempt metadata (last_attempt_status/last_error) —
    the successful content, research_status, researched_at, and the alias
    must all survive untouched, and no duplicate row is created.
    """
    factory = _file_session_factory(tmp_path, "success_then_failed.db")
    db_success = factory()
    db_failed = factory()

    assert get_company_research_by_identity(db_success, None, "acme gmbh") is None
    assert get_company_research_by_identity(db_failed, None, "acme gmbh") is None

    record, outcome = upsert_company_research(
        db_success,
        _data(short_summary="good content", confidence=0.5),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    assert outcome == CREATED
    db_success.close()

    updated = record_failed_attempt(
        db_failed,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        company_name="Acme GmbH",
        provider_name="job_data",
        error_message="transient",
    )

    assert updated.id == record.id
    assert updated.short_summary == "good content"
    assert updated.research_status == "PARTIAL"
    assert updated.researched_at is not None
    assert updated.last_attempt_status == "FAILED"
    assert updated.last_error == "transient"

    final = factory()
    total = final.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    alias = final.scalar(
        select(CompanyResearchIdentityAlias).where(
            CompanyResearchIdentityAlias.normalized_company_name == "acme gmbh"
        )
    )
    assert alias is not None
    assert alias.company_research_id == record.id
    db_failed.close()
    final.close()


def test_failed_then_success_then_domain_promotion_then_name_only_stays_one_record(tmp_path):
    """FR-M-02 section 4 regression: the full defect chain Codex found.

    Step 1: research fails -> FAILED name-only row (no alias claimed).
    Step 2: retry succeeds -> same row becomes PARTIAL (Fix A upgrades it
            in place, Fix B claims its alias).
    Step 3: a trusted-domain promotion (repository-level; v1's own service
            never does this, see H-02) promotes the same row to a known
            domain in place.
    Step 4: a brand-new name-only create request comes in. Before Fix B,
            the row's alias was never claimed in step 2, so step 4 would
            "rediscover" the name as unclaimed and create a duplicate
            "name:acme gmbh" row alongside the promoted domain row.

    Expected: exactly one company_research row throughout, one valid alias
    resolving consistently to it.
    """
    db = _db()

    # Step 1
    failed = record_failed_attempt(
        db,
        normalized_domain=None,
        normalized_company_name="acme gmbh",
        company_name="Acme GmbH",
        provider_name="job_data",
        error_message="boom",
    )
    assert failed.research_status == "FAILED"

    # Step 2
    recovered, outcome_2 = upsert_company_research(
        db,
        _data(short_summary="recovered"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    assert outcome_2 == UPDATED
    assert recovered.id == failed.id
    assert recovered.research_status == "PARTIAL"

    # Step 3
    promoted, outcome_3 = upsert_company_research(
        db,
        _data(short_summary="promoted"),
        normalized_domain="acme.com",
        normalized_company_name="acme gmbh",
    )
    assert outcome_3 == UPDATED
    assert promoted.id == failed.id
    assert promoted.identity_key == "domain:acme.com"

    # Step 4
    later, outcome_4 = upsert_company_research(
        db,
        _data(short_summary="later-name-only-request"),
        normalized_domain=None,
        normalized_company_name="acme gmbh",
    )
    assert outcome_4 == SUPERSEDED
    assert later.id == promoted.id
    assert later.identity_key == "domain:acme.com"

    total = db.scalar(select(func.count()).select_from(CompanyResearchRecord))
    assert total == 1
    alias = db.scalar(
        select(CompanyResearchIdentityAlias).where(
            CompanyResearchIdentityAlias.normalized_company_name == "acme gmbh"
        )
    )
    assert alias is not None
    assert alias.company_research_id == promoted.id
