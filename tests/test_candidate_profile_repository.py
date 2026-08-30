import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.candidate_profile_repository import (
    CandidateProfileVersionConflictError,
    apply_candidate_profile_patch,
    count_candidate_profiles,
    get_or_create_candidate_profile,
    to_candidate_profile_response,
)
from app.db.models import CandidateProfileRecord, CandidateSkillRecord
from app.models.candidate_profile import (
    TRUSTED_GENERATION_SOURCES,
    CandidateCertification,
    CandidateEducation,
    CandidateExperience,
    CandidateJobPreferences,
    CandidateLanguage,
    CandidateProfilePatchRequest,
    CandidateProject,
    CandidateSkill,
    FieldTrust,
    SourceType,
    is_top_level_fact_usable_for_generation,
    is_usable_for_generation,
    normalize_text_identity,
)


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return Session(engine)


def _file_session_factory(tmp_path, name: str):
    db_path = tmp_path / name
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


# --- singleton behavior -------------------------------------------------


def test_get_or_create_candidate_profile_creates_empty_profile():
    db = _db()
    profile = get_or_create_candidate_profile(db)

    assert profile.id == 1
    assert profile.profile_version == 1
    assert profile.first_name is None
    assert profile.skills == []
    assert count_candidate_profiles(db) == 1


def test_get_or_create_candidate_profile_is_idempotent():
    db = _db()
    first = get_or_create_candidate_profile(db)
    second = get_or_create_candidate_profile(db)

    assert first.id == second.id
    assert count_candidate_profiles(db) == 1


def test_singleton_check_constraint_rejects_a_second_row():
    """DB-level enforcement (section 20), not just application convention."""
    db = _db()
    get_or_create_candidate_profile(db)

    duplicate = CandidateProfileRecord(id=2, profile_version=1)
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()


def test_concurrent_first_access_deduplicates(tmp_path):
    """Two independent sessions racing to create the singleton for the
    first time must converge on exactly one row — real independent
    Sessions, no monkeypatched lookup.
    """
    factory = _file_session_factory(tmp_path, "concurrent_singleton.db")
    db_a = factory()
    db_b = factory()

    assert db_a.get(CandidateProfileRecord, 1) is None
    assert db_b.get(CandidateProfileRecord, 1) is None

    profile_a = get_or_create_candidate_profile(db_a)
    profile_b = get_or_create_candidate_profile(db_b)

    assert profile_a.id == profile_b.id == 1

    final = factory()
    assert count_candidate_profiles(final) == 1
    db_a.close()
    db_b.close()
    final.close()


# --- versioning -----------------------------------------------------------


def test_version_increments_on_scalar_patch():
    db = _db()
    get_or_create_candidate_profile(db)

    patch = CandidateProfilePatchRequest(
        expected_profile_version=1, professional_title="Junior Python Developer"
    )
    updated = apply_candidate_profile_patch(db, patch)

    assert updated.profile_version == 2
    assert updated.professional_title == "Junior Python Developer"


def test_version_increments_again_on_second_patch():
    db = _db()
    apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=1, first_name="Anna")
    )
    updated = apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=2, last_name="Muster")
    )

    assert updated.profile_version == 3
    assert updated.first_name == "Anna"
    assert updated.last_name == "Muster"


def test_empty_patch_with_correct_version_does_not_bump_version():
    db = _db()
    get_or_create_candidate_profile(db)
    before = get_or_create_candidate_profile(db)
    updated = apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=1)
    )

    assert updated.profile_version == 1
    assert updated.updated_at == before.updated_at
    assert updated.first_name is None
    assert updated.skills == []
    assert updated.field_trust_json == "{}"


def test_empty_patch_with_stale_version_raises_conflict():
    """Section 16: consistency over silently treating a stale expected
    version on an empty PATCH as a harmless no-op.
    """
    db = _db()
    apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=1, first_name="Anna")
    )

    with pytest.raises(CandidateProfileVersionConflictError) as excinfo:
        apply_candidate_profile_patch(db, CandidateProfilePatchRequest(expected_profile_version=1))
    assert excinfo.value.current_version == 2


