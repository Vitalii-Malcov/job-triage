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


def test_bewerbung_drafts_migration_creates_table_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_bewerbung_drafts.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "bewerbung_drafts" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("bewerbung_drafts")}
    assert columns == {
        "id",
        "job_id",
        "cv_draft_id",
        "match_id",
        "candidate_profile_version",
        "job_snapshot_fingerprint",
        "match_algorithm_version",
        "cv_adapter_version",
        "bewerbung_generator_version",
        "provider",
        "status",
        "draft_json",
        "created_at",
    }

    indexes = {tuple(idx["column_names"]) for idx in inspector.get_indexes("bewerbung_drafts")}
    assert ("job_id",) in indexes
    assert ("cv_draft_id",) in indexes
    assert ("match_id",) in indexes

    # No cache-identity UNIQUE constraint (unlike candidate_cv_drafts) —
    # deliberate, see BewerbungDraftRecord's docstring: every successful
    # generation always inserts a new row.
    assert inspector.get_unique_constraints("bewerbung_drafts") == []


def test_bewerbung_drafts_allows_duplicate_pinned_inputs(tmp_path: Path) -> None:
    """Regeneration is intentional (Stage 6D section 35) — two rows with
    byte-identical pinned inputs must both insert successfully, unlike
    candidate_cv_drafts' cache-identity UNIQUE constraint."""
    db_path = tmp_path / "migrations_bewerbung_drafts_duplicates.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    insert_sql = text(
        """
        INSERT INTO bewerbung_drafts (
            job_id, cv_draft_id, match_id, candidate_profile_version,
            job_snapshot_fingerprint, match_algorithm_version, cv_adapter_version,
            bewerbung_generator_version, provider, status, draft_json, created_at
        ) VALUES (
            1, 1, 1, 1, 'fp-a', 'v1', 'v1', 'v1', 'deterministic', 'DRAFT', '{}',
            CURRENT_TIMESTAMP
        )
        """
    )
    with engine.begin() as connection:
        connection.execute(insert_sql)
        connection.execute(insert_sql)

    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM bewerbung_drafts")).scalar_one()
    assert count == 2


def test_bewerbung_drafts_downgrade_removes_table_then_upgrade_restores_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_bewerbung_drafts_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "db47a801596b")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "bewerbung_drafts" not in tables
    # Downgrading one step must not touch the previous (Candidate CV
    # Draft) migration's own tables.
    assert "candidate_cv_drafts" in tables

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "bewerbung_drafts" in inspector.get_table_names()


def test_application_package_reviews_migration_creates_tables_and_indexes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_application_package_reviews.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "application_package_reviews" in inspector.get_table_names()
    assert "application_package_review_revisions" in inspector.get_table_names()

    review_columns = {col["name"] for col in inspector.get_columns("application_package_reviews")}
    assert review_columns == {
        "id",
        "job_id",
        "cv_draft_id",
        "bewerbung_draft_id",
        "match_id",
        "candidate_profile_version",
        "job_snapshot_fingerprint",
        "match_algorithm_version",
        "cv_adapter_version",
        "bewerbung_generator_version",
        "status",
        "review_version",
        "has_manual_overrides",
        "approved_revision_id",
        "decision_note",
        "decided_at",
        "created_at",
        "updated_at",
    }

    revision_columns = {
        col["name"] for col in inspector.get_columns("application_package_review_revisions")
    }
    assert revision_columns == {
        "id",
        "review_id",
        "revision_number",
        "reviewed_cv_json",
        "reviewed_bewerbung_json",
        "manual_override_paths_json",
        "edit_note",
        "created_at",
    }

    review_indexes = {
        tuple(idx["column_names"]) for idx in inspector.get_indexes("application_package_reviews")
    }
    assert ("job_id",) in review_indexes
    assert ("cv_draft_id",) in review_indexes
    assert ("bewerbung_draft_id",) in review_indexes

    revision_indexes = {
        tuple(idx["column_names"])
        for idx in inspector.get_indexes("application_package_review_revisions")
    }
    assert ("review_id",) in revision_indexes

    revision_unique = inspector.get_unique_constraints("application_package_review_revisions")
    assert any(
        set(uc["column_names"]) == {"review_id", "revision_number"} for uc in revision_unique
    )


