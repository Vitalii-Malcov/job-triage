"""Stage 17B: persistent candidate configuration, provenance and audit.

Throwaway SQLite databases only; the contact data below is fictitious.
"""

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.candidate_profile_repository import (
    CandidateProfileTrustDowngradeError,
    CandidateProfileVersionConflictError,
    apply_candidate_profile_patch,
    get_or_create_candidate_profile,
)
from app.db.candidate_search_repository import (
    CandidateConfigRefusedError,
    apply_config_plan,
    load_search_config,
    plan_config_change,
)
from app.db.models import (
    ApplicationEventRecord,
    ApplicationRecord,
    CandidateLanguageRecord,
    CandidateProfileRecord,
    CandidateProfileRevisionImmutableError,
    CandidateProfileRevisionRecord,
    CandidateSkillRecord,
)
from app.models.application_lifecycle import ApplicationChannel as C
from app.models.application_lifecycle import ApplicationEventOrigin as O
from app.models.application_lifecycle import ApplicationLifecycleStatus as S
from app.models.candidate_profile import CandidateProfilePatchRequest
from app.models.candidate_search import CandidateConfigSpec, CandidateSearchSettings
from app.services.application_tracker import ApplicationTracker
from scripts import stage17_candidate_config as cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def spec_dict(**overrides) -> dict:
    data = {
        "schema": "jobtriage.candidate-config/1",
        "source": "USER_CONFIRMED",
        "confirmed_on": "2026-10-27",
        "profile": {"location_city": "Musterstadt", "location_country": "Germany"},
        "contact": {
            "email": "candidate@example.org",
            "phone": "+49 170 0000000",
            "website_url": "https://example.org/portfolio",
        },
        "search": {
            "home_city": "Musterstadt",
            "home_postal_code": "12345",
            "home_region": "Hessen",
            "commute_origin_label": "Musterstadt Bahnhof",
            "commute_origin": {"lat": 50.1, "lon": 8.75},
            "max_commute_km": 100,
            "commute_measure": "CAR_ROUTE",
            "relocation_regions": ["Hessen"],
            "remote_work_scope": "GERMANY",
            "remote_work_from_region": "Hessen",
            "minimum_salary": 45000,
            "maximum_salary": 50000,
            "salary_currency": "EUR",
            "salary_negotiable": True,
            "earliest_start_date": "2027-03-01",
            "primary_roles": ["Junior Python Developer", "Python Backend Developer"],
            "secondary_roles": ["Technical Application Support"],
        },
        "search_sources": {"commute_origin": "IMPORTED"},
        "languages": [
            {"language": "Deutsch", "level": "B1", "certificate": "B1"},
            {"language": "Englisch", "level": "UNKNOWN"},
        ],
        "skills": [
            {"name": "Pydantic", "category": "FRAMEWORK"},
            {"name": "AWS", "category": "CLOUD", "notes": "Learning/project only"},
            {"name": "python", "category": "LANGUAGE"},  # already present: untouched
        ],
    }
    data.update(overrides)
    return data


def spec(**overrides) -> CandidateConfigSpec:
    return CandidateConfigSpec.model_validate(spec_dict(**overrides))


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'config.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    profile = get_or_create_candidate_profile(session)
    profile = apply_candidate_profile_patch(
        session,
        CandidateProfilePatchRequest(
            expected_profile_version=profile.profile_version,
            first_name="Max",
            location_city="Frankfurt am Main",
            location_country="Germany",
            target_roles=["Junior Python Developer"],
            skills=[{"name": "Python", "category": "LANGUAGE", "notes": "kept as is"}],
            languages=[
                {
                    "language": "Deutsch",
                    "level": "B1",
                    "notes": "B2 course ongoing",
                    "source": "USER_PROVIDED_DOCUMENT",
                }
            ],
            job_preferences={"employment_types": ["FULL_TIME"]},
        ),
    )
    yield session
    session.close()
    engine.dispose()


def _apply(db, config=None, **kwargs):
    profile = db.get(CandidateProfileRecord, 1)
    plan = plan_config_change(profile, config or spec())
    return plan, apply_config_plan(
        db, plan, actor="operator", reason="confirmed", now=NOW, **kwargs
    )


