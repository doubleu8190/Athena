# Dockerfile — Athena application image (builds on athena-base)
#
# Prerequisite: Build the base image first:
#   sh scripts/build-base.sh

# ── Frontend build stage ─────────────────────────────────────────────
FROM node:26-alpine AS frontend-builder

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Runtime stage ────────────────────────────────────────────────────
FROM athena-base:latest

WORKDIR /app

# Copy application code
COPY athena/ ./athena/
COPY alembic.ini ./
COPY data/ ./data/
COPY scripts/ ./scripts/

# Copy frontend build output (production mode — FastAPI serves it via StaticFiles)
COPY --from=frontend-builder /build/dist/ ./frontend/dist/

# Ensure athena user owns the application directory
# (base image created the user but COPY adds files as root)
RUN chown -R athena:athena /app

USER athena

ENTRYPOINT ["/bin/sh", "/app/scripts/entrypoint.sh"]