def test_empty_patch_stale_conflict_then_retry_recovers_same_session():
    """M-01 section 11: after a stale empty-PATCH conflict, the same
    Session must remain usable and a retry with the fresh version must
    succeed — the rollback inside the conflict path must not leave the
    Session broken.
    """
    db = _db()
    apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=1, first_name="Anna")
    )

    with pytest.raises(CandidateProfileVersionConflictError) as excinfo:
        apply_candidate_profile_patch(db, CandidateProfilePatchRequest(expected_profile_version=1))
    assert excinfo.value.current_version == 2

    retried = apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=2)
    )
    assert retried.profile_version == 2
    assert retried.first_name == "Anna"


def test_empty_patch_version_check_is_db_authoritative_not_stale_object(tmp_path, monkeypatch):
    """M-01: the empty-PATCH version check must be validated atomically
    against the database at the moment of the check, not against a Python
    object loaded earlier. This reproduces the exact race the old
    Python-only comparison (`profile.profile_version != expected_version`)
    got wrong:

        A loads its profile object (version 1, stale from this point on)
        B commits a real change -> version 2
        A performs its version check

    Session A's initial load is synchronized (via a monkeypatched
    get_or_create_candidate_profile) to happen *before* session B commits,
    proving the eventual conflict cannot be explained by A simply reading
    fresh data — a stale-object comparison would have returned 200 here.
    """
    import threading

    import app.db.candidate_profile_repository as repo_module

    factory = _file_session_factory(tmp_path, "empty_patch_race.db")
    seed_db = factory()
    get_or_create_candidate_profile(seed_db)
    seed_db.close()

    db_a = factory()
    db_b = factory()

    a_loaded = threading.Event()
    b_committed = threading.Event()
    original_get_or_create = repo_module.get_or_create_candidate_profile

    def _synced_get_or_create(db):
        profile = original_get_or_create(db)
        if db is db_a:
            a_loaded.set()
            assert b_committed.wait(timeout=5), "session B never committed in time"
        return profile

    monkeypatch.setattr(repo_module, "get_or_create_candidate_profile", _synced_get_or_create)

    outcome: dict[str, object] = {}

    def _run_a():
        try:
            apply_candidate_profile_patch(
                db_a, CandidateProfilePatchRequest(expected_profile_version=1)
            )
            outcome["result"] = "success"
        except CandidateProfileVersionConflictError as exc:
            outcome["result"] = "conflict"
            outcome["current_version"] = exc.current_version

    thread_a = threading.Thread(target=_run_a)
    thread_a.start()
    assert a_loaded.wait(timeout=5), "session A never reached its version check"

    apply_candidate_profile_patch(
        db_b, CandidateProfilePatchRequest(expected_profile_version=1, first_name="Bea")
    )
    b_committed.set()
    thread_a.join(timeout=5)

    assert outcome["result"] == "conflict"
    assert outcome["current_version"] == 2

    final = factory()
    canonical = get_or_create_candidate_profile(final)
    assert canonical.profile_version == 2
    assert canonical.first_name == "Bea"
    db_a.close()
    db_b.close()
    final.close()


# --- PATCH partial-update safety -------------------------------------------


def test_patch_does_not_erase_unrelated_fields():
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            skills=[CandidateSkill(name="Python")],
            education=[CandidateEducation(institution="TU Berlin")],
        ),
    )

    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=2, professional_title="Junior Python Developer"
        ),
    )

    assert updated.professional_title == "Junior Python Developer"
    assert [s.name for s in updated.skills] == ["Python"]
    assert [e.institution for e in updated.education] == ["TU Berlin"]


def test_patch_replaces_skills_list_wholesale():
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            skills=[CandidateSkill(name="Python"), CandidateSkill(name="SQL")],
        ),
    )
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=2, skills=[CandidateSkill(name="Docker")]
        ),
    )

    assert [s.name for s in updated.skills] == ["Docker"]


