"""Consumer — event-driven task execution.

For each enabled task entry in base.jsonc, starts an asyncio coroutine
that blocks on BLPOP of that task's Redis list.  Same queue = serial
execution; different queues = parallel.

No platform locks, no SKIP, no retry — the queue itself provides
serialization within each task.

live_record tasks notify the scheduler via notify_live_done() when
a recording ends, so the scheduler can re-classify the account into
its proper tier and re-arm the check timer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from redis.asyncio import Redis

from packages.core.config import ConfigLoader
from services.worker.executors import (
    execute_content_fetch,
    execute_live_record,
    mark_live_error,
)
from services.worker.executors.content_executor import _record_adaptive_state
from services.worker.runtime import (
    db_get_session_factory,
    mark_task_failed,
    mark_task_running,
    mark_task_success,
    mark_task_success_with_metrics,
)
from services.worker.scheduler import notify_live_done

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


class Consumer:

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._redis: Redis | None = None

    async def start(self) -> None:
        from packages.core.db.urls import redis_get_url
        self._redis = Redis.from_url(redis_get_url(), decode_responses=True)
        _state = ConfigLoader(CONFIG_DIR).load_all()
        queue_list = []
        for idx, entry in enumerate(_state.base.schedules):
            # Listen on all schedules (enabled or disabled) so manually
            # submitted tasks are always consumed regardless of schedule config.
            if entry.type == "live_record":
                # live_record: single queue, unchanged
                queue_key = f"task_{idx}"
                queue_list.append(queue_key)
                c = asyncio.create_task(self._handle_queue_events(queue_key))
                self._tasks.append(c)
                logger.info("[consumer] started consumer for %s (task_%d)", queue_key, idx)
            else:
                # content_fetch / other: one coroutine per platform for
                # true parallelism — each platform has its own queue from
                # the scheduler, so one slow platform never blocks others.
                for platform in _state.sites:
                    queue_key = f"task_{idx}:{platform}"
                    queue_list.append(queue_key)
                    c = asyncio.create_task(self._handle_queue_events(queue_key))
                    self._tasks.append(c)
                    logger.info("[consumer] started consumer for %s (task_%d, %s)", queue_key, idx, platform)
        logger.info("[consumer] all listening queues: %s", queue_list)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._redis:
            await self._redis.aclose()

    async def _handle_queue_events(self, queue_key: str) -> None:
        while True:
            try:
                raw = await self._redis.blpop(queue_key, timeout=1)
                if raw is None:
                    continue
                _, data = raw
                msg = json.loads(data)
                logger.info("[consumer] RECEIVED from queue=%s task_id=%s account_id=%s task_type=%s",
                            queue_key, msg.get("task_id"), msg.get("account_id"), msg.get("task_type"))
                params = msg.get("params") or {}
                task_type = msg.get("task_type", "")
                await self._execute(msg)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[consumer] error processing %s", queue_key)

    async def _execute(self, msg: dict) -> None:
        task_type = msg.get("task_type", "")
        task_id = msg.get("task_id", "")
        account_id = msg.get("account_id")

        try:
            await mark_task_running(task_id)

            if task_type == "content_fetch":
                execution = await execute_content_fetch(task_id=task_id, account_id=account_id)
                await mark_task_success_with_metrics(
                    task_id,
                    items_fetched=execution.items_fetched,
                    items_downloaded=execution.items_downloaded,
                    items_skipped=execution.items_skipped,
                    bytes_downloaded=execution.bytes_downloaded,
                )
                logger.info(
                    "[consumer] content_fetch DONE account=%d - fetched=%s downloaded=%s skipped=%s bytes=%s",
                    account_id,
                    execution.items_fetched, execution.items_downloaded,
                    execution.items_skipped, execution.bytes_downloaded,
                )

            elif task_type == "live_record":
                execution = await execute_live_record(task_id=task_id, account_id=account_id)
                if execution.status in ("offline",):
                    # Room not live — task is done
                    await mark_task_success(task_id)
                    logger.info("[consumer] live_record DONE account=%d - offline", account_id)
                    notify_live_done(account_id)
                elif execution.status == "recording_started":
                    # Background download running — consumer freed; notification
                    # will come from _background_record when it completes.
                    logger.info("[consumer] live_record RECORDING account=%d - session=%s", account_id, execution.session_id)
                else:
                    await mark_task_success(task_id)
                    logger.info("[consumer] live_record DONE account=%d - session=%s", account_id, execution.session_id)
                    notify_live_done(account_id)

        except Exception as exc:
            logger.error("[consumer] %s FAILED account=%d - %s", task_type, account_id, exc)
            if task_type in ("live_record"):
                notify_live_done(account_id)
                await mark_live_error(account_id, str(exc))
            elif task_type == "content_fetch":
                try:
                    _record_adaptive_state(account_id, 0)
                except Exception:
                    pass
            await mark_task_failed(task_id, str(exc))