# --- spec validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("contact", "phone"), "0176 123"),
        (("contact", "email"), "not-an-address"),
        (("contact", "website_url"), "http://insecure.example.org"),
        (("search", "home_postal_code"), "6315"),
        (("search", "maximum_salary"), 40000),
        (("search", "max_commute_km"), 0),
    ],
)
def test_spec_rejects_invalid_values(path, value):
    data = spec_dict()
    data[path[0]][path[1]] = value
    with pytest.raises(ValidationError):
        CandidateConfigSpec.model_validate(data)


def test_spec_rejects_unknown_keys_and_untrusted_sources():
    with pytest.raises(ValidationError):
        CandidateConfigSpec.model_validate({**spec_dict(), "experience": "10 years"})
    with pytest.raises(ValidationError):
        CandidateConfigSpec.model_validate(spec_dict(source="USER_PROVIDED_DOCUMENT"))
    with pytest.raises(ValidationError, match="unknown fields"):
        CandidateConfigSpec.model_validate(spec_dict(search_sources={"phone": "IMPORTED"}))
    with pytest.raises(ValidationError, match="outrank"):
        CandidateConfigSpec.model_validate(
            spec_dict(source="MANUAL_ENTRY", search_sources={"home_city": "USER_CONFIRMED"})
        )


def test_commute_limit_needs_the_car_route_measure():
    with pytest.raises(ValidationError, match="CAR_ROUTE"):
        CandidateSearchSettings(max_commute_km=100)


def test_target_roles_keep_primary_first_without_duplicates():
    settings = CandidateSearchSettings(
        primary_roles=["Junior Python Developer", "QA"], secondary_roles=["qa", "Support"]
    )
    assert settings.target_roles == ["Junior Python Developer", "QA", "Support"]


# --- plan / apply -----------------------------------------------------------------


def test_apply_persists_everything_with_provenance_and_one_revision(db):
    plan, version = _apply(db)
    assert version == 3 and not plan.refusals
    db.expire_all()
    config = load_search_config(db)
    assert config.location_city == "Musterstadt"
    assert config.search.max_commute_km == 100
    assert config.search.commute_measure == "CAR_ROUTE"
    assert config.search.remote_work_from_region == "Hessen"
    assert config.search.earliest_start_date == date(2027, 3, 1)
    assert (config.search.minimum_salary, config.search.maximum_salary) == (45000, 50000)
    assert config.contact.phone == "+49 170 0000000"
    assert config.search_trust["home_city"].source == "USER_CONFIRMED"
    assert config.search_trust["commute_origin"].source == "IMPORTED"
    assert config.contact_trust["phone"].confirmed_on == date(2026, 10, 27)

    profile = db.get(CandidateProfileRecord, 1)
    # Role priorities live in the search configuration; profile.target_roles
    # (which drives the Stage 11A/11B collector gates) is left as it was.
    assert json.loads(profile.target_roles_json) == ["Junior Python Developer"]
    assert config.search.primary_roles == ["Junior Python Developer", "Python Backend Developer"]
    assert config.search.secondary_roles == ["Technical Application Support"]
    assert json.loads(profile.field_trust_json)["location_city"]["source"] == "USER_CONFIRMED"
    # Stage 6A columns kept consistent, unrelated ones untouched
    prefs = profile.job_preferences
    assert prefs.minimum_salary == 45000 and prefs.salary_currency == "EUR"
    assert prefs.relocation is True and prefs.remote_preference == "FLEXIBLE"
    assert json.loads(prefs.employment_types_json) == ["FULL_TIME"]

    revisions = db.scalars(select(CandidateProfileRevisionRecord)).all()
    assert len(revisions) == 1
    revision = revisions[0]
    assert (revision.profile_version, revision.actor, revision.source) == (
        3,
        "operator",
        "USER_CONFIRMED",
    )
    changed = json.loads(revision.changed_fields_json)
    assert "search.max_commute_km" in changed and "skill.AWS" in changed
    assert json.loads(revision.before_json)["profile.location_city"] == "Frankfurt am Main"


def test_skills_are_only_added_never_modified_or_removed(db):
    _apply(db)
    skills = {s.name: s for s in db.scalars(select(CandidateSkillRecord)).all()}
    assert set(skills) == {"Python", "Pydantic", "AWS"}
    assert skills["Python"].notes == "kept as is"
    assert skills["AWS"].notes == "Learning/project only"  # never upgraded to commercial