def test_patch_replacing_overlapping_skill_does_not_violate_unique_constraint():
    """Regression: replacing [Python, SQL] with [Python, Docker] must not
    trip UNIQUE(candidate_profile_id, normalized_name) from old/new rows
    momentarily coexisting mid-flush.
    """
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            skills=[CandidateSkill(name="Python"), CandidateSkill(name="SQL")],
        ),
    )
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=2,
            skills=[CandidateSkill(name="Python"), CandidateSkill(name="Docker")],
        ),
    )

    assert sorted(s.name for s in updated.skills) == ["Docker", "Python"]
    total_skill_rows = db.scalar(select(func.count()).select_from(CandidateSkillRecord))
    assert total_skill_rows == 2


def test_patch_job_preferences_replaces_wholesale_without_touching_skills():
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1, skills=[CandidateSkill(name="Python")]
        ),
    )
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=2,
            job_preferences=CandidateJobPreferences(minimum_salary=45000, salary_currency="EUR"),
        ),
    )

    assert updated.job_preferences.minimum_salary == 45000
    assert [s.name for s in updated.skills] == ["Python"]

    updated2 = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=3,
            job_preferences=CandidateJobPreferences(minimum_salary=55000, salary_currency="EUR"),
        ),
    )
    assert updated2.job_preferences.minimum_salary == 55000
    assert [s.name for s in updated2.skills] == ["Python"]


# --- normalization / dedup -------------------------------------------------


def test_normalize_text_identity_strips_casefolds_collapses_whitespace():
    assert normalize_text_identity("  Python   3  ") == "python 3"
    assert normalize_text_identity("PostgreSQL") == normalize_text_identity("postgresql")


def test_skill_normalized_name_persisted():
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1, skills=[CandidateSkill(name="  Python  ")]
        ),
    )
    record = db.scalar(select(CandidateSkillRecord))
    assert record.name == "Python"
    assert record.normalized_name == "python"


# --- nested entity persistence ---------------------------------------------


def test_incomplete_education_preserved():
    db = _db()
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            education=[CandidateEducation(institution="TU Berlin", completed=False, end_date=None)],
        ),
    )
    edu = updated.education[0]
    assert edu.completed is False
    assert edu.end_date is None


def test_experience_technologies_not_auto_populated_from_skills():
    """A technology in `skills` must never leak into an experience entry
    unless explicitly listed there too (section 6).
    """
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            skills=[CandidateSkill(name="Docker")],
            experiences=[
                CandidateExperience(company="Acme", job_title="Dev", technologies=["Python"])
            ],
        ),
    )
    updated = to_candidate_profile_response(record)
    assert updated.experiences[0].technologies == ["Python"]
    assert "Docker" not in updated.experiences[0].technologies


def test_project_persistence_round_trip():
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            projects=[
                CandidateProject(
                    name="Job Triage",
                    technologies=["Python", "FastAPI"],
                    repository_url="https://github.com/example/job-triage",
                    highlights=["Built a scoring engine"],
                )
            ],
        ),
    )
    updated = to_candidate_profile_response(record)
    project = updated.projects[0]
    assert project.name == "Job Triage"
    assert project.technologies == ["Python", "FastAPI"]
    assert project.repository_url == "https://github.com/example/job-triage"
    assert project.highlights == ["Built a scoring engine"]


def test_language_persistence_round_trip():
    db = _db()
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            languages=[CandidateLanguage(language="German", level="B2", certificate="telc B2")],
        ),
    )
    lang = updated.languages[0]
    assert lang.language == "German"
    assert lang.level == "B2"
    assert lang.certificate == "telc B2"


def test_certification_persistence_round_trip():
    db = _db()
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            certifications=[
                CandidateCertification(name="AWS Cloud Practitioner", status="COMPLETED")
            ],
        ),
    )
    cert = updated.certifications[0]
    assert cert.name == "AWS Cloud Practitioner"
    assert cert.status == "COMPLETED"


# --- provenance (nested entities) -------------------------------------------


def test_provenance_defaults_to_manual_entry_confirmed():
    db = _db()
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1, skills=[CandidateSkill(name="Python")]
        ),
    )
    skill = updated.skills[0]
    assert skill.source == "MANUAL_ENTRY"
    assert skill.confidence == "CONFIRMED"


