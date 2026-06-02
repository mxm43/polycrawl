from __future__ import annotations

import asyncio
import json
from typing import Any

import redis as sync_redis

_REDIS_CHANNEL = "polycrawl:events"

_ws_clients: set[asyncio.Queue[dict[str, Any]]] = set()


def publish_event(redis_url: str, event_type: str, data: dict[str, Any] | None = None) -> None:
    """Publish an event to the Redis ``polycrawl:events`` channel.

    Called from any process (Worker, API, Beat) to notify connected
    WebSocket clients about state changes.
    """
    try:
        payload = json.dumps({"type": event_type, "data": data or {}}, ensure_ascii=False)
        client = sync_redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
        client.publish(_REDIS_CHANNEL, payload)
        client.close()
    except Exception:
        pass


async def subscribe_to_events(redis_url: str) -> None:
    """Background coroutine: subscribe to ``polycrawl:events`` on Redis and
    forward every message to all connected WebSocket queues."""
    loop = asyncio.get_running_loop()
    pubsub = sync_redis.from_url(redis_url, decode_responses=True).pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(_REDIS_CHANNEL)

    try:
        while True:
            msg = await loop.run_in_executor(None, pubsub.get_message, 0.5)
            if msg is None:
                await asyncio.sleep(0.01)
                continue
            data = msg.get("data")
            if not data:
                continue
            try:
                event = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue

            # Broadcast to all connected WebSocket clients (best-effort)
            removed: list[asyncio.Queue[dict[str, Any]]] = []
            for q in list(_ws_clients):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    removed.append(q)
            for q in removed:
                _ws_clients.discard(q)
            await asyncio.sleep(0)
    finally:
        pubsub.close()


def subscribe_ws_events() -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    _ws_clients.add(q)
    return q


def unsubscribe_ws_events(q: asyncio.Queue[dict[str, Any]]) -> None:
    _ws_clients.discard(q)