def test_language_provenance_is_upgraded_but_level_never_invented(db):
    _apply(db)
    languages = {lang.language: lang for lang in db.scalars(select(CandidateLanguageRecord))}
    assert languages["Deutsch"].level == "B1"
    assert languages["Deutsch"].source == "USER_CONFIRMED"
    assert languages["Englisch"].level == "UNKNOWN"


def test_reapplying_the_same_file_is_a_no_op(db):
    _apply(db)
    plan = plan_config_change(db.get(CandidateProfileRecord, 1), spec())
    assert plan.is_noop
    with pytest.raises(CandidateConfigRefusedError, match="nothing to change"):
        apply_config_plan(db, plan, actor="operator", reason="again")


def test_omitted_search_keys_are_left_untouched(db):
    _apply(db)
    partial = spec_dict()
    partial["search"] = {"max_commute_km": 80, "commute_measure": "CAR_ROUTE"}
    plan, _ = _apply(db, CandidateConfigSpec.model_validate(partial))
    assert {c.name for c in plan.changes if c.area == "search"} == {"max_commute_km"}
    db.expire_all()
    assert load_search_config(db).search.relocation_regions == ["Hessen"]


def test_a_less_authoritative_source_never_overwrites_a_confirmed_value(db):
    _apply(db)
    lower = spec_dict(source="MANUAL_ENTRY")
    lower["search"]["max_commute_km"] = 150
    lower["contact"]["phone"] = "+49 170 1111111"
    lower["languages"] = [{"language": "Deutsch", "level": "B2"}]
    plan = plan_config_change(
        db.get(CandidateProfileRecord, 1), CandidateConfigSpec.model_validate(lower)
    )
    assert any("search.max_commute_km" in r for r in plan.refusals)
    assert any("contact.phone" in r for r in plan.refusals)
    assert any("Deutsch" in r for r in plan.refusals)
    with pytest.raises(CandidateConfigRefusedError):
        apply_config_plan(db, plan, actor="operator", reason="old CV")
    db.expire_all()
    assert load_search_config(db).search.max_commute_km == 100


def test_language_level_increase_needs_a_user_confirmed_source(db):
    manual = spec_dict(source="MANUAL_ENTRY")
    manual["languages"] = [{"language": "Deutsch", "level": "B2"}]
    plan = plan_config_change(
        db.get(CandidateProfileRecord, 1), CandidateConfigSpec.model_validate(manual)
    )
    assert any("B1 -> B2" in r for r in plan.refusals)


def test_stale_version_is_refused_without_writing(db):
    profile = db.get(CandidateProfileRecord, 1)
    plan = plan_config_change(profile, spec())
    db.execute(
        CandidateProfileRecord.__table__.update().values(profile_version=99)
    )  # a concurrent writer
    db.commit()
    with pytest.raises(CandidateProfileVersionConflictError):
        apply_config_plan(db, plan, actor="operator", reason="stale")
    assert db.scalar(select(func.count()).select_from(CandidateProfileRevisionRecord)) == 0


def test_revisions_are_append_only(db):
    _apply(db)
    revision = db.scalars(select(CandidateProfileRevisionRecord)).one()
    revision.reason = "rewritten"
    with pytest.raises(CandidateProfileRevisionImmutableError):
        db.flush()
    db.rollback()
    with pytest.raises(sqlalchemy.exc.DatabaseError):
        db.connection().exec_driver_sql("DELETE FROM candidate_profile_revisions")
    db.rollback()


# --- Stage 6A PATCH compatibility ---------------------------------------------------


def test_patching_job_preferences_keeps_the_search_configuration(db):
    _apply(db)
    profile = db.get(CandidateProfileRecord, 1)
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=profile.profile_version,
            job_preferences={"employment_types": ["FULL_TIME", "PART_TIME"]},
        ),
    )
    db.expire_all()
    config = load_search_config(db)
    assert config.search.max_commute_km == 100
    assert config.search.home_city == "Musterstadt"


