# syntax=docker/dockerfile:1
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
RUN pip install --user --no-cache-dir -e .

# ── runtime image ──────────────────────────────────────────────
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy pre-built dependencies
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# Copy project code
COPY . .

# ── Docker Secrets entrypoint ──────────────────────────────────
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── ports ──────────────────────────────────────────────────────
EXPOSE 8000

# docker compose 通过 command 覆盖选择服务:
#   api:    uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
#   worker: python -m services.worker.run
#   migrate: alembic upgrade head
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
