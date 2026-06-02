"""Async config file watcher — replaces the old 2-second polling loop.

Uses watchdog (cross-platform file system events) to detect JSONC changes
and feeds them into an asyncio.Queue so the API can react immediately
without busy-waiting.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _ConfigEventHandler(FileSystemEventHandler):
    """Pushes modified/created/moved .jsonc file paths into an asyncio queue."""

    def __init__(self, queue: asyncio.Queue[str]) -> None:
        self.queue = queue

    def _enqueue(self, path: str) -> None:
        if not path.endswith(".jsonc"):
            return
        try:
            self.queue.put_nowait(path)
        except asyncio.QueueFull:
            pass

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_moved(self, event) -> None:
        if event.is_directory:
            return
        self._enqueue(event.dest_path)


async def watch_config_dir(
    config_dir: Path,
) -> tuple[asyncio.Queue[str], Observer]:
    """Start a watchdog observer in a daemon thread.

    Returns:
        (queue, observer) — call *observer.stop()* on shutdown.
    """
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=128)
    handler = _ConfigEventHandler(queue)
    observer = Observer()
    observer.schedule(handler, str(config_dir), recursive=True)
    observer.daemon = True
    observer.start()
    return queue, observer