def test_provenance_explicit_inferred_is_preserved_not_upgraded():
    db = _db()
    updated = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            skills=[CandidateSkill(name="Rust", source="INFERRED", confidence="UNCONFIRMED")],
        ),
    )
    skill = updated.skills[0]
    assert skill.source == "INFERRED"
    assert skill.confidence == "UNCONFIRMED"


# --- response conversion -----------------------------------------------


def test_to_candidate_profile_response_full_round_trip():
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            first_name="Anna",
            target_roles=["Junior Python Developer"],
            skills=[CandidateSkill(name="Python")],
        ),
    )
    profile = get_or_create_candidate_profile(db)
    response = to_candidate_profile_response(profile)

    assert response.first_name == "Anna"
    assert response.target_roles == ["Junior Python Developer"]
    assert response.skills[0].name == "Python"
    assert response.job_preferences.remote_preference == "UNKNOWN"


# --- CP-M-01: trusted-source + confirmed-state generation gate -------------


@pytest.mark.parametrize(
    ("source", "confidence", "expected"),
    [
        ("USER_CONFIRMED", "CONFIRMED", True),
        ("USER_PROVIDED_DOCUMENT", "CONFIRMED", True),
        ("MANUAL_ENTRY", "CONFIRMED", True),
        ("IMPORTED", "CONFIRMED", False),
        ("INFERRED", "CONFIRMED", False),
        ("UNKNOWN", "CONFIRMED", False),
    ],
)
def test_is_usable_for_generation_source_matrix(
    source: SourceType, confidence, expected: bool
) -> None:
    assert is_usable_for_generation(source, confidence) is expected


@pytest.mark.parametrize("source", sorted(TRUSTED_GENERATION_SOURCES))
@pytest.mark.parametrize("confidence", ["UNCONFIRMED", "INFERRED", "UNKNOWN"])
def test_is_usable_for_generation_trusted_source_still_requires_confirmed(
    source: SourceType, confidence
) -> None:
    """No source, however trusted, may bypass the confirmed-state half of
    the rule (CP-M-01)."""
    assert is_usable_for_generation(source, confidence) is False


# --- CP-M-02: top-level field provenance ------------------------------------


def test_top_level_field_defaults_to_manual_entry_confirmed():
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1, professional_title="Junior Python Developer"
        ),
    )
    profile = to_candidate_profile_response(record)

    trust = profile.field_trust["professional_title"]
    assert trust.source == "MANUAL_ENTRY"
    assert trust.confidence == "CONFIRMED"
    assert is_top_level_fact_usable_for_generation(profile, "professional_title") is True


def test_top_level_field_trust_round_trip_confirmed():
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            professional_title="Junior Python Developer",
            field_trust={
                "professional_title": FieldTrust(source="MANUAL_ENTRY", confidence="CONFIRMED")
            },
        ),
    )
    profile = to_candidate_profile_response(record)
    assert is_top_level_fact_usable_for_generation(profile, "professional_title") is True


def test_top_level_field_trust_round_trip_inferred_persists_but_not_usable():
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            professional_summary="Presumably knows backend development.",
            field_trust={
                "professional_summary": FieldTrust(source="INFERRED", confidence="CONFIRMED")
            },
        ),
    )
    profile = to_candidate_profile_response(record)
    assert profile.professional_summary == "Presumably knows backend development."
    assert profile.field_trust["professional_summary"].source == "INFERRED"
    assert profile.field_trust["professional_summary"].confidence == "CONFIRMED"
    assert is_top_level_fact_usable_for_generation(profile, "professional_summary") is False


def test_top_level_field_trust_round_trip_unconfirmed_persists_but_not_usable():
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            career_goal="Become a backend engineer.",
            field_trust={
                "career_goal": FieldTrust(source="USER_PROVIDED_DOCUMENT", confidence="UNCONFIRMED")
            },
        ),
    )
    profile = to_candidate_profile_response(record)
    assert profile.career_goal == "Become a backend engineer."
    assert profile.field_trust["career_goal"].confidence == "UNCONFIRMED"
    assert is_top_level_fact_usable_for_generation(profile, "career_goal") is False


