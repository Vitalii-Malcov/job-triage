from pathlib import Path

from alembic.command import downgrade, upgrade
from alembic.config import Config
from sqlalchemy import create_engine, inspect

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
