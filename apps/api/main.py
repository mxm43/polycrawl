from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from packages.core.db import db_check_health, db_get_session_factory
from packages.core.db.redis_client import redis_sync

from apps.api.schemas import (
    AccountResponse,
    AccountScheduledRequest,
    CreatorCreateRequest,
    CreatorLinkResponse,
    CreatorPlatformGroupResponse,
    CreatorResponse,
    CreatorSummaryResponse,
    HealthResponse,
    LinkCreateRequest,
    LiveStatusResponse,
    ScheduleEntryResponse,
    ScheduleUpdateRequest,
    SchedulesResponse,
    StopLiveRecordResponse,
    TaskCreateRequest,
    TaskRetryResponse,
    TaskResponse,
)
from packages.core.config import ConfigLoader
from packages.core.config.creator_keys import DuplicateCreatorKeyError, ensure_no_duplicates, generate_creator_key
from packages.core.config.models import parse_duration_to_seconds
from packages.core.config.jsonc import load_jsonc, update_jsonc_key
from packages.core.config.cookie_verify import get_verify_state, invalidate as _invalidate_cookies, set_verified, set_saved_at as _set_saved_at, get_saved_at as _get_saved_at
from packages.core.config.models import (
    AccountConfig,
    CreatorConfig,
    CreatorsFile,
    ScheduleEntry,
    SchedulesConfig,
)
from packages.core.config.watcher import watch_config_dir
from packages.core.db import db_get_session_factory
from packages.core.db import db_check_health
from packages.core.events import publish_event, subscribe_to_events, subscribe_ws_events, unsubscribe_ws_events
from packages.core.db.models import Account, Artifact, Creator, LiveStatus, Task, TaskRun
from packages.core.logging import read_log_file, setup_logging, subscribe_to_logs, subscribe_ws, unsubscribe_ws
from packages.core.sync import sync_creators_to_db
from packages.core.utils import now_utc_naive
from redis import Redis

logger = logging.getLogger(__name__)

app = FastAPI(title="PolyCrawl API", version="0.1.0")

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
WEB_DIR = ROOT / "apps" / "web"
loader = ConfigLoader(CONFIG_DIR)
session_factory: async_sessionmaker[AsyncSession] | None = None
_config_observer: Any = None



if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="web-assets")


def _push_to_task_queue(task_type: str, task_id: str, account_id: int, params: dict | None = None) -> str | None:
    """Push a task message to the matching task_{idx} Redis queue.
    Returns the queue key name, or None if no matching queue found.
    """
    state = loader.load_all()
    with redis_sync() as r:
        for idx, entry in enumerate(state.base.schedules):
            if not entry.enabled:
                continue
            if entry.type == task_type:
                queue_key = f"task_{idx}"
                msg = json.dumps({
                    "task_id": task_id,
                    "account_id": account_id,
                    "task_type": task_type,
                    "params": params or {},
                })
                r.rpush(queue_key, msg)
                return queue_key
        return None


async def _reload_config() -> None:
    """Reload config from disk and sync creators to DB."""
    global session_factory
    try:
        state = loader.load_all()
        redis_url = redis_get_url()
        if session_factory is not None:
            await sync_creators_to_db(session_factory, state.creators)
        if redis_url:
            publish_event(redis_url, "creators_updated", {})
    except Exception as exc:
        logger.warning("Config reload failed (kept previous): %s", exc)


@app.on_event("startup")
async def startup_load_config() -> None:
    global session_factory, _config_observer
    state = loader.load_all()
    session_factory = db_get_session_factory()
    await sync_creators_to_db(session_factory, state.creators)

    # File-system watcher (no polling)
    config_queue, _config_observer = await watch_config_dir(CONFIG_DIR)
    asyncio.create_task(_config_event_processor(config_queue))

    # Initialize logging system with Redis Pub/Sub
    log_dir = CONFIG_DIR.parent / state.base.global_config.get("data_dir", "data") / "logs"
    log_level = state.base.global_config.get("log_level", "INFO")
    setup_logging(log_dir=log_dir, log_level=log_level)
    # Start background Redis 鈫?WebSocket subscribers
    asyncio.create_task(subscribe_to_logs())
    asyncio.create_task(subscribe_to_events())
    logger.info("API started 鈥?config watcher + scheduler active")


async def _config_event_processor(queue: asyncio.Queue[str]) -> None:
    """Consume file-system modification events and reload config (debounced)."""
    while True:
        try:
            await queue.get()
            # Debounce: drain any queued events within 500ms
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=0.5)
                except (asyncio.TimeoutError, asyncio.QueueEmpty):
                    break
            await _reload_config()
        except Exception:
            await asyncio.sleep(1)


