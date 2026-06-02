"""Centralized database & Redis URL resolution.

All modules must use these functions instead of reading from
config files or state.base.storage.*.

Environment variables (set by Docker entrypoint or locally):
  POLYCRAWL_DATABASE_URL
  POLYCRAWL_REDIS_URL
"""

from __future__ import annotations

import os


def db_get_url() -> str:
    """Return the PostgreSQL connection string from POLYCRAWL_DATABASE_URL."""
    return os.environ.get("POLYCRAWL_DATABASE_URL", "")


def redis_get_url() -> str:
    """Return the Redis connection string from POLYCRAWL_REDIS_URL."""
    return os.environ.get("POLYCRAWL_REDIS_URL", "")
