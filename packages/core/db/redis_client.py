"""Unified Redis client factory.

Usage:

    from packages.core.db.redis_client import redis_sync, redis_async

    # Sync
    with redis_sync() as r:
        r.set("key", "value")

    # Async
    async with await redis_async() as r:
        await r.set("key", "value")
"""

from __future__ import annotations

from contextlib import contextmanager

from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis

from .urls import redis_get_url


def _ensure_url() -> str:
    url = redis_get_url()
    if not url:
        raise RuntimeError("POLYCRAWL_REDIS_URL is not set")
    return url


@contextmanager
def redis_sync(*, socket_connect_timeout: float | None = None) -> SyncRedis:
    """Get a sync Redis client (use with 'with' statement).

    Args:
        socket_connect_timeout: Optional connection timeout in seconds.
    """
    kwargs: dict = {"decode_responses": True}
    if socket_connect_timeout is not None:
        kwargs["socket_connect_timeout"] = socket_connect_timeout
    r = SyncRedis.from_url(_ensure_url(), **kwargs)
    try:
        yield r
    finally:
        r.close()


async def redis_async() -> AsyncRedis:
    """Get an async Redis client (use with 'async with')."""
    return await AsyncRedis.from_url(_ensure_url(), decode_responses=True)


def redis_pubsub() -> SyncRedis:
    """Get a sync Redis client for long-lived PubSub subscriptions.

    The caller is responsible for closing the connection when done.
    Example::

        client = redis_pubsub()
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe("channel")
        ...
        client.close()
    """
    return SyncRedis.from_url(_ensure_url(), decode_responses=True)