@app.on_event("shutdown")
async def shutdown_app() -> None:
    global _config_observer
    if _config_observer:
        _config_observer.stop()
        _config_observer.join(timeout=5)
        _config_observer = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    if session_factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    async with session_factory() as session:
        yield session


def _task_to_response(task: Task) -> TaskResponse:
    return TaskResponse(
        id=str(task.id),
        account_id=task.account_id,
        task_type=task.task_type,
        status=task.status,
        params=task.params,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        queue_key=task.queue_key,
    )


def _signal_stop_live_record(account_id: int) -> None:
    """Set a Redis key to signal the worker to stop recording this account.

    The worker's ``_background_record`` monitors this key and stops the
    download when it appears.
    """
    try:
        from redis import Redis
        r = Redis.from_url(redis_get_url(), decode_responses=True)
        r.setex(f"polycrawl:live:stop:{account_id}", 60, "1")
        r.close()
    except Exception:
        logger.exception("[api] failed to signal stop for account %d", account_id)


def _work_key_from_content_id(content_id: object) -> str:
    raw = str(content_id or "").strip()
    if not raw:
        return ""
    return re.sub(r"_(?:img|vid)\d+$", "", raw)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    try:
        state = loader.load_all()
    except Exception:
        return HealthResponse(status="degraded", config="error", database="unknown")

    db_ok = await db_check_health()
    if db_ok:
        return HealthResponse(status="ok", config="ok", database="ok")
    return HealthResponse(status="degraded", config="ok", database="error")


@app.get("/creators", response_model=list[CreatorResponse])
def list_creators() -> list[CreatorResponse]:
    state = loader.load_all()
    return [
        CreatorResponse(creator_key=c.creator_key or "", display_name=c.display_name, tags=c.tags)
        for c in state.creators.creators
    ]


@app.get("/config/effective")
def get_effective_config() -> dict[str, object]:
    state = loader.load_all()
    return state.model_dump(by_alias=True)


@app.get("/accounts", response_model=list[AccountResponse])
async def list_accounts(session: AsyncSession = Depends(get_db_session)) -> list[AccountResponse]:
    result = await session.execute(
        select(Account, Creator.display_name)
        .join(Creator, Creator.id == Account.creator_id)
        .order_by(Account.id.asc())
        .limit(1000)
    )
    rows = result.all()
    return [
        AccountResponse(
            id=account.id,
            creator_id=account.creator_id,
            creator_display_name=display_name,
            platform=account.platform,
            account_type=account.account_type,
            account_url=account.account_url,
            account_alias=account.account_alias,
            scheduled=account.scheduled if account.scheduled is not None else True,
        )
        for account, display_name in rows
    ]


