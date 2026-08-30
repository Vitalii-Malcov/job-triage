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
