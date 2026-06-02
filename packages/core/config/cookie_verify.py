"""Redis-backed cookie verification state.

Stores ``verified_ok`` and ``verified_at`` per platform in Redis
instead of in the user's JSONC config files, so that:

- Pasting new cookies via the web console never overwrites verify state.
- State can be invalidated by worker executors on auth failure.
- TTL prevents stale data from accumulating forever.

Redis key pattern: ``polycrawl:cookies:verify:{platform}``
Value: JSON ``{"verified_ok": bool, "verified_at": int}``
Default TTL: 7 days.
"""

from __future__ import annotations

import json
import time
from typing import Any

from redis import Redis

_KEY_PREFIX = "polycrawl:cookies:verify:"
_SAVED_AT_PREFIX = "polycrawl:cookies:saved_at:"
_DEFAULT_TTL = 604800  # 7 days


def get_verify_state(platform: str) -> dict[str, Any]:
    """Return ``{verified_ok, verified_at}`` for a platform, or empty dict."""
    from packages.core.db import redis_sync
    url = redis_get_url()
    if not url:
        return {}
    try:
        with redis_sync() as r:
            raw = r.get(_KEY_PREFIX + platform)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def set_verified(platform: str, ok: bool) -> None:
    """Store ``verified_ok`` and ``verified_at`` in Redis with a TTL."""
    from packages.core.db import redis_sync
    url = redis_get_url()
    if not url:
        return
    try:
        with redis_sync() as r:
            r.setex(_KEY_PREFIX + platform, _DEFAULT_TTL, json.dumps(data))
    except Exception:
        pass


def invalidate(platform: str) -> None:
    """Shortcut: mark a platform's cookies as invalid (set verified_ok=false)."""
    set_verified(platform, False)


def clear(platform: str) -> None:
    """Remove verify state for a platform."""
    from packages.core.db import redis_sync
    url = redis_get_url()
    if not url:
        return
    try:
        with redis_sync() as r:
            r.delete(_KEY_PREFIX + platform)
    except Exception:
        pass


def set_saved_at(platform: str) -> None:
    """Record when cookies were last saved for a platform (Redis, not config file)."""
    from packages.core.db.urls import redis_get_url
    url = redis_get_url()
    if not url:
        return
    try:
        with redis_sync() as r:
            r.set(_SAVED_AT_PREFIX + platform, str(int(time.time())))
    except Exception:
        pass


def get_saved_at(platform: str) -> int | None:
    """Return Unix timestamp of last cookie save, or None."""
    from packages.core.db.urls import redis_get_url
    url = redis_get_url()
    if not url:
        return None
    try:
        with redis_sync() as r:
            raw = r.get(_SAVED_AT_PREFIX + platform)
        if raw:
            return int(raw)
    except Exception:
        pass
    return None