@app.get("/creators/summary", response_model=list[CreatorSummaryResponse])
async def list_creator_summaries(
    session: AsyncSession = Depends(get_db_session),
) -> list[CreatorSummaryResponse]:
    # Default order: match config file order. Fall back to display_name for orphaned records.
    config_order: dict[str, int] = {}
    try:
        for idx, c in enumerate(loader.load_all().creators.creators):
            if c.creator_key:
                config_order[c.creator_key] = idx
    except Exception:
        pass

    creators_result = await session.execute(select(Creator))
    all_creators = list(creators_result.scalars().all())
    creators = sorted(all_creators, key=lambda c: config_order.get(c.creator_key, 999999))
    if not creators:
        return []

    creator_ids = [creator.id for creator in creators]

    accounts_result = await session.execute(
        select(Account).where(Account.creator_id.in_(creator_ids)).order_by(Account.creator_id.asc(), Account.id.asc())
    )
    accounts = accounts_result.scalars().all()

    artifact_result = await session.execute(
        select(Account.creator_id, Artifact.account_id, Artifact.file_size, Artifact.publish_date, Artifact.content_id)
        .join(Account, Account.id == Artifact.account_id)
        .where(Account.creator_id.in_(creator_ids))
    )
    artifact_rows = artifact_result.all()

    accounts_by_creator: dict[int, list[Account]] = defaultdict(list)
    for account in accounts:
        accounts_by_creator[account.creator_id].append(account)

    stat_by_creator: dict[int, dict[str, object]] = defaultdict(
        lambda: {"files_count": 0, "works_count": 0, "total_bytes": 0, "last_updated_at": None}
    )
    last_updated_by_account: dict[int, datetime | None] = defaultdict(lambda: None)
    work_keys_by_creator: dict[int, set[str]] = defaultdict(set)

    for creator_id, account_id, file_size, publish_date, content_id in artifact_rows:
        stat = stat_by_creator[creator_id]
        stat["files_count"] = int(stat["files_count"]) + 1
        stat["total_bytes"] = int(stat["total_bytes"]) + max(int(file_size or 0), 0)

        work_key = _work_key_from_content_id(content_id)
        if work_key and work_key not in work_keys_by_creator[creator_id]:
            work_keys_by_creator[creator_id].add(work_key)
            stat["works_count"] = int(stat["works_count"]) + 1

        if publish_date is None:
            continue

        prev_time = stat["last_updated_at"]
        if prev_time is None or publish_date > prev_time:
            stat["last_updated_at"] = publish_date

        prev_account_time = last_updated_by_account.get(account_id)
        if prev_account_time is None or publish_date > prev_account_time:
            last_updated_by_account[account_id] = publish_date

    result: list[CreatorSummaryResponse] = []
    # Build tags lookup from config
    creator_tags: dict[str, list[str]] = {}
    try:
        for c in loader.load_all().creators.creators:
            if c.creator_key:
                creator_tags[c.creator_key] = c.tags
    except Exception:
        pass

    for creator in creators:
        creator_accounts = accounts_by_creator.get(creator.id, [])

        grouped_links: dict[str, list[CreatorLinkResponse]] = defaultdict(list)
        for account in creator_accounts:
            grouped_links[account.platform].append(
                CreatorLinkResponse(
                    platform=account.platform,
                    account_type=account.account_type,
                    account_url=account.account_url,
                    account_alias=account.account_alias,
                    last_updated_at=last_updated_by_account.get(account.id),
                )
            )

        platform_groups = [
            CreatorPlatformGroupResponse(platform=platform, links=links)
            for platform, links in sorted(grouped_links.items(), key=lambda x: x[0])
        ]

        platforms = [group.platform for group in platform_groups]
        stat = stat_by_creator.get(creator.id, {})

        result.append(
            CreatorSummaryResponse(
                creator_key=creator.creator_key,
                display_name=creator.display_name,
                tags=creator_tags.get(creator.creator_key, []),
                platforms=platforms,
                files_count=int(stat.get("files_count", 0)),
                works_count=int(stat.get("works_count", 0)),
                downloads_count=int(stat.get("files_count", 0)),
                total_bytes=int(stat.get("total_bytes", 0)),
                last_updated_at=stat.get("last_updated_at"),
                platform_groups=platform_groups,
            )
        )

    return result


@app.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(session: AsyncSession = Depends(get_db_session)) -> list[TaskResponse]:
    result = await session.execute(select(Task).order_by(Task.created_at.desc()).limit(100))
    tasks = result.scalars().all()
    return [_task_to_response(task) for task in tasks]


