from pathlib import Path

import pytest
import sqlalchemy.exc
from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_head_creates_expected_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_test.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"jobs", "user_profiles"} <= tables

    job_columns = {col["name"] for col in inspector.get_columns("jobs")}
    assert job_columns == {
        "id",
        "fingerprint",
        "source",
        "title",
        "company",
        "location",
        "url",
        "description",
        "skills_json",
        "data_confidence",
        "skill_source",
        "must_have_skills_json",
        "nice_to_have_skills_json",
        "score",
        "recommendation",
        "status",
        "first_seen_at",
        "last_seen_at",
    }

    profile_columns = {col["name"] for col in inspector.get_columns("user_profiles")}
    assert profile_columns == {"id", "name", "skills_json", "updated_at"}

    job_unique_constraints = inspector.get_unique_constraints("jobs")
    assert any(uc["column_names"] == ["fingerprint"] for uc in job_unique_constraints)

    job_indexes = inspector.get_indexes("jobs")
    assert any(idx["column_names"] == ["status"] for idx in job_indexes)


def test_enrichment_migration_backfills_existing_jobs_with_safe_defaults(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_existing_job.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "9fd80046ea7e")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    fingerprint, source, title, company, location, url, description,
                    skills_json, score, recommendation, status, first_seen_at, last_seen_at
                ) VALUES (
                    'existing-fingerprint', 'bundesagentur', 'Existing Job', 'Example GmbH',
                    'Berlin', 'https://example.com/jobs/existing', 'Existing description',
                    '["python"]', 75, 'MAYBE', 'NEW', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    upgrade(cfg, "head")

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                SELECT data_confidence, skill_source, must_have_skills_json,
                       nice_to_have_skills_json
                FROM jobs WHERE fingerprint = 'existing-fingerprint'
                """
                )
            )
            .mappings()
            .one()
        )

    assert row["data_confidence"] == 0.0
    assert row["skill_source"] is None
    assert row["must_have_skills_json"] == "[]"
    assert row["nice_to_have_skills_json"] == "[]"


def test_downgrade_base_then_upgrade_head_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_roundtrip.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "base")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "jobs" not in inspector.get_table_names()
    assert "user_profiles" not in inspector.get_table_names()

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    tables = set(inspector.get_table_names())
    assert {"jobs", "user_profiles"} <= tables


def test_company_research_migration_creates_table_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_company_research.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "company_research" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("company_research")}
    assert columns == {
        "id",
        "identity_key",
        "normalized_company_name",
        "normalized_domain",
        "company_name",
        "company_domain",
        "industry",
        "headquarters",
        "company_size",
        "short_summary",
        "products_or_services_json",
        "technologies_json",
        "hiring_signals_json",
        "relevant_facts_json",
        "positive_signals_json",
        "risk_signals_json",
        "source_urls_json",
        "evidence_json",
        "confidence",
        "research_status",
        "provider_name",
        "researched_at",
        "last_attempt_at",
        "last_attempt_status",
        "last_error",
        "version",
        "created_at",
        "updated_at",
    }

    indexes = {tuple(idx["column_names"]) for idx in inspector.get_indexes("company_research")}
    assert ("normalized_domain",) in indexes
    assert ("normalized_company_name",) in indexes

    # identity_key's uniqueness is a UniqueConstraint (matching
    # app/db/models.py's mapped_column(..., unique=True) exactly, so
    # `alembic check` sees no model/migration drift), not a separate index —
    # see jobs.fingerprint's uq_jobs_fingerprint for the same pattern.
    unique_constraints = inspector.get_unique_constraints("company_research")
    assert any(uc["column_names"] == ["identity_key"] for uc in unique_constraints)


def test_company_research_identity_aliases_migration_creates_table(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_company_research_aliases.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "company_research_identity_aliases" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("company_research_identity_aliases")}
    assert columns == {"id", "normalized_company_name", "company_research_id", "created_at"}

    unique_constraints = inspector.get_unique_constraints("company_research_identity_aliases")
    assert any(uc["column_names"] == ["normalized_company_name"] for uc in unique_constraints)

    foreign_keys = inspector.get_foreign_keys("company_research_identity_aliases")
    assert any(
        fk["referred_table"] == "company_research"
        and fk["constrained_columns"] == ["company_research_id"]
        for fk in foreign_keys
    )


def test_company_research_identity_aliases_rejects_duplicate_names(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_company_research_aliases_unique.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO company_research (
                    identity_key, normalized_company_name, normalized_domain,
                    company_name, provider_name, research_status, confidence, version,
                    created_at, updated_at
                ) VALUES (
                    'name:acme gmbh', 'acme gmbh', NULL, 'Acme GmbH', 'job_data',
                    'PARTIAL', 0.5, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO company_research_identity_aliases (
                    normalized_company_name, company_research_id, created_at
                ) VALUES ('acme gmbh', 1, CURRENT_TIMESTAMP)
                """
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO company_research_identity_aliases (
                        normalized_company_name, company_research_id, created_at
                    ) VALUES ('acme gmbh', 1, CURRENT_TIMESTAMP)
                    """
                )
            )


def test_company_research_identity_key_rejects_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_company_research_unique.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO company_research (
                    identity_key, normalized_company_name, normalized_domain,
                    company_name, provider_name, research_status, confidence, version,
                    created_at, updated_at
                ) VALUES (
                    'name:acme gmbh', 'acme gmbh', NULL, 'Acme GmbH', 'job_data',
                    'PARTIAL', 0.5, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO company_research (
                        identity_key, normalized_company_name, normalized_domain,
                        company_name, provider_name, research_status, confidence, version,
                        created_at, updated_at
                    ) VALUES (
                        'name:acme gmbh', 'acme gmbh', NULL, 'Acme GmbH Duplicate', 'job_data',
                        'PARTIAL', 0.5, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )


def test_company_research_downgrade_removes_table_then_upgrade_restores_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_company_research_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "c4e72b1a8d9f")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "company_research" not in inspector.get_table_names()
    assert "company_research_identity_aliases" not in inspector.get_table_names()

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "company_research" in inspector.get_table_names()


def test_candidate_profile_migration_creates_expected_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_candidate_profile.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "candidate_profiles",
        "candidate_skills",
        "candidate_experiences",
        "candidate_education",
        "candidate_certifications",
        "candidate_projects",
        "candidate_languages",
        "candidate_job_preferences",
    } <= tables

    profile_columns = {col["name"] for col in inspector.get_columns("candidate_profiles")}
    assert profile_columns == {
        "id",
        "profile_version",
        "first_name",
        "last_name",
        "professional_title",
        "location_city",
        "location_country",
        "professional_summary",
        "career_goal",
        "target_roles_json",
        "field_trust_json",
        "created_at",
        "updated_at",
    }

    skill_unique = inspector.get_unique_constraints("candidate_skills")
    assert any(
        set(uc["column_names"]) == {"candidate_profile_id", "normalized_name"}
        for uc in skill_unique
    )

    language_unique = inspector.get_unique_constraints("candidate_languages")
    assert any(
        set(uc["column_names"]) == {"candidate_profile_id", "normalized_language"}
        for uc in language_unique
    )

    preferences_unique = inspector.get_unique_constraints("candidate_job_preferences")
    assert any(uc["column_names"] == ["candidate_profile_id"] for uc in preferences_unique)

    for child_table in (
        "candidate_skills",
        "candidate_experiences",
        "candidate_education",
        "candidate_certifications",
        "candidate_projects",
        "candidate_languages",
        "candidate_job_preferences",
    ):
        foreign_keys = inspector.get_foreign_keys(child_table)
        assert any(
            fk["referred_table"] == "candidate_profiles"
            and fk["constrained_columns"] == ["candidate_profile_id"]
            for fk in foreign_keys
        )


def test_candidate_profiles_singleton_check_constraint_rejects_second_row(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_candidate_profile_singleton.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO candidate_profiles (
                    id, profile_version, professional_summary, career_goal,
                    target_roles_json, field_trust_json, created_at, updated_at
                ) VALUES (1, 1, '', '', '[]', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO candidate_profiles (
                        id, profile_version, professional_summary, career_goal,
                        target_roles_json, field_trust_json, created_at, updated_at
                    ) VALUES (2, 1, '', '', '[]', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )


def test_candidate_profile_downgrade_removes_tables_then_upgrade_restores_them(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_candidate_profile_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "a1c9e3f7b2d4")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "candidate_profiles" not in tables
    assert "candidate_skills" not in tables
    # Downgrading one step must not touch the previous (Company Research)
    # migration's own tables.
    assert "company_research" in tables

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "candidate_profiles" in inspector.get_table_names()


def test_candidate_job_matches_migration_creates_table_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_candidate_job_matches.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "candidate_job_matches" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("candidate_job_matches")}
    assert columns == {
        "id",
        "job_id",
        "candidate_profile_version",
        "job_snapshot_fingerprint",
        "algorithm_version",
        "company_research_id",
        "overall_score",
        "coverage_score",
        "required_skill_score",
        "preferred_skill_score",
        "analysis_json",
        "created_at",
    }

    indexes = {tuple(idx["column_names"]) for idx in inspector.get_indexes("candidate_job_matches")}
    assert ("job_id",) in indexes

    unique_constraints = inspector.get_unique_constraints("candidate_job_matches")
    assert any(
        set(uc["column_names"])
        == {
            "job_id",
            "candidate_profile_version",
            "job_snapshot_fingerprint",
            "algorithm_version",
        }
        for uc in unique_constraints
    )


def test_candidate_job_matches_cache_identity_rejects_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_candidate_job_matches_unique.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO candidate_job_matches (
                    job_id, candidate_profile_version, job_snapshot_fingerprint,
                    algorithm_version, company_research_id, overall_score, coverage_score,
                    required_skill_score, preferred_skill_score, analysis_json, created_at
                ) VALUES (
                    1, 1, 'fp-a', 'v1', NULL, 50, 50, 50, 100, '{}', CURRENT_TIMESTAMP
                )
                """
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO candidate_job_matches (
                        job_id, candidate_profile_version, job_snapshot_fingerprint,
                        algorithm_version, company_research_id, overall_score, coverage_score,
                        required_skill_score, preferred_skill_score, analysis_json, created_at
                    ) VALUES (
                        1, 1, 'fp-a', 'v1', NULL, 10, 10, 10, 10, '{}', CURRENT_TIMESTAMP
                    )
                    """
                )
            )


def test_candidate_job_matches_downgrade_removes_table_then_upgrade_restores_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_candidate_job_matches_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "fa99eefca6bd")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "candidate_job_matches" not in tables
    # Downgrading one step must not touch the previous (Candidate Profile)
    # migration's own tables.
    assert "candidate_profiles" in tables

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "candidate_job_matches" in inspector.get_table_names()


def test_candidate_cv_drafts_migration_creates_table_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_candidate_cv_drafts.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "candidate_cv_drafts" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("candidate_cv_drafts")}
    assert columns == {
        "id",
        "job_id",
        "match_id",
        "candidate_profile_version",
        "job_snapshot_fingerprint",
        "match_algorithm_version",
        "cv_adapter_version",
        "status",
        "draft_json",
        "created_at",
    }

    indexes = {tuple(idx["column_names"]) for idx in inspector.get_indexes("candidate_cv_drafts")}
    assert ("job_id",) in indexes
    assert ("match_id",) in indexes

    unique_constraints = inspector.get_unique_constraints("candidate_cv_drafts")
    assert any(
        set(uc["column_names"]) == {"match_id", "cv_adapter_version"} for uc in unique_constraints
    )


def test_candidate_cv_drafts_cache_identity_rejects_duplicates(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_candidate_cv_drafts_unique.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO candidate_cv_drafts (
                    job_id, match_id, candidate_profile_version, job_snapshot_fingerprint,
                    match_algorithm_version, cv_adapter_version, status, draft_json, created_at
                ) VALUES (
                    1, 1, 1, 'fp-a', 'v1', 'v1', 'DRAFT', '{}', CURRENT_TIMESTAMP
                )
                """
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO candidate_cv_drafts (
                        job_id, match_id, candidate_profile_version, job_snapshot_fingerprint,
                        match_algorithm_version, cv_adapter_version, status, draft_json, created_at
                    ) VALUES (
                        1, 1, 1, 'fp-b', 'v1', 'v1', 'DRAFT', '{}', CURRENT_TIMESTAMP
                    )
                    """
                )
            )


def test_candidate_cv_drafts_downgrade_removes_table_then_upgrade_restores_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_candidate_cv_drafts_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "ececa0eab87a")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "candidate_cv_drafts" not in tables
    # Downgrading one step must not touch the previous (Candidate Job
    # Match) migration's own tables.
    assert "candidate_job_matches" in tables

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "candidate_cv_drafts" in inspector.get_table_names()