def test_application_package_review_revisions_rejects_duplicate_revision_number(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_application_package_reviews_unique.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO application_package_reviews (
                    job_id, cv_draft_id, bewerbung_draft_id, match_id,
                    candidate_profile_version, job_snapshot_fingerprint,
                    match_algorithm_version, cv_adapter_version, bewerbung_generator_version,
                    status, review_version, has_manual_overrides, created_at, updated_at
                ) VALUES (
                    1, 1, 1, 1, 1, 'fp-a', 'v1', 'v1', 'v1',
                    'PENDING_REVIEW', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO application_package_review_revisions (
                    review_id, revision_number, reviewed_cv_json, reviewed_bewerbung_json,
                    manual_override_paths_json, created_at
                ) VALUES (1, 1, '{}', '{}', '[]', CURRENT_TIMESTAMP)
                """
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO application_package_review_revisions (
                        review_id, revision_number, reviewed_cv_json, reviewed_bewerbung_json,
                        manual_override_paths_json, created_at
                    ) VALUES (1, 1, '{}', '{}', '[]', CURRENT_TIMESTAMP)
                    """
                )
            )


def test_application_package_reviews_downgrade_removes_tables_then_upgrade_restores_them(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_application_package_reviews_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "2a3383bb29c5")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "application_package_reviews" not in tables
    assert "application_package_review_revisions" not in tables
    # Downgrading one step must not touch the previous (Bewerbung Drafts)
    # migration's own tables.
    assert "bewerbung_drafts" in tables

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    tables = set(inspector.get_table_names())
    assert "application_package_reviews" in tables
    assert "application_package_review_revisions" in tables


def test_gmail_inbox_migration_creates_tables_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_gmail_inbox.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "gmail_threads" in inspector.get_table_names()
    assert "gmail_messages" in inspector.get_table_names()

    thread_columns = {col["name"] for col in inspector.get_columns("gmail_threads")}
    assert thread_columns == {
        "id",
        "account_key",
        "thread_key",
        "subject",
        "created_at",
        "updated_at",
    }

    message_columns = {col["name"] for col in inspector.get_columns("gmail_messages")}
    assert message_columns == {
        "id",
        "thread_id",
        "account_key",
        "mailbox",
        "uid_validity",
        "uid",
        "message_id_header",
        "in_reply_to",
        "references_json",
        "from_address",
        "from_display_name",
        "to_addresses_json",
        "cc_addresses_json",
        "subject",
        "sent_at",
        "received_at",
        "direction",
        "body_plain",
        "body_truncated",
        "has_html",
        "attachments_json",
        "created_at",
    }

    thread_unique = inspector.get_unique_constraints("gmail_threads")
    assert any(set(uc["column_names"]) == {"account_key", "thread_key"} for uc in thread_unique)

    message_unique = inspector.get_unique_constraints("gmail_messages")
    assert any(
        set(uc["column_names"]) == {"account_key", "mailbox", "uid_validity", "uid"}
        for uc in message_unique
    )

    message_indexes = {
        tuple(idx["column_names"]) for idx in inspector.get_indexes("gmail_messages")
    }
    assert ("thread_id",) in message_indexes
    assert ("message_id_header",) in message_indexes
    assert ("account_key",) in message_indexes

    thread_indexes = {tuple(idx["column_names"]) for idx in inspector.get_indexes("gmail_threads")}
    assert ("account_key",) in thread_indexes


def _insert_gmail_thread(
    connection, *, account_key: str = "a@example.com", thread_key: str
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO gmail_threads (account_key, thread_key, subject, created_at, updated_at)
            VALUES (:account_key, :thread_key, 'Hello', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        ),
        {"account_key": account_key, "thread_key": thread_key},
    )


