"""Generic database repository.

Provides model-agnostic CRUD helpers.  All modules should use
these instead of directly manipulating SQLAlchemy sessions.

Usage:

    from packages.core.db.repository import db_get, db_list, db_save, db_delete

    creator = await db_get(session, Creator, 1)
    creators = await db_list(session, Creator)
    await db_save(session, my_creator)
    await db_delete(session, old_task)
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

ModelT = TypeVar("ModelT", bound=DeclarativeBase)


async def db_get(session: AsyncSession, model: type[ModelT], id: Any) -> ModelT | None:
    """Fetch a row by primary key."""
    return await session.get(model, id)


async def db_get_by(session: AsyncSession, model: type[ModelT], **filters: Any) -> ModelT | None:
    """Fetch the first row matching the given filters."""
    stmt = select(model).filter_by(**filters).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def db_list(session: AsyncSession, model: type[ModelT]) -> list[ModelT]:
    """Fetch all rows for the given model."""
    result = await session.execute(select(model))
    return list(result.scalars().all())


async def db_save(session: AsyncSession, instance: ModelT) -> None:
    """Add or update a row and flush."""
    session.add(instance)
    await session.flush()


async def db_delete(session: AsyncSession, instance: ModelT) -> None:
    """Delete a row and flush."""
    await session.delete(instance)
    await session.flush()
