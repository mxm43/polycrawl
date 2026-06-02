from __future__ import annotations

from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from packages.core.db.base import Base
from packages.core.db import models  # noqa: F401

config = context.config
ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


def _resolve_db_url() -> str:
    from packages.core.db.urls import db_get_url
    url = db_get_url()
    return url.replace("+asyncpg", "+psycopg") if url else ""


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
