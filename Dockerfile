# Dockerfile — Athena Core multi-stage build

FROM python:3.14-slim AS builder

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir . && \
    pip install --no-cache-dir alembic

# ── Runtime stage ────────────────────────────────────────────────────
FROM python:3.14-slim AS runtime

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY athena/ ./athena/
COPY alembic.ini ./
COPY data/ ./data/
COPY scripts/ ./scripts/

# Create data directory and non-root user
RUN mkdir -p /data /workspace && \
    addgroup --system athena && \
    adduser --system --no-create-home --ingroup athena athena && \
    chown -R athena:athena /data /workspace /app

USER athena

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV SQLITE_DB_PATH=/data/athena.db
ENV DATA_DIR=/data

EXPOSE 8000

ENTRYPOINT ["/bin/sh", "/app/scripts/entrypoint.sh"]
