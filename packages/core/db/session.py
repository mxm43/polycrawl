from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine



def get_async_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, echo=False, future=True)


def get_async_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    engine = get_async_engine(database_url)
    return async_sessionmaker(engine, expire_on_commit=False)
