from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from packages.core.db.base import Base
from packages.core.db.models import Account, Artifact, Creator, Task, TaskRun
from services.worker import runtime as worker_runtime
from services.worker.tasks import content_fetch

DB_URL = os.getenv("POLYCRAWL_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="POLYCRAWL_TEST_DATABASE_URL is not set; integration test requires PostgreSQL",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _reset_db(database_url: str) -> None:
    engine = create_async_engine(database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def _seed_task(database_url: str) -> tuple[uuid.UUID, int]:
    engine = create_async_engine(database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        creator = Creator(creator_key="creator_inttest", display_name="integration")
        session.add(creator)
        await session.flush()

        account = Account(
            creator_id=creator.id,
            platform="douyin",
            account_type="profile",
            account_url="https://www.douyin.com/user/integration",
        )
        session.add(account)
        await session.flush()

        task = Task(
            account_id=account.id,
            task_type="content_fetch",
            status="queued",
            params={},
        )
        session.add(task)
        await session.flush()

        run = TaskRun(task_id=task.id, run_number=1, status="queued", started_at=_now())
        session.add(run)

        await session.commit()
        task_id = task.id
        account_id = account.id

    await engine.dispose()
    return task_id, account_id


async def _read_state(database_url: str, task_id: uuid.UUID) -> tuple[Task, TaskRun, list[Artifact]]:
    engine = create_async_engine(database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        task = await session.get(Task, task_id)
        run_result = await session.execute(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.run_number.desc()).limit(1)
        )
        run = run_result.scalars().first()

        artifacts_result = await session.execute(select(Artifact).where(Artifact.task_id == task_id))
        artifacts = artifacts_result.scalars().all()

    await engine.dispose()
    assert task is not None
    assert run is not None
    return task, run, artifacts


def test_content_pipeline_integration_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DB_URL is not None

    monkeypatch.setattr(worker_runtime, "db_get_url", lambda: DB_URL)

    asyncio.run(_reset_db(DB_URL))
    task_id, account_id = asyncio.run(_seed_task(DB_URL))

    result1 = content_fetch(str(task_id), account_id)
    assert result1["status"] == "success"
    assert result1["items_downloaded"] >= 1

    task, run, artifacts = asyncio.run(_read_state(DB_URL, task_id))
    assert task.status == "success"
    assert run.status == "success"
    assert run.items_downloaded >= 1
    assert len(artifacts) >= 1

    first_artifact_count = len(artifacts)

    result2 = content_fetch(str(task_id), account_id)
    assert result2["items_skipped"] >= 1

    _, run2, artifacts2 = asyncio.run(_read_state(DB_URL, task_id))
    assert run2.items_skipped is not None
    assert run2.items_skipped >= 1
    # Dedup: artifacts count stays unchanged on re-run
    assert len(artifacts2) == first_artifact_count
