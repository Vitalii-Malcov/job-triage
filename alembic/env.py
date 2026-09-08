from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from app.db.models import JobRecord, UserProfile  # noqa: F401  (registers tables on Base.metadata)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# disable_existing_loggers=False: fileConfig() defaults to True, which sets
# `.disabled = True` on every Logger object that already exists at call time
# but isn't named in alembic.ini's [loggers] section -- e.g. app.scheduler,
# app.services.scheduler when a caller runs migrations programmatically
# (tests/test_migrations.py, run_migrations_if_enabled() at app startup)
# after those loggers were already created. That disabled state outlives
# this function and silently drops all further logging from those loggers.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Inject the application's database URL instead of hardcoding it in alembic.ini,
# so migrations always target whatever DATABASE_URL the app is configured with.
# A URL already set on the Config object (e.g. by tests or other programmatic
# callers) takes precedence over the app settings default.
db_url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
config.set_main_option("sqlalchemy.url", db_url)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