def test_patch_cannot_replace_a_confirmed_fact_with_document_data(db):
    _apply(db)
    version = db.get(CandidateProfileRecord, 1).profile_version
    old_cv = CandidateProfilePatchRequest(
        expected_profile_version=version,
        location_city="Frankfurt am Main",
        field_trust={"location_city": {"source": "USER_PROVIDED_DOCUMENT"}},
    )
    with pytest.raises(CandidateProfileTrustDowngradeError) as exc:
        apply_candidate_profile_patch(db, old_cv)
    assert exc.value.fields == ["location_city"]
    db.expire_all()
    profile = db.get(CandidateProfileRecord, 1)
    assert (profile.location_city, profile.profile_version) == ("Musterstadt", version)


def test_patch_cannot_downgrade_a_confirmed_language_from_a_document(db):
    _apply(db)
    version = db.get(CandidateProfileRecord, 1).profile_version
    with pytest.raises(CandidateProfileTrustDowngradeError, match="languages.Deutsch"):
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=version,
                languages=[
                    {"language": "Deutsch", "level": "B2", "source": "USER_PROVIDED_DOCUMENT"}
                ],
            ),
        )


def test_a_direct_manual_patch_of_a_confirmed_fact_is_still_allowed(db):
    _apply(db)
    version = db.get(CandidateProfileRecord, 1).profile_version
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(expected_profile_version=version, location_city="Offenbach"),
    )
    assert db.get(CandidateProfileRecord, 1).location_city == "Offenbach"


def test_patch_endpoint_maps_a_trust_downgrade_to_409(db, monkeypatch):
    from app.core.config import Settings
    from app.db.session import get_db
    from app.main import app
    from app.security import rate_limit as rate_limit_module

    _apply(db)
    settings = Settings(api_key="test-api-key", rate_limit_requests=1000)
    for target in ("app.security.auth", "app.security.rate_limit", "app.api.routes"):
        monkeypatch.setattr(f"{target}.get_settings", lambda: settings)
    rate_limit_module._requests.clear()
    app.dependency_overrides[get_db] = lambda: db
    try:
        version = db.get(CandidateProfileRecord, 1).profile_version
        response = TestClient(app).patch(
            "/api/v1/candidate-profile",
            headers={"X-API-Key": "test-api-key"},
            json={
                "expected_profile_version": version,
                "location_city": "Frankfurt am Main",
                "field_trust": {"location_city": {"source": "IMPORTED"}},
            },
        )
    finally:
        app.dependency_overrides.clear()
        rate_limit_module._requests.clear()
    assert response.status_code == 409
    assert response.json()["detail"]["fields"] == ["location_city"]
    db.expire_all()
    assert db.get(CandidateProfileRecord, 1).location_city == "Musterstadt"


# --- applications are never touched ----------------------------------------------------


def test_configuration_never_touches_existing_applications(db):
    tracker = ApplicationTracker()
    for index in range(10):
        tracker.create_application(
            db,
            company_name=f"Employer {index}",
            role_title="Python Developer",
            source="bundesagentur",
            source_reference=f"10001-{index}-S",
            application_channel=C.EMAIL,
            initial_status=S.SUBMITTED,
            origin=O.IMPORT,
            actor="test",
            occurred_at=NOW,
        )
    before = [
        (a.id, a.current_status, a.next_followup_at, a.version)
        for a in db.scalars(select(ApplicationRecord).order_by(ApplicationRecord.id))
    ]
    events = db.scalar(select(func.count()).select_from(ApplicationEventRecord))
    _apply(db)
    db.expire_all()
    after = [
        (a.id, a.current_status, a.next_followup_at, a.version)
        for a in db.scalars(select(ApplicationRecord).order_by(ApplicationRecord.id))
    ]
    assert after == before and len(after) == 10
    assert db.scalar(select(func.count()).select_from(ApplicationEventRecord)) == events


# --- migration -------------------------------------------------------------------------


