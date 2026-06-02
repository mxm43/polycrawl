from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.config import ConfigLoader
from packages.core.db import get_async_session_factory
from packages.core.db.models import Task, TaskRun
from packages.core.events import publish_event

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def _resolve_database_url() -> str:
    state = ConfigLoader(CONFIG_DIR).load_all()
    return state.base.storage.database_url


def get_media_root() -> Path:
    state = ConfigLoader(CONFIG_DIR).load_all()
    raw = state.base.storage.media_base_path
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    return p


def get_worker_session_factory() -> async_sessionmaker[AsyncSession]:
    return get_async_session_factory(_resolve_database_url())


def _get_redis_url() -> str:
    return ConfigLoader(CONFIG_DIR).load_all().base.storage.redis_url


async def mark_task_running(task_id: str) -> None:
    session_factory = get_worker_session_factory()
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return

        task.status = "running"
        task.started_at = datetime.utcnow()

        run = await _latest_task_run(session, task.id)
        if run is not None:
            run.status = "running"
            run.started_at = datetime.utcnow()

        await session.commit()
        publish_event(_get_redis_url(), "task_updated", {"task_id": task_id, "status": "running"})


async def mark_task_success(task_id: str) -> None:
    await mark_task_success_with_metrics(task_id)


async def mark_task_success_with_metrics(
    task_id: str,
    *,
    items_fetched: int | None = None,
    items_downloaded: int | None = None,
    items_skipped: int | None = None,
    bytes_downloaded: int | None = None,
) -> None:
    session_factory = get_worker_session_factory()
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return

        now = datetime.utcnow()
        task.status = "success"
        task.completed_at = now

        run = await _latest_task_run(session, task.id)
        if run is not None:
            run.status = "success"
            run.completed_at = now
            if run.started_at is not None:
                run.duration_seconds = Decimal(str((now - run.started_at).total_seconds()))
            if items_fetched is not None:
                run.items_fetched = items_fetched
            if items_downloaded is not None:
                run.items_downloaded = items_downloaded
            if items_skipped is not None:
                run.items_skipped = items_skipped
            if bytes_downloaded is not None:
                run.bytes_downloaded = bytes_downloaded

        await session.commit()
        publish_event(_get_redis_url(), "task_updated", {"task_id": task_id, "status": "success"})


async def mark_task_failed(task_id: str, error_message: str) -> None:
    session_factory = get_worker_session_factory()
    async with session_factory() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return

        now = datetime.utcnow()
        task.status = "failed"
        task.completed_at = now
        task.error_message = error_message

        run = await _latest_task_run(session, task.id)
        if run is not None:
            run.status = "failed"
            run.error_message = error_message
            run.completed_at = now
            if run.started_at is not None:
                run.duration_seconds = Decimal(str((now - run.started_at).total_seconds()))

        await session.commit()
        publish_event(_get_redis_url(), "task_updated", {"task_id": task_id, "status": "failed"})


async def _latest_task_run(session: AsyncSession, task_id: object) -> TaskRun | None:
    query = select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.run_number.desc()).limit(1)
    result = await session.execute(query)
    return result.scalars().first()
