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


def _redis_from_config() -> Redis:
    """Create a Redis connection from the project config."""
    from packages.core.config import ConfigLoader
    from pathlib import Path
    cfg = ConfigLoader(Path(__file__).resolve().parents[3] / "config").load_all()
    return Redis.from_url(cfg.base.storage.redis_url, decode_responses=True)


def get_verify_state(platform: str) -> dict[str, Any]:
    """Return ``{verified_ok, verified_at}`` for a platform, or empty dict."""
    try:
        r = _redis_from_config()
        raw = r.get(_KEY_PREFIX + platform)
        r.close()
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def set_verified(platform: str, ok: bool) -> None:
    """Store ``verified_ok`` and ``verified_at`` in Redis with a TTL."""
    try:
        r = _redis_from_config()
        data = {"verified_ok": ok, "verified_at": int(time.time())}
        r.setex(_KEY_PREFIX + platform, _DEFAULT_TTL, json.dumps(data))
        r.close()
    except Exception:
        pass


def invalidate(platform: str) -> None:
    """Shortcut: mark a platform's cookies as invalid (set verified_ok=false)."""
    set_verified(platform, False)


def clear(platform: str) -> None:
    """Remove verify state for a platform."""
    try:
        r = _redis_from_config()
        r.delete(_KEY_PREFIX + platform)
        r.close()
    except Exception:
        pass


def set_saved_at(platform: str) -> None:
    """Record when cookies were last saved for a platform (Redis, not config file)."""
    try:
        r = _redis_from_config()
        r.set(_SAVED_AT_PREFIX + platform, str(int(time.time())))
        r.close()
    except Exception:
        pass


def get_saved_at(platform: str) -> int | None:
    """Return Unix timestamp of last cookie save, or None."""
    try:
        r = _redis_from_config()
        raw = r.get(_SAVED_AT_PREFIX + platform)
        r.close()
        if raw:
            return int(raw)
    except Exception:
        pass
    return None
