#!/bin/sh
# ── PolyCrawl Docker entrypoint ─────────────────────────────────
# Reads Docker Secrets (if available) and constructs connection
# URLs, then executes the container's main command.
#
# Usage in docker-compose.yml:
#   entrypoint: ["/entrypoint.sh"]
#   command: ["python", "-m", "services.worker.run"]
# ────────────────────────────────────────────────────────────────

set -e

# ── PostgreSQL password from secret ─────────────────────────────
if [ -f /run/secrets/postgres_password ]; then
    POSTGRES_PASSWORD=$(cat /run/secrets/postgres_password)
    export POSTGRES_PASSWORD
    DB_USER="${POSTGRES_USER:-polycrawl}"
    DB_NAME="${POSTGRES_DB:-polycrawl_db}"
    export POLYCRAWL_DATABASE_URL="postgresql+asyncpg://${DB_USER}:${POSTGRES_PASSWORD}@postgres:5432/${DB_NAME}"
fi

# ── Redis password from secret ──────────────────────────────────
if [ -f /run/secrets/redis_password ]; then
    REDIS_PASSWORD=$(cat /run/secrets/redis_password)
    export REDIS_PASSWORD
    export POLYCRAWL_REDIS_URL="redis://:${REDIS_PASSWORD}@redis:6379/0"
fi

exec "$@"
