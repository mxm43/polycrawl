from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .urls import db_get_url


def _ensure_url() -> str:
    url = db_get_url()
    if not url:
        raise RuntimeError("POLYCRAWL_DATABASE_URL is not set")
    return url


def get_async_engine() -> AsyncEngine:
    return create_async_engine(_ensure_url(), echo=False, future=True)


def db_get_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = get_async_engine()
    return async_sessionmaker(engine, expire_on_commit=False)
