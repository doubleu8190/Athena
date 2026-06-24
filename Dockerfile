# Dockerfile — Athena application image (builds on athena-base)
#
# Prerequisite: Build the base image first:
#   sh scripts/build-base.sh

# ── Runtime stage ────────────────────────────────────────────────────
FROM athena-base:latest

WORKDIR /app

# Copy application code
COPY athena/ ./athena/
COPY alembic.ini ./
COPY data/ ./data/
COPY scripts/ ./scripts/

# Ensure athena user owns the application directory
# (base image created the user but COPY adds files as root)
RUN chown -R athena:athena /app

USER athena

ENTRYPOINT ["/bin/sh", "/app/scripts/entrypoint.sh"]
