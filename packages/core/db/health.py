from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .urls import db_get_url


async def db_check_health() -> bool:
    url = db_get_url()
    if not url:
        return False
    engine: AsyncEngine = create_async_engine(url, echo=False, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()
