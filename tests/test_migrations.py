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
        "thread_key",
        "subject",
        "created_at",
        "updated_at",
    }

    message_columns = {col["name"] for col in inspector.get_columns("gmail_messages")}
    assert message_columns == {
        "id",
        "thread_id",
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
    assert any(uc["column_names"] == ["thread_key"] for uc in thread_unique)

    message_unique = inspector.get_unique_constraints("gmail_messages")
    assert any(
        set(uc["column_names"]) == {"mailbox", "uid_validity", "uid"} for uc in message_unique
    )

    message_indexes = {
        tuple(idx["column_names"]) for idx in inspector.get_indexes("gmail_messages")
    }
    assert ("thread_id",) in message_indexes
    assert ("message_id_header",) in message_indexes


def test_gmail_messages_rejects_duplicate_provider_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "migrations_gmail_inbox_unique.db"
    cfg = _alembic_config(db_path)
    upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO gmail_threads (thread_key, subject, created_at, updated_at)
                VALUES ('<root@example.com>', 'Hello', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO gmail_messages (
                    thread_id, mailbox, uid_validity, uid, references_json,
                    to_addresses_json, cc_addresses_json, subject, received_at,
                    direction, body_plain, body_truncated, has_html, attachments_json,
                    created_at
                ) VALUES (
                    1, 'INBOX', 100, 1, '[]', '[]', '[]', 'Hello', CURRENT_TIMESTAMP,
                    'INBOUND', 'hi', 0, 0, '[]', CURRENT_TIMESTAMP
                )
                """
            )
        )

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO gmail_messages (
                        thread_id, mailbox, uid_validity, uid, references_json,
                        to_addresses_json, cc_addresses_json, subject, received_at,
                        direction, body_plain, body_truncated, has_html, attachments_json,
                        created_at
                    ) VALUES (
                        1, 'INBOX', 100, 1, '[]', '[]', '[]', 'Hello again', CURRENT_TIMESTAMP,
                        'INBOUND', 'hi again', 0, 0, '[]', CURRENT_TIMESTAMP
                    )
                    """
                )
            )


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
    # Downgrading one step must not touch the previous (Application
    # Package Reviews) migration's own tables.
    assert "application_package_reviews" in tables

    upgrade(cfg, "head")

    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    tables = set(inspector.get_table_names())
    assert "gmail_threads" in tables
    assert "gmail_messages" in tables