@app.post("/tasks", response_model=TaskResponse)
async def create_task(
    req: TaskCreateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TaskResponse:
    if req.account_id is None:
        raise HTTPException(status_code=400, detail="account_id is required")

    account = await session.get(Account, req.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account not found: {req.account_id}")

    task = Task(
        account_id=req.account_id,
        task_type=req.task_type,
        status="pending",
        params=req.params,
        max_retries=req.max_retries,
    )

    try:
        session.add(task)
        await session.flush()

        queue_key = _push_to_task_queue(req.task_type, str(task.id), req.account_id, req.params)

        task.status = "queued"
        task.queue_key = queue_key or ""

        task_run = TaskRun(task_id=task.id, run_number=1, status="queued")
        session.add(task_run)

        await session.commit()
        await session.refresh(task)

        publish_event("task_created", {"task_id": str(task.id)})

        if queue_key is None:
            logger.warning("No matching schedule entry for task_type=%s, task queued but not dispatched", req.task_type)

        return _task_to_response(task)
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create task: {exc}") from exc


@app.post("/tasks/{task_id}/retry", response_model=TaskRetryResponse)
async def retry_task(task_id: str, session: AsyncSession = Depends(get_db_session)) -> TaskRetryResponse:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    if task.retry_count >= task.max_retries:
        raise HTTPException(status_code=400, detail="max_retries reached")

    if task.account_id is None:
        raise HTTPException(status_code=400, detail="task has no account_id")

    task.retry_count += 1
    task.status = "retrying"
    task.started_at = None
    task.completed_at = None
    task.error_message = None

    run_result = await session.execute(
        select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.run_number.desc()).limit(1)
    )
    last_run = run_result.scalars().first()
    next_run_number = 1 if last_run is None else last_run.run_number + 1

    task_run = TaskRun(task_id=task.id, run_number=next_run_number, status="queued")
    session.add(task_run)

    queue_key = _push_to_task_queue(task.task_type, str(task.id), task.account_id, task.params)
    task.status = "queued"
    task.queue_key = queue_key or ""

    await session.commit()
    return TaskRetryResponse(
        id=str(task.id),
        retry_count=task.retry_count,
        status=task.status,
        queue_key=task.queue_key,
    )


@app.get("/live/status", response_model=list[LiveStatusResponse])
async def live_status(session: AsyncSession = Depends(get_db_session)) -> list[LiveStatusResponse]:
    result = await session.execute(
        select(LiveStatus, Account.creator_id, Creator.creator_key, Creator.display_name)
        .join(Account, LiveStatus.account_id == Account.id)
        .join(Creator, Account.creator_id == Creator.id)
        .order_by(LiveStatus.updated_at.desc(), Creator.display_name.asc())
        .limit(500)
    )
    rows = result.all()
    return [
        LiveStatusResponse(
            account_id=row.LiveStatus.account_id,
            creator_key=row.creator_key,
            display_name=row.display_name,
            status=row.LiveStatus.status,
            status_since=row.LiveStatus.status_since,
            updated_at=row.LiveStatus.updated_at,
            error_message=row.LiveStatus.error_message,
        )
        for row in rows
    ]


@app.post("/live/records/{account_id}/stop", response_model=StopLiveRecordResponse)
async def stop_live_record(
    account_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> StopLiveRecordResponse:
    query = (
        select(Task)
        .where(
            Task.account_id == account_id,
            Task.task_type == "live_record",
            Task.status.in_(["queued", "running", "retrying"]),
        )
        .order_by(Task.created_at.desc())
        .limit(1)
    )
    result = await session.execute(query)
    task = result.scalars().first()

    if task is None:
        return StopLiveRecordResponse(account_id=account_id, stopped=False, detail="no active live_record task")

    if task.queue_key:
        _signal_stop_live_record(account_id)

    task.status = "canceled"
    task.completed_at = now_utc_naive()
    task.error_message = "stopped by user"

    run_result = await session.execute(
        select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.run_number.desc()).limit(1)
    )
    run = run_result.scalars().first()
    if run is not None:
        run.status = "canceled"
        run.error_message = "stopped by user"
        run.completed_at = task.completed_at

    await session.commit()
    return StopLiveRecordResponse(account_id=account_id, stopped=True, detail="live_record task canceled")


@app.post("/creators", response_model=CreatorResponse)
async def create_creator(req: CreatorCreateRequest) -> CreatorResponse:
    creators_path = CONFIG_DIR / "creators.jsonc"
    raw = load_jsonc(creators_path)
    creators_file = CreatorsFile.model_validate(raw)

    provided_keys = [c.creator_key for c in creators_file.creators if c.creator_key]
    try:
        ensure_no_duplicates([k for k in provided_keys if k])
    except DuplicateCreatorKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing = set(provided_keys)
    creator_key = generate_creator_key(existing)

    creators_file.creators.append(
        CreatorConfig(
            creator_key=creator_key,
            display_name=req.display_name,
            accounts=[],
        )
    )

    payload = creators_file.model_dump(exclude_none=True)
    update_jsonc_key(creators_path, "creators", payload["creators"], indent_shift=2)

    if session_factory is not None:
        await sync_creators_to_db(session_factory, creators_file)

    return CreatorResponse(creator_key=creator_key, display_name=req.display_name, tags=[])


# ---------------------------------------------------------------------------
# Creator tags
# ---------------------------------------------------------------------------


class _TagsUpdateRequest(BaseModel):
    tags: list[str] = Field(default_factory=list)


@app.patch("/creators/{creator_key}/tags", response_model=CreatorResponse)
async def update_creator_tags(creator_key: str, req: _TagsUpdateRequest) -> CreatorResponse:
    """Replace tags for a creator. Writes back to creators.jsonc."""
    creators_path = CONFIG_DIR / "creators.jsonc"
    raw = load_jsonc(creators_path)
    cf = CreatorsFile.model_validate(raw)

    found = None
    for creator in cf.creators:
        if creator.creator_key == creator_key:
            creator.tags = req.tags
            found = creator
            break

    if found is None:
        raise HTTPException(status_code=404, detail=f"Creator not found: {creator_key}")

    payload = cf.model_dump(exclude_none=True)
    update_jsonc_key(creators_path, "creators", payload["creators"], indent_shift=2)

    return CreatorResponse(creator_key=creator_key, display_name=found.display_name, tags=found.tags)


# ---------------------------------------------------------------------------
# Schedule management
# ---------------------------------------------------------------------------

@app.get("/schedules", response_model=SchedulesResponse)
def get_schedules() -> SchedulesResponse:
    state = loader.load_all()
    schedules = state.base.schedules
    return SchedulesResponse(
        tasks=[ScheduleEntryResponse(
            type=t.type, enabled=t.enabled, strategy=t.strategy.model_dump() if t.strategy else {"use": "incremental"}, tag_filter=t.tag_filter,
            start_at=t.start_at, interval=t.interval,
        ) for t in schedules]
    )


@app.put("/schedules", response_model=SchedulesResponse)
def update_schedules(req: ScheduleUpdateRequest) -> SchedulesResponse:
    base_path = CONFIG_DIR / "base.jsonc"

    # Read existing data for merging (comments are stripped here but that's OK,
    # we only use it as reference 鈥?the actual file text is patched below)
    existing = load_jsonc(base_path)
    old_tasks: list[dict[str, Any]] = existing.get("tasks", []) or []

    # Merge: for each incoming task, start from the old task data,
    # then override with non-null values from the frontend.
    # This preserves fields the frontend doesn't know about (e.g. strategy.tick)
    # while removing fields the frontend explicitly set to null.
    merged_tasks: list[dict[str, Any]] = []
    raw_new_tasks = [t.model_dump(exclude_none=True) for t in req.tasks]
    for i, new_task in enumerate(raw_new_tasks):
        old_task = old_tasks[i] if i < len(old_tasks) else {}

        # Start from old task, apply non-null frontend values on top
        merged = dict(old_task)
        for k, v in new_task.items():
            if v is not None:
                merged[k] = v
            elif k in merged:
                del merged[k]

        # Deep-merge strategy: start from old, override with non-null new values
        old_strat = old_task.get("strategy") or {}
        new_strat = new_task.get("strategy") or {}
        merged_strat = dict(old_strat)
        for k, v in new_strat.items():
            if v is not None:
                merged_strat[k] = v
            elif k in merged_strat:
                del merged_strat[k]
        merged["strategy"] = merged_strat

        # Remove any trailing null values
        merged = {k: v for k, v in merged.items() if v is not None}

        merged_tasks.append(merged)

    # Text-based patching: replace only the "tasks" array in the raw file,
    # preserving all comments and other keys.
    update_jsonc_key(base_path, "tasks", merged_tasks, indent_shift=2)

    return get_schedules()


@app.patch("/accounts/{account_id}/scheduled", response_model=AccountResponse)
async def toggle_account_scheduled(
    account_id: int,
    req: AccountScheduledRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AccountResponse:
    account = await session.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    # Write back to creators.jsonc
    creators_path = CONFIG_DIR / "creators.jsonc"
    raw = load_jsonc(creators_path)
    creators_file = CreatorsFile.model_validate(raw)

    updated = False
    for creator in creators_file.creators:
        for acct in creator.accounts:
            for url_entry in acct.normalized_urls():
                if url_entry.url.strip().rstrip("?") == str(account.account_url).strip().rstrip("?"):
                    url_entry.enabled = req.scheduled
                    updated = True
                    break
            if updated:
                break
        if updated:
            break

    if updated:
        payload = creators_file.model_dump(exclude_none=True)
        update_jsonc_key(creators_path, "creators", payload["creators"], indent_shift=2)

    # Also update DB column for dispatcher queries
    account.scheduled = req.scheduled
    await session.commit()

    creator_result = await session.execute(
        select(Creator).where(Creator.id == account.creator_id)
    )
    creator = creator_result.scalars().first()

    return AccountResponse(
        id=account.id,
        creator_id=account.creator_id,
        creator_display_name=creator.display_name if creator else None,
        platform=account.platform,
        account_type=account.account_type,
        account_url=account.account_url,
        account_alias=account.account_alias,
        scheduled=account.scheduled,
    )


# ---------------------------------------------------------------------------
# Creator tags  # noqa: E266
# ---------------------------------------------------------------------------


@app.patch("/creators/{creator_key}/tags", response_model=CreatorResponse)
async def update_creator_tags(creator_key: str, req: CreatorCreateRequest) -> CreatorResponse:
    """Replace tags for a creator. Writes back to creators.jsonc."""
    # Parse the new tags from comma-separated input in display_name
    # We use a dedicated request model; for simplicity, tags are passed as JSON array.
    from pydantic import BaseModel as _BM
    class _TagsReq(_BM):
        tags: list[str]
    _tags_data = await req.json() if hasattr(req, 'json') else {}
    # Actually let's just read the request body
    import json
    body = json.loads(await req.body()) if hasattr(req, 'body') else {}
    new_tags = body.get('tags', [])
    
    creators_path = CONFIG_DIR / "creators.jsonc"
    raw = load_jsonc(creators_path)
    cf = CreatorsFile.model_validate(raw)

    for creator in cf.creators:
        if creator.creator_key == creator_key:
            creator.tags = new_tags
            break
    else:
        raise HTTPException(status_code=404, detail=f"Creator not found: {creator_key}")

    payload = cf.model_dump(exclude_none=True)
    update_jsonc_key(creators_path, "creators", payload["creators"], indent_shift=2)

    return CreatorResponse(creator_key=creator_key, display_name="", tags=new_tags)


# ---------------------------------------------------------------------------
# Link management
# ---------------------------------------------------------------------------

@app.post("/creators/{creator_key}/links")
async def add_creator_link(creator_key: str, req: LinkCreateRequest) -> dict[str, str]:
    """Add a new link to a creator. Writes back to creators.jsonc."""
    creators_path = CONFIG_DIR / "creators.jsonc"
    raw = load_jsonc(creators_path)
    cf = CreatorsFile.model_validate(raw)

    found = None
    for creator in cf.creators:
        if creator.creator_key == creator_key:
            found = creator
            break

    if found is None:
        raise HTTPException(status_code=404, detail=f"Creator not found: {creator_key}")

    # Check if URL already exists
    for acct in found.accounts:
        for url_entry in acct.normalized_urls():
            if url_entry.url.strip().rstrip("?") == req.account_url.strip().rstrip("?"):
                raise HTTPException(status_code=409, detail="Link already exists")

    from packages.core.config.models import AccountConfig, UrlEntry
    new_account = AccountConfig(
        platform=req.platform,
        type=req.account_type,
        account_url=[UrlEntry(url=req.account_url, enabled=True)],
    )
    if req.account_alias:
        new_account.account_alias = req.account_alias
    found.accounts.append(new_account)

    payload = cf.model_dump(exclude_none=True)
    update_jsonc_key(creators_path, "creators", payload["creators"], indent_shift=2)

    await _reload_config()
    return {"status": "ok", "detail": f"Link added to {creator_key}"}


@app.delete("/creators/{creator_key}/links")
async def remove_creator_link(creator_key: str, account_url: str = Query(...)) -> dict[str, str]:
    """Remove a link from a creator by URL. Writes back to creators.jsonc."""
    creators_path = CONFIG_DIR / "creators.jsonc"
    raw = load_jsonc(creators_path)
    cf = CreatorsFile.model_validate(raw)

    found = None
    for creator in cf.creators:
        if creator.creator_key == creator_key:
            found = creator
            break

    if found is None:
        raise HTTPException(status_code=404, detail=f"Creator not found: {creator_key}")

    target_url = account_url.strip().rstrip("?")
    before = len(found.accounts)
    found.accounts = [
        acct for acct in found.accounts
        if not any(
            url_entry.url.strip().rstrip("?") == target_url
            for url_entry in acct.normalized_urls()
        )
    ]

    if len(found.accounts) == before:
        raise HTTPException(status_code=404, detail="Link not found")

    payload = cf.model_dump(exclude_none=True)
    update_jsonc_key(creators_path, "creators", payload["creators"], indent_shift=2)

    await _reload_config()
    return {"status": "ok", "detail": f"Link removed from {creator_key}"}


# ---------------------------------------------------------------------------
# Creator reorder
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _ReorderModel

class _ReorderRequest(_ReorderModel):
    creator_keys: list[str]

@app.put("/creators/reorder")
async def reorder_creators(req: _ReorderRequest) -> dict[str, str]:
    """Reorder creators in creators.jsonc."""
    creators_path = CONFIG_DIR / "creators.jsonc"
    raw = load_jsonc(creators_path)
    cf = CreatorsFile.model_validate(raw)

    key_to_creator = {c.creator_key: c for c in cf.creators if c.creator_key}
    reordered = []
    for key in req.creator_keys:
        if key in key_to_creator:
            reordered.append(key_to_creator.pop(key))
    # Append any remaining (shouldn't happen, but safety)
    reordered.extend(key_to_creator.values())

    cf.creators = reordered
    payload = cf.model_dump(exclude_none=True)
    update_jsonc_key(creators_path, "creators", payload["creators"], indent_shift=2)

    await _reload_config()
    return {"status": "ok", "detail": "Creators reordered"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

@app.get("/logs")
def get_logs(
    limit: int = Query(default=200, le=500),
    level: str | None = Query(default=None),
) -> list[dict[str, str | None]]:
    return read_log_file(limit=limit, level=level)


@app.delete("/logs")
def clear_logs() -> dict[str, str]:
    """Truncate the shared log file."""
    from packages.core.logging import clear_log_file
    clear_log_file()
    return {"status": "cleared"}


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    await websocket.accept()
    q = subscribe_ws()
    try:
        while True:
            entry = await q.get()
            try:
                await websocket.send_json(entry)
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe_ws(q)


# ---------------------------------------------------------------------------
# Cookie management (manual configuration via web console)
# ---------------------------------------------------------------------------

from pydantic import BaseModel as _CookieModel

class _CookiesSaveRequest(_CookieModel):
    platform: str
    cookies: dict[str, str]

class _VerifyRequest(_CookieModel):
    platform: str


@app.post("/login/cookies")
def save_platform_cookies(req: _CookiesSaveRequest) -> dict[str, str]:
    """Save cookies for a platform's site config (e.g. xiaohongshu.jsonc)."""
    site_path = CONFIG_DIR / "sites" / f"{req.platform}.jsonc"
    if not site_path.exists():
        config: dict[str, object] = {"platform": {"cookies": {}}}
    else:
        config = load_jsonc(site_path)

    platform = config.setdefault("platform", {})
    platform.setdefault("cookies", {}).update(req.cookies)

    update_jsonc_key(site_path, "platform", config["platform"])
    # Clear Redis verify state so next status poll shows "unverified"
    from packages.core.config.cookie_verify import clear as _clear_verify
    _clear_verify(req.platform)
    # Record save timestamp in Redis (not in JSONC 鈥?runtime metadata)
    _set_saved_at(req.platform)
    return {"status": "ok", "detail": f"Cookies saved for {req.platform}"}


@app.get("/login/status")
def login_status() -> list[dict[str, object]]:
    """Return cookie status for all known platforms."""
    results: list[dict[str, object]] = []
    sites_dir = CONFIG_DIR / "sites"
    if not sites_dir.exists():
        return results

    now_ts = int(time.time())
    for fpath in sorted(sites_dir.glob("*.jsonc")):
        platform_name = fpath.stem
        try:
            cfg = load_jsonc(fpath)
        except Exception:
            results.append({"platform": platform_name, "has_cookies": False, "error": "config_parse_failed"})
            continue

        pf = cfg.get("platform") or {}
        pc = pf.get("cookies") or {}
        has_cookies = bool(pc and any(v for v in pc.values() if v and v != "{}"))

        status: dict[str, object] = {
            "platform": platform_name,
            "has_cookies": has_cookies,
            "cookie_count": len(pc) if isinstance(pc, dict) else 0,
        }

        # Read verify state from Redis (not config file)
        vs = get_verify_state(platform_name)
        if vs:
            status["verified_ok"] = vs.get("verified_ok")
            status["verified_at"] = vs.get("verified_at")

        # Read save timestamp from Redis (not config file)
        saved_at = _get_saved_at(platform_name)
        if saved_at:
            elapsed = now_ts - saved_at
            status["saved_at"] = saved_at
            status["saved_at_iso"] = datetime.fromtimestamp(saved_at, tz=UTC).isoformat()
            status["elapsed_seconds"] = elapsed
            status["expired"] = elapsed > 86400
            status["critical"] = elapsed > 604800

        results.append(status)

    return results


@app.post("/login/verify")
def verify_platform_cookies(req: _VerifyRequest) -> dict[str, object]:
    """Actually test cookies against the platform's auth API and update status."""
    platform = req.platform
    site_path = CONFIG_DIR / "sites" / f"{platform}.jsonc"
    if not site_path.exists():
        return {"status": "error", "detail": f"Config not found for {platform}"}

    try:
        cfg = load_jsonc(site_path)
    except Exception as e:
        return {"status": "error", "detail": f"Config parse failed: {e}"}

    cookies_dict: dict[str, str] = {
        k: str(v) for k, v in (((cfg.get("platform") or {}).get("cookies") or {}).items())
        if v
    }
    if not cookies_dict:
        return {"status": "error", "detail": "No cookies found in config"}

    import httpx

    result: dict[str, object] = {"platform": platform, "tested_at": int(time.time())}

    try:
        if platform == "xiaohongshu":
            # Use user/me endpoint with existing signer
            from packages.provider_impls.xiaohongshu.xs_signer import XHSignatureSigner, SessionManager
            from packages.provider_impls.xiaohongshu.xs_config import CryptoConfig
            ua = cookies_dict.get("ua") or CryptoConfig().PUBLIC_USERAGENT
            signer = XHSignatureSigner(CryptoConfig().with_overrides(PUBLIC_USERAGENT=ua))
            sm = SessionManager(CryptoConfig().with_overrides(PUBLIC_USERAGENT=ua))
            h = signer.sign_headers_get("/api/sns/web/v2/user/me", cookies_dict, params={}, session=sm)
            r = httpx.get("https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
                headers={"user-agent": ua, "cookie": "; ".join(f"{k}={v}" for k, v in cookies_dict.items()),
                         "origin": "https://www.xiaohongshu.com", "referer": "https://www.xiaohongshu.com/", **h},
                timeout=15)
            data = r.json()
            if data.get("success") and data.get("data", {}).get("guest") is False:
                result["valid"] = True
                result["detail"] = f"已登录: {data['data'].get('nickname', data['data'].get('user_id', ''))}"
            elif data.get("success") and data.get("data", {}).get("guest") is True:
                result["valid"] = False
                result["detail"] = "cookies 存在但为游客状态（未登录）"
            else:
                result["valid"] = False
                result["detail"] = data.get("msg", "API 返回异常")

        elif platform == "douyin":
            # Check the main page: valid cookies 鈫?200 + no redirect to login
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            r = httpx.get("https://www.douyin.com/",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                         "Cookie": cookie_str},
                timeout=15, follow_redirects=False)
            if r.status_code == 200 and len(r.text) > 1000:
                result["valid"] = True
                result["detail"] = f"HTTP 200, 页面正常"
            elif r.status_code in (301, 302, 303, 307):
                result["valid"] = False
                result["detail"] = f"被重定向 (HTTP {r.status_code}), cookies 可能无效"
            else:
                result["valid"] = False
                result["detail"] = f"HTTP {r.status_code}, 内容不足"

        elif platform == "weibo":
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            r = httpx.get("https://m.weibo.cn/api/config",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                         "Referer": "https://m.weibo.cn/",
                         "Cookie": cookie_str},
                timeout=15)
            if r.status_code == 200:
                data = r.json()
                uid = data.get("data", {}).get("uid")
                if uid:
                    result["valid"] = True
                    result["detail"] = f"已登录(uid={uid})"
                else:
                    result["valid"] = False
                    result["detail"] = "未检测到登录状态"
            else:
                result["valid"] = False
                result["detail"] = f"HTTP {r.status_code}"

        else:
            # Generic: just check that cookies are accepted
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
            r = httpx.get(f"https://www.{platform}.com/", headers={"Cookie": cookie_str}, timeout=10, follow_redirects=False)
            result["valid"] = r.status_code < 400
            result["detail"] = f"HTTP {r.status_code}"

    except Exception as e:
        result["valid"] = False
        result["detail"] = f"请求异常: {type(e).__name__}: {e}"

    # Write verification result to Redis
    try:
        from packages.core.config.cookie_verify import set_verified as _set_v
        _set_v(platform, result.get("valid", False))
    except Exception:
        pass

    return result


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    """Receive real-time platform events (task updates, live status, etc.)."""
    await websocket.accept()
    q = subscribe_ws_events()
    try:
        while True:
            event = await q.get()
            try:
                await websocket.send_json(event)
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe_ws_events(q)


@app.get("/platforms")
def list_platforms() -> list[dict[str, str | list[str]]]:
    """Return available platform names and their account types from provider packages."""
    from packages.core.providers.registry import ProviderRegistry
    registry = ProviderRegistry()
    PROVIDER_DIR = Path(__file__).resolve().parents[2] / "packages" / "providers"
    platforms: list[dict[str, str | list[str]]] = []
    if not PROVIDER_DIR.is_dir():
        return platforms
    for entry in sorted(PROVIDER_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        if not (entry / "__init__.py").exists():
            continue
        try:
            provider = registry.get(entry.name)
            platforms.append({"name": provider.platform, "account_types": provider.account_types})
        except (KeyError, Exception):
            platforms.append({"name": entry.name, "account_types": ["profile"]})
    return platforms


@app.get("/")
async def web_index() -> FileResponse:
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="web ui not found")
    return FileResponse(index_path)
