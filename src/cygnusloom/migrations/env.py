from __future__ import annotations

import os
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import cygnusloom.db.models  # noqa: F401  (registers the tables)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

url = os.environ.get("CYGNUSLOOM_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if not url:
    raise RuntimeError("CYGNUSLOOM_DATABASE_URL is required")
config.set_main_option("sqlalchemy.url", url)

target_metadata = SQLModel.metadata

# Alembic's version table stores the revision id in VARCHAR(32). A released revision id that
# was longer could already be recorded on databases whose backend did not enforce the limit,
# such as SQLite. Rename it before Alembic resolves the revision tree.
_LEGACY_REVISIONS = {
    "0012_remove_campaign_game_version": "0012_remove_game_version",
}


def _rename_legacy_revisions(connection: sa.Connection) -> None:
    """Move an old revision id to its shortened spelling, if the version table has it."""
    if not sa.inspect(connection).has_table("alembic_version"):
        return
    for old, new in _LEGACY_REVISIONS.items():
        connection.execute(
            sa.text("UPDATE alembic_version SET version_num = :new WHERE version_num = :old"),
            {"new": new, "old": old},
        )


def run_migrations_offline() -> None:
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _rename_legacy_revisions(connection)
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