def _alembic(path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return cfg


def test_migration_is_additive_and_preserves_existing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    upgrade(_alembic(path), "f4b8d2a6c9e1")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO candidate_profiles (id, profile_version, location_city, "
            "professional_summary, career_goal, target_roles_json, field_trust_json, "
            "created_at, updated_at) VALUES (1, 4, 'Frankfurt am Main', '', '', "
            "'[\"Junior Python Developer\"]', '{}', '2026-10-15', '2026-10-15')"
        )
        conn.execute(
            "INSERT INTO candidate_job_preferences (candidate_profile_id, "
            "preferred_locations_json, remote_preference, employment_types_json) "
            "VALUES (1, '[\"Hessen\"]', 'UNKNOWN', '[]')"
        )
    upgrade(_alembic(path), "a9d3f6b2c4e8")
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT profile_version, location_city, contact_phone, contact_trust_json "
            "FROM candidate_profiles"
        ).fetchone() == (4, "Frankfurt am Main", None, "{}")
        assert conn.execute(
            "SELECT preferred_locations_json, max_commute_km, remote_work_scope, "
            "relocation_regions_json, search_trust_json FROM candidate_job_preferences"
        ).fetchone() == ('["Hessen"]', None, "UNKNOWN", "[]", "{}")
        triggers = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    assert {
        "trg_candidate_profile_revisions_no_update",
        "trg_candidate_profile_revisions_no_delete",
        "trg_application_events_no_update",
    } <= triggers


def test_downgrade_refuses_while_audit_rows_exist_and_round_trips_otherwise(tmp_path):
    path = tmp_path / "roundtrip.db"
    upgrade(_alembic(path), "head")
    downgrade(_alembic(path), "f4b8d2a6c9e1")
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(candidate_profiles)")}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "contact_phone" not in columns and "candidate_profile_revisions" not in tables
    upgrade(_alembic(path), "head")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO candidate_profiles (id, profile_version, professional_summary, "
            "career_goal, target_roles_json, field_trust_json, contact_trust_json, created_at, "
            "updated_at) VALUES (1, 1, '', '', '[]', '{}', '{}', '2026-10-27', '2026-10-27')"
        )
        conn.execute(
            "INSERT INTO candidate_profile_revisions (candidate_profile_id, profile_version, "
            "recorded_at, actor, reason, source, changed_fields_json, before_json, after_json) "
            "VALUES (1, 1, '2026-10-27', 'op', 'r', 'USER_CONFIRMED', '[]', '{}', '{}')"
        )
    with pytest.raises(RuntimeError, match="candidate_profile_revisions holds 1 row"):
        downgrade(_alembic(path), "f4b8d2a6c9e1")


# --- CLI -------------------------------------------------------------------------------


@pytest.fixture()
def migrated(tmp_path):
    path = tmp_path / "cli.db"
    upgrade(_alembic(path), "head")
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    session = sessionmaker(engine)()
    profile = get_or_create_candidate_profile(session)
    apply_candidate_profile_patch(
        session,
        CandidateProfilePatchRequest(
            expected_profile_version=profile.profile_version, location_city="Frankfurt am Main"
        ),
    )
    session.close()
    engine.dispose()
    config = tmp_path / "config.json"
    config.write_text(json.dumps(spec_dict()), encoding="utf-8")
    return path, config


def _cli(path, *args):
    return cli.run(["--database", str(path), *args])


def test_cli_dry_run_writes_nothing_and_apply_needs_the_reviewed_version(migrated, capsys):
    path, config = migrated
    common = ["apply", "--config", str(config), "--actor", "operator", "--reason", "confirmed"]
    assert _cli(path, *common) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: profile_version=2" in out and "CHANGE search.max_commute_km" in out
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT profile_version FROM candidate_profiles").fetchone() == (2,)
    assert _cli(path, *common, "--apply", "--expected-version", "1") == 2
    assert "does not match" in capsys.readouterr().err
    assert _cli(path, *common, "--apply", "--expected-version", "2") == 0
    assert "APPLIED: profile_version=3" in capsys.readouterr().out
    assert _cli(path, *common) == 0
    assert "NO_CHANGE" in capsys.readouterr().out
    assert _cli(path, "history") == 0
    assert "| operator | USER_CONFIRMED | confirmed |" in capsys.readouterr().out
    assert _cli(path, "show") == 0
    assert "search.max_commute_km=100  [USER_CONFIRMED/CONFIRMED]" in capsys.readouterr().out


def test_cli_refuses_an_invalid_file_and_a_uri_path(migrated, capsys, tmp_path):
    path, _ = migrated
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**spec_dict(), "source": "INFERRED"}), encoding="utf-8")
    args = ["--actor", "operator", "--reason", "x"]
    assert _cli(path, "apply", "--config", str(bad), *args) == 2
    assert _cli(path, "apply", "--config", "file:///etc/passwd", *args) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_cli_refuses_the_protected_canonical_database(capsys):
    assert cli.run(["--database", str(PROJECT_ROOT / "stage14b_canonical_v12.db"), "show"]) == 2
    assert "REFUSED" in capsys.readouterr().err


