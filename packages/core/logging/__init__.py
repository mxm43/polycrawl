from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Any

import redis as sync_redis


_LOG_ENTRY_FMT = "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"
def _local_formatter() -> logging.Formatter:
    """Return a Formatter that uses the system local time."""
    return logging.Formatter(_LOG_ENTRY_FMT, datefmt=_DATE_FMT)


_LOG_DIR = "data/logs"
_LOG_FILE = "polycrawl.log"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_BACKUP_COUNT = 5


def _format_entry(record: logging.LogRecord) -> dict[str, str]:
    ts = datetime.fromtimestamp(record.created).strftime(_DATE_FMT)
    return {
        "timestamp": ts + f".{int(record.msecs):03d}",
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }


# ---------------------------------------------------------------------------
# Redis Pub/Sub logging handler
# ---------------------------------------------------------------------------

_REDIS_CHANNEL = "polycrawl:logs"


class _RedisPubHandler(logging.Handler):
    """Log handler that publishes entries to a Redis channel.

    Every process (API, Worker, Beat) uses this handler.  The API also
    maintains a Redis SUBSCRIBE loop that forwards these entries to all
    connected WebSocket clients.

    Creates a fresh Redis connection per emit() to stay fork-safe.
    """

    def __init__(self, redis_url: str) -> None:
        super().__init__()
        self._redis_url = redis_url

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = _format_entry(record)
            client = sync_redis.from_url(self._redis_url, decode_responses=True, socket_connect_timeout=2)
            client.publish(_REDIS_CHANNEL, json.dumps(entry, ensure_ascii=False))
            client.close()
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Redis subscriber – called by API startup to feed WebSocket clients
# ---------------------------------------------------------------------------

_ws_clients: set[asyncio.Queue[dict[str, Any]]] = set()


async def subscribe_to_logs(redis_url: str) -> None:
    """Background coroutine: subscribe to ``polycrawl:logs`` on Redis and forward
    every entry to all connected WebSocket queues."""
    loop = asyncio.get_running_loop()
    pubsub = sync_redis.from_url(redis_url, decode_responses=True).pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(_REDIS_CHANNEL)

    try:
        while True:
            msg = await loop.run_in_executor(None, pubsub.get_message, 1.0)
            if msg is None:
                await asyncio.sleep(0.01)
                continue
            data = msg.get("data")
            if not data:
                continue
            try:
                entry = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue

            # Broadcast to all connected WebSocket clients (best-effort)
            removed: list[asyncio.Queue[dict[str, Any]]] = []
            for q in list(_ws_clients):
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    removed.append(q)
            for q in removed:
                _ws_clients.discard(q)
            await asyncio.sleep(0)  # yield control
    finally:
        pubsub.close()


def subscribe_ws() -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _ws_clients.add(q)
    return q


def unsubscribe_ws(q: asyncio.Queue[dict[str, Any]]) -> None:
    _ws_clients.discard(q)


# ---------------------------------------------------------------------------
# Read recent log entries from the shared log file
# ---------------------------------------------------------------------------

def clear_log_file(log_dir: str | Path = _LOG_DIR) -> None:
    log_path = Path(log_dir) / _LOG_FILE
    if log_path.exists():
        log_path.write_text("", encoding="utf-8")


def read_log_file(
    log_dir: str | Path = _LOG_DIR,
    limit: int = 200,
    level: str | None = None,
) -> list[dict[str, str]]:
    """Parse the latest *limit* lines from the rotating log file."""
    log_path = Path(log_dir) / _LOG_FILE
    if not log_path.exists():
        return []

    # Read last N lines
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    results: list[dict[str, str]] = []
    for line in reversed(lines):
        line = line.rstrip("\n")
        if not line:
            continue
        # Parse: "2026-05-25 12:30:54.884 [INFO   ] logger: message"
        ts = line[:23] if len(line) >= 23 else line
        rest = line[24:] if len(line) > 24 else ""
        lvl = ""
        msg = ""
        if "]" in rest:
            parts = rest.split("]", 1)
            lvl = parts[0].strip(" [")
            rem = parts[1].strip() if len(parts) > 1 else ""
            if ": " in rem:
                logger_name, msg = rem.split(": ", 1)
            else:
                logger_name = ""
                msg = rem
        else:
            logger_name = ""
            msg = rest

        entry: dict[str, str] = {
            "timestamp": ts,
            "level": lvl,
            "logger": logger_name,
            "message": msg,
        }
        if level and lvl != level.upper():
            continue
        results.append(entry)
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# Public setup function
# ---------------------------------------------------------------------------

def setup_logging(
    redis_url: str = "",
    log_dir: str | Path = _LOG_DIR,
    log_level: str = "INFO",
    root_logger: logging.Logger | None = None,
) -> None:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    root = root_logger or logging.getLogger()

    # Guard: skip if already fully initialized (file + Redis handlers exist).
    has_all = any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
    if has_all:
        return

    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # ---- Rotating file handler ----
    _log_file = log_path / _LOG_FILE
    fh = logging.handlers.RotatingFileHandler(
        _log_file,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(_local_formatter())
    root.addHandler(fh)

    # ---- Console handler (skip if inherited from parent) ----
    has_console = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    if not has_console:
        ch = logging.StreamHandler()
        ch.setFormatter(_local_formatter())
        root.addHandler(ch)

    # ---- Redis Pub/Sub handler ----
    if redis_url:
        rh = _RedisPubHandler(redis_url)
        rh.setLevel(logging.INFO)  # only INFO+ to Redis (avoid flooding)
        root.addHandler(rh)

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    logging.debug("Logging initialized — level=%s, dir=%s", log_level, log_path.resolve())