def _insert_gmail_message(
    connection,
    *,
    thread_id: int = 1,
    account_key: str = "a@example.com",
    mailbox: str = "INBOX",
    uid_validity: int = 100,
    uid: int = 1,
    direction: str = "INBOUND",
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO gmail_messages (
                thread_id, account_key, mailbox, uid_validity, uid, references_json,
                to_addresses_json, cc_addresses_json, subject, received_at,
                direction, body_plain, body_truncated, has_html, attachments_json,
                created_at
            ) VALUES (
                :thread_id, :account_key, :mailbox, :uid_validity, :uid, '[]', '[]', '[]',
                'Hello', CURRENT_TIMESTAMP, :direction, 'hi', 0, 0, '[]', CURRENT_TIMESTAMP
            )
            """
        ),
        {
            "thread_id": thread_id,
            "account_key": account_key,
            "mailbox": mailbox,
            "uid_validity": uid_validity,
            "uid": uid,
            "direction": direction,
        },
    )


def test_gmail_messages_rejects_duplicate_provider_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_gmail_inbox_unique.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, thread_key="<root@example.com>")
        _insert_gmail_message(connection)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_gmail_message(connection)


def test_gmail_messages_same_identity_across_two_accounts_is_allowed(tmp_path: Path) -> None:
    """GMAIL-002: the UNIQUE constraint is (account_key, mailbox,
    uid_validity, uid) — two different accounts may legitimately share
    the same mailbox/uid_validity/uid values without colliding.
    """
    db_path = tmp_path / "migrations_gmail_inbox_account_scope.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(connection, thread_id=1, account_key="a@example.com")
        _insert_gmail_thread(connection, account_key="b@example.com", thread_key="<root@b>")
        _insert_gmail_message(connection, thread_id=2, account_key="b@example.com")

    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM gmail_messages")).scalar()
    assert count == 2


def test_gmail_messages_check_constraints_reject_invalid_values(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_gmail_inbox_checks.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, thread_key="<root@example.com>")

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_gmail_message(connection, uid=0)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_gmail_message(connection, uid=-1)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_gmail_message(connection, uid_validity=0)

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            _insert_gmail_message(connection, direction="MALICIOUS")

    with engine.begin() as connection:
        _insert_gmail_message(connection, uid=1, uid_validity=100, direction="OUTBOUND")


def test_gmail_inbox_downgrade_removes_tables_then_upgrade_restores_them(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_gmail_inbox_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "0ce10aaf8c86")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "gmail_threads" not in tables
    assert "gmail_messages" not in tables
    # Downgrading past both Gmail migrations must not touch the previous
    # (Application Package Reviews) migration's own tables.
    assert "application_package_reviews" in tables

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    tables = set(inspector.get_table_names())
    assert "gmail_threads" in tables
    assert "gmail_messages" in tables


def test_gmail_account_scope_migration_downgrades_one_step_cleanly(tmp_path: Path) -> None:
    """The account-scoping/hardening migration (7058c097a542) downgrades
    to the original Stage 7A schema (8634f4be953a) without touching its
    tables — only the account_key columns/constraints it added.
    """
    db_path = tmp_path / "migrations_gmail_account_scope_downgrade.db"
    cfg = _alembic_config(db_path)

    upgrade(cfg, "head")
    downgrade(cfg, "8634f4be953a")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "gmail_threads" in tables
    assert "gmail_messages" in tables

    message_columns = {col["name"] for col in inspector.get_columns("gmail_messages")}
    assert "account_key" not in message_columns
    thread_columns = {col["name"] for col in inspector.get_columns("gmail_threads")}
    assert "account_key" not in thread_columns

    # The original (pre-account-scoping) unique constraints must be
    # restored exactly.
    message_unique = inspector.get_unique_constraints("gmail_messages")
    assert any(
        set(uc["column_names"]) == {"mailbox", "uid_validity", "uid"} for uc in message_unique
    )
    thread_unique = inspector.get_unique_constraints("gmail_threads")
    assert any(uc["column_names"] == ["thread_key"] for uc in thread_unique)

    upgrade(cfg, "head")
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    message_columns = {col["name"] for col in inspector.get_columns("gmail_messages")}
    assert "account_key" in message_columns


# ---------------------------------------------------------------------------
# GMAIL-013: safe downgrade preflight — 7058c097a542's downgrade() must
# refuse to collapse account-scoped identity into a colliding old-schema
# identity, and must do so BEFORE any DDL runs.
# ---------------------------------------------------------------------------


def _alembic_current_revision(engine) -> str:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_gmail_account_scope_downgrade_preflight_allows_clean_cycle(tmp_path: Path) -> None:
    """Case 1 (no cross-account conflicts): the normal upgrade -> downgrade
    -> upgrade cycle must still succeed when there is nothing for the
    preflight to object to.
    """
    db_path = tmp_path / "migrations_gmail_downgrade_preflight_clean.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "7058c097a542")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(connection, thread_id=1, account_key="a@example.com", uid=1)

    downgrade(cfg, "8634f4be953a")

    assert _alembic_current_revision(engine) == "8634f4be953a"
    inspector = inspect(engine)
    assert "_alembic_tmp_gmail_messages" not in inspector.get_table_names()
    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM gmail_messages")).scalar()
    assert count == 1

    upgrade(cfg, "8634f4be953a")  # re-upgrade back for symmetry with other tests
    assert _alembic_current_revision(engine) == "8634f4be953a"


def test_gmail_account_scope_downgrade_preflight_blocks_cross_account_message_conflict(
    tmp_path: Path,
) -> None:
    """Case 2 (adverse): two different accounts sharing the exact same
    (mailbox, uid_validity, uid) — legitimate under account-scoped
    identity, but something the pre-account-scoping schema's UNIQUE
    constraint cannot represent. Downgrade must fail BEFORE any DDL,
    leaving revision/rows/schema completely untouched, and must never
    leave behind a `_alembic_tmp_gmail_messages` table.
    """
    db_path = tmp_path / "migrations_gmail_downgrade_preflight_message_conflict.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "7058c097a542")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(
            connection,
            thread_id=1,
            account_key="a@example.com",
            mailbox="INBOX",
            uid_validity=100,
            uid=1,
        )
        _insert_gmail_thread(connection, account_key="b@example.com", thread_key="<root@b>")
        _insert_gmail_message(
            connection,
            thread_id=2,
            account_key="b@example.com",
            mailbox="INBOX",
            uid_validity=100,
            uid=1,
        )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - migration-defined exception type
        downgrade(cfg, "8634f4be953a")
    assert "Cannot downgrade" in str(exc_info.value)
    assert "account_key" in str(exc_info.value)

    # Nothing must have changed: revision, rows, schema, no leftover
    # batch-mode temp table.
    assert _alembic_current_revision(engine) == "7058c097a542"
    inspector = inspect(engine)
    assert "_alembic_tmp_gmail_messages" not in inspector.get_table_names()
    assert "account_key" in {col["name"] for col in inspector.get_columns("gmail_messages")}
    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM gmail_messages")).scalar()
    assert count == 2


def test_gmail_account_scope_downgrade_preflight_blocks_cross_account_thread_key_conflict(
    tmp_path: Path,
) -> None:
    """Same as the message-identity case above, but for thread_key: two
    different accounts sharing the exact same thread_key — legitimate
    under account-scoped identity, unrepresentable by the old
    (account_key-less) gmail_threads UNIQUE constraint.
    """
    db_path = tmp_path / "migrations_gmail_downgrade_preflight_thread_conflict.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "7058c097a542")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<shared@x>")
        _insert_gmail_thread(connection, account_key="b@example.com", thread_key="<shared@x>")

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - migration-defined exception type
        downgrade(cfg, "8634f4be953a")
    assert "Cannot downgrade" in str(exc_info.value)
    assert "thread_key" in str(exc_info.value)

    assert _alembic_current_revision(engine) == "7058c097a542"
    inspector = inspect(engine)
    assert "_alembic_tmp_gmail_threads" not in inspector.get_table_names()
    assert "account_key" in {col["name"] for col in inspector.get_columns("gmail_threads")}
    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM gmail_threads")).scalar()
    assert count == 2


# ---------------------------------------------------------------------------
# GMAIL-013 (upper preflight): the tests above start from 7058c097a542
# directly (the "lower preflight" scenario). Current HEAD is two
# migrations further (e6ccb9b4271b adding gmail_message_id_claims, then
# 813c9d5086d0 adding gmail_message_analyses for Stage 7B) — both of
# those migrations duplicate the SAME account-scope preflight in their
# own downgrade(), specifically so downgrading from whatever the current
# HEAD happens to be fails closed before ANY of their own DDL (dropping
# gmail_message_id_claims / gmail_message_analyses) has run, rather than
# mutating the database ahead of 7058c097a542's preflight catching the
# conflict several steps later.
# ---------------------------------------------------------------------------


def test_gmail_account_scope_downgrade_preflight_blocks_message_conflict_from_head(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_gmail_downgrade_preflight_message_conflict_head.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(
            connection,
            thread_id=1,
            account_key="a@example.com",
            mailbox="INBOX",
            uid_validity=100,
            uid=1,
        )
        _insert_gmail_thread(connection, account_key="b@example.com", thread_key="<root@b>")
        _insert_gmail_message(
            connection,
            thread_id=2,
            account_key="b@example.com",
            mailbox="INBOX",
            uid_validity=100,
            uid=1,
        )
        connection.execute(
            text(
                """
                INSERT INTO gmail_message_id_claims (
                    account_key, message_id_header, claimant_mailbox,
                    claimant_uid_validity, claimant_uid, thread_id, contested, created_at
                ) VALUES (
                    'a@example.com', '<root@a>', 'INBOX', 100, 1, 1, 0, CURRENT_TIMESTAMP
                )
                """
            )
        )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - migration-defined exception type
        downgrade(cfg, "8634f4be953a")
    assert "Cannot downgrade" in str(exc_info.value)
    assert "account_key" in str(exc_info.value)

    # Nothing must have changed — not even 805108385946's own DDL
    # (dropping job_reference_tokens), 847b7f5c87d8's own DDL (altering
    # gmail_message_analyses), 813c9d5086d0's own DDL (dropping
    # gmail_message_analyses), or e6ccb9b4271b's OWN DDL (dropping
    # gmail_message_id_claims) may have run.
    assert _alembic_current_revision(engine) == "805108385946"
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "gmail_message_id_claims" in tables
    assert "gmail_message_analyses" in tables
    assert "job_reference_tokens" in tables
    assert not any(table.startswith("_alembic_tmp") for table in tables)
    assert "account_key" in {col["name"] for col in inspector.get_columns("gmail_messages")}
    with engine.connect() as connection:
        message_count = connection.execute(text("SELECT COUNT(*) FROM gmail_messages")).scalar()
        claim_count = connection.execute(
            text("SELECT COUNT(*) FROM gmail_message_id_claims")
        ).scalar()
    assert message_count == 2
    assert claim_count == 1


def test_gmail_account_scope_downgrade_preflight_blocks_thread_conflict_from_head(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_gmail_downgrade_preflight_thread_conflict_head.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<shared@x>")
        _insert_gmail_thread(connection, account_key="b@example.com", thread_key="<shared@x>")

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - migration-defined exception type
        downgrade(cfg, "8634f4be953a")
    assert "Cannot downgrade" in str(exc_info.value)
    assert "thread_key" in str(exc_info.value)

    assert _alembic_current_revision(engine) == "805108385946"
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "gmail_message_id_claims" in tables
    assert "gmail_message_analyses" in tables
    assert "job_reference_tokens" in tables
    assert not any(table.startswith("_alembic_tmp") for table in tables)
    assert "account_key" in {col["name"] for col in inspector.get_columns("gmail_threads")}
    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM gmail_threads")).scalar()
    assert count == 2


def test_gmail_account_scope_downgrade_from_head_clean_cycle(tmp_path: Path) -> None:
    """Case 3 (required): with compatible data, upgrade head -> downgrade
    to 8634f4be953a -> upgrade head must succeed — the claims migration
    must correctly drop/recreate during a legitimate downgrade cycle.
    """
    db_path = tmp_path / "migrations_gmail_downgrade_clean_cycle_head.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(connection, thread_id=1, account_key="a@example.com", uid=1)

    downgrade(cfg, "8634f4be953a")

    assert _alembic_current_revision(engine) == "8634f4be953a"
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "gmail_message_id_claims" not in tables
    assert "gmail_message_analyses" not in tables
    assert not any(table.startswith("_alembic_tmp") for table in tables)

    upgrade(cfg, "head")
    assert _alembic_current_revision(engine) == "805108385946"
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "gmail_message_id_claims" in inspector.get_table_names()
    assert "gmail_message_analyses" in inspector.get_table_names()
    assert "job_reference_tokens" in inspector.get_table_names()
    assert "context_fingerprint" in {
        col["name"] for col in inspector.get_columns("gmail_message_analyses")
    }


# ---------------------------------------------------------------------------
# 813c9d5086d0 (Stage 7B: gmail_message_analyses)
# ---------------------------------------------------------------------------


def test_gmail_message_analyses_table_shape(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_gmail_message_analyses_shape.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    assert "gmail_message_analyses" in tables

    columns = {col["name"] for col in inspector.get_columns("gmail_message_analyses")}
    assert columns == {
        "id",
        "account_key",
        "gmail_message_id",
        "analysis_version",
        "input_fingerprint",
        "context_fingerprint",
        "match_type",
        "matched_job_id",
        "match_confidence",
        "match_score",
        "match_evidence_json",
        "candidate_matches_json",
        "classification",
        "classification_confidence",
        "classification_evidence_json",
        "is_automated",
        "requires_human_review",
        "created_at",
    }

    unique_constraints = inspector.get_unique_constraints("gmail_message_analyses")
    assert any(
        set(uc["column_names"])
        == {"gmail_message_id", "analysis_version", "input_fingerprint", "context_fingerprint"}
        for uc in unique_constraints
    )

    check_constraint_names = {
        cc["name"] for cc in inspector.get_check_constraints("gmail_message_analyses")
    }
    assert "ck_gmail_message_analyses_match_type_valid" in check_constraint_names
    assert "ck_gmail_message_analyses_classification_valid" in check_constraint_names

    foreign_keys = inspector.get_foreign_keys("gmail_message_analyses")
    assert any(fk["referred_table"] == "gmail_messages" for fk in foreign_keys)


def test_gmail_message_analyses_upgrade_downgrade_upgrade_cycle_preserves_sibling_data(
    tmp_path: Path,
) -> None:
    """813c9d5086d0's own data (analysis rows) is documented as acceptable
    to lose on downgrade (re-derivable) — but its SIBLING tables
    (gmail_messages/gmail_threads/gmail_message_id_claims) must survive
    the cycle completely untouched.
    """
    db_path = tmp_path / "migrations_gmail_message_analyses_cycle.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(connection, thread_id=1, account_key="a@example.com", uid=1)
        connection.execute(
            text(
                """
                INSERT INTO gmail_message_analyses (
                    account_key, gmail_message_id, analysis_version, input_fingerprint,
                    match_type, matched_job_id, match_confidence, match_score,
                    match_evidence_json, candidate_matches_json,
                    classification, classification_confidence, classification_evidence_json,
                    is_automated, requires_human_review, created_at
                ) VALUES (
                    'a@example.com', 1, 1, 'fp1',
                    'UNMATCHED', NULL, 'LOW', 0,
                    '[]', '[]',
                    'UNKNOWN', 'LOW', '[]',
                    0, 1, CURRENT_TIMESTAMP
                )
                """
            )
        )

    downgrade(cfg, "e6ccb9b4271b")

    assert _alembic_current_revision(engine) == "e6ccb9b4271b"
    inspector = inspect(engine)
    assert "gmail_message_analyses" not in inspector.get_table_names()
    with engine.connect() as connection:
        message_count = connection.execute(text("SELECT COUNT(*) FROM gmail_messages")).scalar()
        thread_count = connection.execute(text("SELECT COUNT(*) FROM gmail_threads")).scalar()
    assert message_count == 1
    assert thread_count == 1

    upgrade(cfg, "head")
    assert _alembic_current_revision(engine) == "805108385946"
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "gmail_message_analyses" in inspector.get_table_names()
    assert "job_reference_tokens" in inspector.get_table_names()
    assert "context_fingerprint" in {
        col["name"] for col in inspector.get_columns("gmail_message_analyses")
    }
    with engine.connect() as connection:
        analysis_count = connection.execute(
            text("SELECT COUNT(*) FROM gmail_message_analyses")
        ).scalar()
        message_count = connection.execute(text("SELECT COUNT(*) FROM gmail_messages")).scalar()
    assert analysis_count == 0  # documented: analyses do not survive a downgrade cycle
    assert message_count == 1  # sibling data is untouched


def test_gmail_message_analyses_context_fingerprint_upgrade_downgrade_upgrade_cycle(
    tmp_path: Path,
) -> None:
    """847b7f5c87d8's own narrow cycle: adding/removing just the
    `context_fingerprint` column + widened UNIQUE constraint, with
    existing gmail_message_analyses rows (inserted under the OLD 3-column
    identity) surviving the ADD COLUMN step via the column's
    server_default.
    """
    db_path = tmp_path / "migrations_gmail_context_fingerprint_cycle.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "813c9d5086d0")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(connection, thread_id=1, account_key="a@example.com", uid=1)
        connection.execute(
            text(
                """
                INSERT INTO gmail_message_analyses (
                    account_key, gmail_message_id, analysis_version, input_fingerprint,
                    match_type, matched_job_id, match_confidence, match_score,
                    match_evidence_json, candidate_matches_json,
                    classification, classification_confidence, classification_evidence_json,
                    is_automated, requires_human_review, created_at
                ) VALUES (
                    'a@example.com', 1, 1, 'fp1',
                    'UNMATCHED', NULL, 'LOW', 0,
                    '[]', '[]',
                    'UNKNOWN', 'LOW', '[]',
                    0, 1, CURRENT_TIMESTAMP
                )
                """
            )
        )

    upgrade(cfg, "847b7f5c87d8")

    assert _alembic_current_revision(engine) == "847b7f5c87d8"
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("gmail_message_analyses")}
    assert "context_fingerprint" in columns
    assert not any(table.startswith("_alembic_tmp") for table in inspector.get_table_names())
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT context_fingerprint FROM gmail_message_analyses WHERE id = 1")
        ).fetchone()
    assert row is not None
    assert row[0] == ""  # backfilled via server_default

    downgrade(cfg, "813c9d5086d0")
    assert _alembic_current_revision(engine) == "813c9d5086d0"
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("gmail_message_analyses")}
    assert "context_fingerprint" not in columns
    assert not any(table.startswith("_alembic_tmp") for table in inspector.get_table_names())
    with engine.connect() as connection:
        count = connection.execute(text("SELECT COUNT(*) FROM gmail_message_analyses")).scalar()
    assert count == 1  # row itself survives the column drop

    upgrade(cfg, "847b7f5c87d8")
    assert _alembic_current_revision(engine) == "847b7f5c87d8"


# ---------------------------------------------------------------------------
# 805108385946 (Stage 7B Codex remediation round 2, Blocker 3:
# job_reference_tokens)
# ---------------------------------------------------------------------------


def test_job_reference_tokens_table_shape(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_job_reference_tokens_shape.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    assert "job_reference_tokens" in inspector.get_table_names()

    columns = {col["name"] for col in inspector.get_columns("job_reference_tokens")}
    assert columns == {"id", "job_id", "token", "created_at"}

    unique_constraints = inspector.get_unique_constraints("job_reference_tokens")
    assert any(set(uc["column_names"]) == {"job_id", "token"} for uc in unique_constraints)

    foreign_keys = inspector.get_foreign_keys("job_reference_tokens")
    assert any(fk["referred_table"] == "jobs" for fk in foreign_keys)


def test_job_reference_tokens_backfills_existing_jobs(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_job_reference_tokens_backfill.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "847b7f5c87d8")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    fingerprint, source, title, company, location, url, description,
                    skills_json, score, recommendation, status, first_seen_at, last_seen_at
                ) VALUES (
                    'backfill-fp', 'test', 'Python Developer', 'Acme GmbH', 'Berlin',
                    'https://acme.example.com/jobs/482173', '', '[]', 80, 'APPLY', 'NEW',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    upgrade(cfg, "head")

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT job_id, token FROM job_reference_tokens")).fetchall()
    assert "482173" in {token for _job_id, token in rows}


def test_job_reference_tokens_upgrade_downgrade_upgrade_cycle_preserves_sibling_data(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "migrations_job_reference_tokens_cycle.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        _insert_gmail_thread(connection, account_key="a@example.com", thread_key="<root@a>")
        _insert_gmail_message(connection, thread_id=1, account_key="a@example.com", uid=1)
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    fingerprint, source, title, company, location, url, description,
                    skills_json, score, recommendation, status, first_seen_at, last_seen_at
                ) VALUES (
                    'cycle-fp', 'test', 'Role', 'Co', '', 'https://co.example.com/jobs/11111',
                    '', '[]', 1, 'SKIP', 'NEW', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    downgrade(cfg, "847b7f5c87d8")
    assert _alembic_current_revision(engine) == "847b7f5c87d8"
    inspector = inspect(engine)
    assert "job_reference_tokens" not in inspector.get_table_names()
    assert not any(table.startswith("_alembic_tmp") for table in inspector.get_table_names())
    with engine.connect() as connection:
        message_count = connection.execute(text("SELECT COUNT(*) FROM gmail_messages")).scalar()
        job_count = connection.execute(text("SELECT COUNT(*) FROM jobs")).scalar()
    assert message_count == 1
    assert job_count == 1  # sibling data untouched by the reference-tokens table drop

    upgrade(cfg, "head")
    assert _alembic_current_revision(engine) == "805108385946"
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "job_reference_tokens" in inspector.get_table_names()


# ---------------------------------------------------------------------------
# Round 3 remediation: R3-003 (migration must not import mutable runtime
# business code) and R3-004 (false "ERENCE" reference token from "No
# reference").
# ---------------------------------------------------------------------------


def _load_job_reference_tokens_migration_module():
    """Loads `805108385946_job_reference_tokens.py` as a standalone module
    by file path (its filename isn't a valid dotted import path) so tests
    can exercise its self-contained `_extract_reference_tokens` directly —
    used to prove migration/runtime extraction parity (R3-003's
    "MIGRATION BACKFILL CONSISTENCY" requirement) without ever importing
    it FROM the migration file itself (that's the exact thing R3-003
    forbids the other direction).
    """
    import importlib.util

    path = PROJECT_ROOT / "alembic" / "versions" / "805108385946_job_reference_tokens.py"
    spec = importlib.util.spec_from_file_location("_migration_805108385946", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_job_reference_tokens_migration_does_not_import_runtime_matching_module() -> None:
    """R3-003: the migration file must never contain an `import`/`from`
    statement referencing `app.*` (mentioning the module NAME in prose,
    to explain why it's deliberately not imported, is fine and expected)
    — it must be self-contained, using only stable infrastructure
    (alembic, sqlalchemy, stdlib).
    """
    import ast

    path = PROJECT_ROOT / "alembic" / "versions" / "805108385946_job_reference_tokens.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert not any(name == "app" or name.startswith("app.") for name in imported_modules), (
        f"migration must not import runtime app.* code, found: {imported_modules}"
    )


def test_job_reference_tokens_migration_survives_runtime_extractor_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """R3-003 required replay test: even if the RUNTIME extractor
    (`app.services.email_matching.extract_reference_tokens`) is broken,
    the migration's own backfill must still succeed — proving it never
    actually depends on that runtime function at all.
    """

    def _boom(*_args, **_kwargs):
        raise RuntimeError("runtime extractor is broken (simulated)")

    monkeypatch.setattr("app.services.email_matching.extract_reference_tokens", _boom)

    db_path = tmp_path / "migrations_job_reference_tokens_runtime_broken.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "847b7f5c87d8")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    fingerprint, source, title, company, location, url, description,
                    skills_json, score, recommendation, status, first_seen_at, last_seen_at
                ) VALUES (
                    'runtime-broken-fp', 'test', 'Job-ID: ABC123', 'Acme GmbH', 'Berlin',
                    'https://acme.example.com/jobs/482173', '', '[]', 80, 'APPLY', 'NEW',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    # Must NOT raise, despite the runtime extractor being broken above.
    upgrade(cfg, "head")
    assert _alembic_current_revision(engine) == "805108385946"

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT token FROM job_reference_tokens")).fetchall()
    tokens = {token for (token,) in rows}
    assert "ABC123" in tokens
    assert "482173" in tokens


def test_job_reference_tokens_backfill_produces_zero_tokens_for_no_reference(
    tmp_path: Path,
) -> None:
    """R3-004: the migration backfill must agree with the fixed parser —
    a job titled literally "No reference" must never backfill a false
    "ERENCE" token.
    """
    db_path = tmp_path / "migrations_job_reference_tokens_no_reference.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "847b7f5c87d8")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO jobs (
                    fingerprint, source, title, company, location, url, description,
                    skills_json, score, recommendation, status, first_seen_at, last_seen_at
                ) VALUES (
                    'no-reference-fp', 'test', 'No reference', 'Acme GmbH', 'Berlin',
                    'https://acme.example.com/jobs', '', '[]', 80, 'APPLY', 'NEW',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    upgrade(cfg, "head")

    with engine.connect() as connection:
        job_id = connection.execute(
            text("SELECT id FROM jobs WHERE fingerprint = 'no-reference-fp'")
        ).scalar_one()
        rows = connection.execute(
            text("SELECT token FROM job_reference_tokens WHERE job_id = :job_id"),
            {"job_id": job_id},
        ).fetchall()
    assert rows == []


def test_job_reference_tokens_migration_local_extractor_matches_runtime_on_corpus() -> None:
    """R3-003's "MIGRATION BACKFILL CONSISTENCY" requirement: the
    migration-local `_extract_reference_tokens` and the runtime
    `app.services.email_matching.extract_reference_tokens` must produce
    IDENTICAL results across a corpus covering every Stage 7B-supported
    case, so the self-contained duplication in the migration (R3-003)
    hasn't silently drifted from the runtime semantics it was frozen from.
    """
    from app.services.email_matching import extract_reference_tokens as runtime_extract

    migration_module = _load_job_reference_tokens_migration_module()
    migration_extract = migration_module._extract_reference_tokens

    corpus = [
        ("", "https://acme.example.com/jobs/12345"),  # valid numeric path
        ("", "https://acme.example.com/jobs/ABC123"),  # valid alphanumeric path
        ("Job-ID: ABC123", ""),  # valid labelled reference
        ("No reference", ""),  # invalid normal prose
        ("conference", ""),
        ("preference", ""),
        ("referencecheck", ""),
        ("Referenznummer: XYZ999", ""),
        ("Stellen-Nr.: QRS777", ""),
        ("Kennziffer: 42424242", ""),
        (
            "Job-ID: ABC123 and also Referenz-Nr: ABC123 again",
            "",
        ),  # duplicates collapse to one token
        (
            "Job-ID: ABC123, Referenz-Nr: XYZ999, see https://acme.example.com/jobs/12345",
            "https://acme.example.com/jobs/12345",
        ),  # multiple valid tokens across text + url
    ]

    for text_value, url_value in corpus:
        assert migration_extract(text_value, url_value) == runtime_extract(text_value, url_value), (
            f"mismatch for text={text_value!r} url={url_value!r}"
        )