# --- Codex Stage 17 review regressions -------------------------------------------------


def test_partial_role_file_never_touches_profile_target_roles(db):
    partial = spec_dict(search_sources={"secondary_roles": "IMPORTED"})
    partial["search"] = {"secondary_roles": ["Imported Role"]}
    _apply(db, CandidateConfigSpec.model_validate(partial))
    db.expire_all()
    profile = db.get(CandidateProfileRecord, 1)
    assert json.loads(profile.target_roles_json) == ["Junior Python Developer"]
    config = load_search_config(db)
    assert config.search.secondary_roles == ["Imported Role"]
    assert config.search_trust["secondary_roles"].source == "IMPORTED"
    # a confirmed role list is never replaced by imported data
    _apply(db)
    plan = plan_config_change(
        db.get(CandidateProfileRecord, 1), CandidateConfigSpec.model_validate(partial)
    )
    assert any("search.secondary_roles" in refusal for refusal in plan.refusals)


def test_partial_update_derives_stage6a_columns_from_the_effective_configuration(db):
    _apply(db)
    partial = spec_dict()
    partial["search"] = {
        "home_city": "Offenbach am Main",
        "commute_origin_label": "Offenbach am Main Hbf",
    }
    _apply(db, CandidateConfigSpec.model_validate(partial))
    db.expire_all()
    prefs = db.get(CandidateProfileRecord, 1).job_preferences
    assert json.loads(prefs.preferred_locations_json) == ["Offenbach am Main", "Hessen"]
    assert prefs.relocation is True
    off = spec_dict()
    off["search"] = {"remote_work_scope": "UNKNOWN"}
    _apply(db, CandidateConfigSpec.model_validate(off))
    db.expire_all()
    assert db.get(CandidateProfileRecord, 1).job_preferences.remote_preference == "UNKNOWN"


def test_commute_limit_is_capped_inside_the_ba_radius_range():
    with pytest.raises(ValidationError):
        CandidateSearchSettings(max_commute_km=176, commute_measure="CAR_ROUTE")
    CandidateSearchSettings(max_commute_km=175, commute_measure="CAR_ROUTE")


def test_document_grade_collection_cannot_drop_a_confirmed_language(db):
    _apply(db)
    version = db.get(CandidateProfileRecord, 1).profile_version
    with pytest.raises(CandidateProfileTrustDowngradeError, match="languages.Deutsch"):
        apply_candidate_profile_patch(
            db,
            CandidateProfilePatchRequest(
                expected_profile_version=version,
                languages=[
                    {"language": "Englisch", "level": "B2", "source": "USER_PROVIDED_DOCUMENT"}
                ],
            ),
        )


def test_document_grade_collection_cannot_inflate_a_confirmed_skill(db):
    _apply(db)
    version = db.get(CandidateProfileRecord, 1).profile_version
    skills = [
        {"name": "Pydantic", "category": "FRAMEWORK", "source": "USER_CONFIRMED"},
        {"name": "Python", "category": "LANGUAGE", "notes": "kept as is"},
        {"name": "AWS", "category": "CLOUD", "proficiency": "EXPERT", "source": "IMPORTED"},
    ]
    with pytest.raises(CandidateProfileTrustDowngradeError, match=r"skills\.AWS \(proficiency"):
        apply_candidate_profile_patch(
            db, CandidateProfilePatchRequest(expected_profile_version=version, skills=skills)
        )
    db.expire_all()
    aws = db.scalars(select(CandidateSkillRecord).where(CandidateSkillRecord.name == "AWS")).one()
    assert aws.proficiency == "UNKNOWN"


def test_a_manual_collection_may_still_remove_a_confirmed_language(db):
    _apply(db)
    version = db.get(CandidateProfileRecord, 1).profile_version
    apply_candidate_profile_patch(
        db,
        CandidateProfilePatchRequest(
            expected_profile_version=version,
            languages=[{"language": "Deutsch", "level": "B1", "source": "MANUAL_ENTRY"}],
        ),
    )
    db.expire_all()
    names = {lang.language for lang in db.scalars(select(CandidateLanguageRecord))}
    assert names == {"Deutsch"}


