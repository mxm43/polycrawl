#!/usr/bin/env python3
"""Worker entry point -- runs scheduler + consumer in a single process.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from packages.core.config import ConfigLoader
from packages.core.logging import setup_logging
from services.worker.scheduler import Scheduler
from services.worker.consumer import Consumer

# Setup logging with file + Redis Pub/Sub (for web UI log viewer)
ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
_state = ConfigLoader(CONFIG_DIR).load_all()
_redis_url = _state.base.storage.redis_url
_data_dir = _state.base.global_config.get("data_dir", "data")
_log_dir = CONFIG_DIR.parent / _data_dir / "logs"
_log_level = _state.base.global_config.get("log_level", "INFO")
setup_logging(redis_url=_redis_url, log_dir=_log_dir, log_level=_log_level)

logger = logging.getLogger(__name__)


async def _startup_recovery() -> None:
    """Clean up stale state from a previous crash before starting.

    - Marks all pending/running tasks in the DB as failed.
    - Drains any leftover items in task queues.
    - Notifies the frontend about the cleanup.
    """
    from redis.asyncio import Redis
    from sqlalchemy import text
    from services.worker.runtime import get_worker_session_factory

    # ── DB: fail stale tasks ────────────────────────────────────
    session_factory = get_worker_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE tasks SET status = 'failed', completed_at = NOW() "
                "WHERE status IN ('pending', 'running')"
            )
        )
        await session.commit()
        cleaned = result.rowcount

    # ── Live: clear orphaned recording statuses ─────────────────
    async with session_factory() as session:
        # Reset LiveStatus stuck in "recording"
        ls_result = await session.execute(
            text(
                "UPDATE live_statuses SET status = 'offline' "
                "WHERE status IN ('recording', 'probing')"
            )
        )
        # Mark LiveSessions without ended_at as interrupted
        lses_result = await session.execute(
            text(
                "UPDATE live_sessions SET status = 'interrupted' "
                "WHERE status = 'recording' AND ended_at IS NULL"
            )
        )
        await session.commit()
        live_cleaned = lses_result.rowcount

    # ── Redis: drain stale queue items ──────────────────────────
    r = Redis.from_url(_redis_url, decode_responses=True)
    try:
        keys = await r.keys("task_*")
        queue_cleaned = 0
        for key in keys:
            qlen = await r.llen(key)
            if qlen > 0:
                await r.delete(key)
                queue_cleaned += qlen
    finally:
        await r.aclose()

    if cleaned or queue_cleaned or live_cleaned:
        logger.info(
            "Startup recovery: failed %d stale task(s), drained %d stale queue item(s), "
            "cleared %d orphaned live status(es)",
            cleaned,
            queue_cleaned,
            live_cleaned,
        )
        # Notify frontend
        try:
            from packages.core.events import publish_event
            publish_event(_redis_url, "creators_updated", {"reason": "startup_recovery"})
        except Exception:
            pass


async def main() -> None:
    await _startup_recovery()

    scheduler = Scheduler()
    consumer = Consumer()

    await scheduler.start()
    await consumer.start()

    logger.info("Worker started -- scheduler + consumer running")

    shutdown_event = asyncio.Event()

    def _shutdown() -> None:
        logger.info("Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    await shutdown_event.wait()

    await consumer.stop()
    await scheduler.stop()
    logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