def test_top_level_field_trust_round_trip_unknown_not_usable():
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            location_city="Berlin",
            field_trust={"location_city": FieldTrust(source="UNKNOWN", confidence="UNKNOWN")},
        ),
    )
    profile = to_candidate_profile_response(record)
    assert profile.location_city == "Berlin"
    assert is_top_level_fact_usable_for_generation(profile, "location_city") is False


def test_top_level_field_with_no_trust_entry_is_not_usable():
    """A field that has never been set via PATCH has no trust entry at
    all — absence of provenance is never treated as trusted (CP-M-02).
    """
    db = _db()
    record = get_or_create_candidate_profile(db)
    profile = to_candidate_profile_response(record)
    assert "location_city" not in profile.field_trust
    assert is_top_level_fact_usable_for_generation(profile, "location_city") is False


def test_field_trust_not_silently_rewritten_by_unrelated_patch():
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1,
            professional_summary="Inferred summary.",
            field_trust={
                "professional_summary": FieldTrust(source="INFERRED", confidence="UNCONFIRMED")
            },
        ),
    )
    record = apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=2, first_name="Anna")
    )
    profile = to_candidate_profile_response(record)

    # professional_summary's own trust must survive completely untouched.
    assert profile.field_trust["professional_summary"].source == "INFERRED"
    assert profile.field_trust["professional_summary"].confidence == "UNCONFIRMED"
    # first_name gets its own default trust entry.
    assert profile.field_trust["first_name"].source == "MANUAL_ENTRY"


def test_target_roles_has_field_trust_too():
    db = _db()
    record = apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=1, target_roles=["Junior Python Developer"]
        ),
    )
    profile = to_candidate_profile_response(record)
    assert "target_roles" in profile.field_trust
    assert is_top_level_fact_usable_for_generation(profile, "target_roles") is True


# --- CP-M-03: optimistic concurrency (real independent Sessions) -----------


def _promote_version_1(db) -> None:
    apply_candidate_profile_patch(
        db, CandidateProfilePatchRequest(expected_profile_version=1, first_name="Seed")
    )


def test_concurrent_scalar_patches_different_fields_one_wins_one_conflicts(tmp_path):
    """Section 18/19: two real independent sessions both hold
    expected_profile_version=1 (from an earlier shared GET). Exactly one
    commits (version 2); the other gets a 409-equivalent conflict, never a
    silent lost update.
    """
    factory = _file_session_factory(tmp_path, "concurrent_scalar.db")
    seed_db = factory()
    get_or_create_candidate_profile(seed_db)
    seed_db.close()

    db_a = factory()
    db_b = factory()
    existing_a = get_or_create_candidate_profile(db_a)
    existing_b = get_or_create_candidate_profile(db_b)
    assert existing_a.profile_version == existing_b.profile_version == 1

    result_a = apply_candidate_profile_patch(
        db_a,
        CandidateProfilePatchRequest(
            expected_profile_version=1, professional_title="Python Developer"
        ),
    )
    assert result_a.profile_version == 2

    with pytest.raises(CandidateProfileVersionConflictError) as excinfo:
        apply_candidate_profile_patch(
            db_b,
            CandidateProfilePatchRequest(expected_profile_version=1, location_city="Frankfurt"),
        )
    assert excinfo.value.current_version == 2

    # B reloads and retries with the fresh version — must succeed and both
    # intended changes must be present in the final DB state.
    result_b = apply_candidate_profile_patch(
        db_b,
        CandidateProfilePatchRequest(expected_profile_version=2, location_city="Frankfurt"),
    )
    assert result_b.profile_version == 3

    final = factory()
    canonical = get_or_create_candidate_profile(final)
    assert canonical.professional_title == "Python Developer"
    assert canonical.location_city == "Frankfurt"
    assert canonical.profile_version == 3
    db_a.close()
    db_b.close()
    final.close()