def test_a_partial_file_may_not_leave_an_invalid_combined_configuration(db):
    _apply(db)
    drop_measure = spec_dict()
    drop_measure["search"] = {"commute_measure": None}
    plan = plan_config_change(
        db.get(CandidateProfileRecord, 1), CandidateConfigSpec.model_validate(drop_measure)
    )
    assert any("combined configuration is invalid" in r for r in plan.refusals)
    inverted = spec_dict()
    inverted["search"] = {"maximum_salary": 30000}
    plan = plan_config_change(
        db.get(CandidateProfileRecord, 1), CandidateConfigSpec.model_validate(inverted)
    )
    assert any("maximum_salary is below minimum_salary" in r for r in plan.refusals)


def test_an_empty_collection_cannot_wipe_confirmed_items(db):
    _apply(db)
    version = db.get(CandidateProfileRecord, 1).profile_version
    for collection in ("languages", "skills"):
        with pytest.raises(CandidateProfileTrustDowngradeError, match=collection):
            apply_candidate_profile_patch(
                db,
                CandidateProfilePatchRequest(expected_profile_version=version, **{collection: []}),
            )
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(CandidateLanguageRecord)) == 2


def test_configured_entry_level_target_feeds_the_collector_seniority_gate(tmp_path):
    from app.services.collector_runner import _candidate_target_seniority

    engine = create_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    try:
        get_or_create_candidate_profile(session)  # empty target_roles
        assert _candidate_target_seniority(session) == "UNKNOWN"
        data = spec_dict()
        data["search"] = {"target_seniority": "JUNIOR"}
        profile = session.get(CandidateProfileRecord, 1)
        apply_config_plan(
            session,
            plan_config_change(profile, CandidateConfigSpec.model_validate(data)),
            actor="t",
            reason="r",
        )
        assert _candidate_target_seniority(session) == "JUNIOR"
    finally:
        session.close()
        engine.dispose()


def test_commute_origin_must_lie_inside_the_home_place():
    with pytest.raises(ValidationError, match="inside home_city"):
        CandidateSearchSettings(
            home_city="Musterstadt",
            commute_origin_label="Nachbarstadt Hbf",
            max_commute_km=100,
            commute_measure="CAR_ROUTE",
        )


def test_upgrading_a_database_already_at_a9d3f6b2c4e8_adds_the_new_columns(tmp_path):
    path = tmp_path / "published.db"
    upgrade(_alembic(path), "a9d3f6b2c4e8")
    upgrade(_alembic(path), "head")
    with sqlite3.connect(path) as conn:
        prefs = {row[1] for row in conn.execute("PRAGMA table_info(candidate_job_preferences)")}
        checks = {row[1] for row in conn.execute("PRAGMA table_info(job_commute_checks)")}
        head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"target_seniority", "search_keywords_json"} <= prefs
    assert {"origin_lat", "origin_lon"} <= checks
    assert head == ("b6c1e9a4d2f7",)
    downgrade(_alembic(path), "a9d3f6b2c4e8")
    with sqlite3.connect(path) as conn:
        prefs = {row[1] for row in conn.execute("PRAGMA table_info(candidate_job_preferences)")}
    assert "target_seniority" not in prefs and "home_city" in prefs


def test_b6c1_downgrade_refuses_to_discard_populated_values(tmp_path):
    path = tmp_path / "populated.db"
    upgrade(_alembic(path), "head")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO candidate_profiles (id, profile_version, professional_summary, "
            "career_goal, target_roles_json, field_trust_json, contact_trust_json, created_at, "
            "updated_at) VALUES (1, 1, '', '', '[]', '{}', '{}', '2026-10-27', '2026-10-27')"
        )
        conn.execute(
            "INSERT INTO candidate_job_preferences (candidate_profile_id, "
            "preferred_locations_json, remote_preference, employment_types_json, "
            "target_seniority) VALUES (1, '[]', 'UNKNOWN', '[]', 'JUNIOR')"
        )
    with pytest.raises(RuntimeError, match="candidate_job_preferences holds 1 row"):
        downgrade(_alembic(path), "a9d3f6b2c4e8")
