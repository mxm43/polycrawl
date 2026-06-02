from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from packages.core.config import ConfigLoader
from packages.core.db.base import Base
from packages.core.db import models  # noqa: F401

config = context.config
ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


def _resolve_db_url() -> str:
    # Allow env var override for migration tooling (e.g. postgres -> localhost)
    env_url = os.environ.get("POLYCRAWL_DATABASE_URL")
    if env_url:
        configured = env_url
    else:
        configured = ConfigLoader(CONFIG_DIR).load_all().base.storage.database_url
    return configured.replace("+asyncpg", "+psycopg")


def run_migrations_offline() -> None:
    url = _resolve_db_url()
    context.configure(
        url=url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _resolve_db_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