def test_concurrent_same_field_patch_one_wins_one_conflicts(tmp_path):
    """Section 19: same field, both racing from version 1 — no silent lost
    update; exactly one value wins.
    """
    factory = _file_session_factory(tmp_path, "concurrent_same_field.db")
    seed_db = factory()
    get_or_create_candidate_profile(seed_db)
    seed_db.close()

    db_a = factory()
    db_b = factory()
    get_or_create_candidate_profile(db_a)
    get_or_create_candidate_profile(db_b)

    result_a = apply_candidate_profile_patch(
        db_a, CandidateProfilePatchRequest(expected_profile_version=1, professional_title="A")
    )
    assert result_a.profile_version == 2
    assert result_a.professional_title == "A"

    with pytest.raises(CandidateProfileVersionConflictError):
        apply_candidate_profile_patch(
            db_b, CandidateProfilePatchRequest(expected_profile_version=1, professional_title="B")
        )

    final = factory()
    canonical = get_or_create_candidate_profile(final)
    assert canonical.professional_title == "A"
    assert canonical.profile_version == 2
    db_a.close()
    db_b.close()
    final.close()


def test_concurrent_collection_patch_stale_writer_never_overwrites(tmp_path):
    """Section 20: two stale PATCHes both replacing `skills` from version
    1 — the loser's collection replacement must never apply; no partial
    or overwritten rows.
    """
    factory = _file_session_factory(tmp_path, "concurrent_collection.db")
    seed_db = factory()
    apply_candidate_profile_patch(
        seed_db,
        CandidateProfilePatchRequest(
            expected_profile_version=1, skills=[CandidateSkill(name="Python")]
        ),
    )
    seed_db.close()

    db_a = factory()
    db_b = factory()
    existing_a = get_or_create_candidate_profile(db_a)
    existing_b = get_or_create_candidate_profile(db_b)
    assert existing_a.profile_version == existing_b.profile_version == 2

    result_a = apply_candidate_profile_patch(
        db_a,
        CandidateProfilePatchRequest(
            expected_profile_version=2,
            skills=[CandidateSkill(name="Python"), CandidateSkill(name="Flask")],
        ),
    )
    assert sorted(s.name for s in result_a.skills) == ["Flask", "Python"]

    with pytest.raises(CandidateProfileVersionConflictError):
        apply_candidate_profile_patch(
            db_b,
            CandidateProfilePatchRequest(
                expected_profile_version=2,
                skills=[CandidateSkill(name="Python"), CandidateSkill(name="SQL")],
            ),
        )

    final = factory()
    canonical = get_or_create_candidate_profile(final)
    assert sorted(s.name for s in canonical.skills) == ["Flask", "Python"]
    db_a.close()
    db_b.close()
    final.close()


def test_rollback_after_cas_restores_version_and_collection(tmp_path, monkeypatch):
    """Section 21: force an exception during collection replacement AFTER
    the CAS succeeds but BEFORE commit. The whole transaction must roll
    back — old skills remain, profile_version remains the pre-CAS value,
    no partial rows, and the Session stays usable afterward.
    """
    import app.db.candidate_profile_repository as repo_module

    factory = _file_session_factory(tmp_path, "rollback.db")
    seed_db = factory()
    apply_candidate_profile_patch(
        seed_db,
        CandidateProfilePatchRequest(
            expected_profile_version=1, skills=[CandidateSkill(name="Python")]
        ),
    )
    seed_db.close()

    db = factory()
    existing = get_or_create_candidate_profile(db)
    assert existing.profile_version == 2

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure during collection replacement")

    monkeypatch.setattr(repo_module, "_skill_to_record", _boom)

    with pytest.raises(RuntimeError):
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=2, skills=[CandidateSkill(name="Docker")]
            ),
        )

    # Session must remain usable after the rollback.
    canonical = get_or_create_candidate_profile(db)
    assert canonical.profile_version == 2
    assert [s.name for s in canonical.skills] == ["Python"]

    total_skill_rows = db.scalar(select(func.count()).select_from(CandidateSkillRecord))
    assert total_skill_rows == 1
    db.close()


def test_conflict_error_never_includes_profile_content():
    db = _db()
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(expected_profile_version=1, first_name="VeryUniqueNameXyz123"),
    )
    with pytest.raises(CandidateProfileVersionConflictError) as excinfo:
        apply_candidate_profile_patch(
            db, CandidateProfilePatchRequest(expected_profile_version=1, last_name="Anything")
        )
    assert "VeryUniqueNameXyz123" not in str(excinfo.value)
