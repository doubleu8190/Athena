# Dockerfile — Athena Core multi-stage build

FROM python:3.14.6 AS builder

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir . && \
    pip install --no-cache-dir alembic

# ── Runtime stage ────────────────────────────────────────────────────
FROM python:3.14.6 AS runtime

WORKDIR /app

# Install runtime dependencies: curl, Node.js 22.x (for chrome-devtools-mcp),
# Chromium (Puppeteer system dep), and shared libraries required by Chromium.
RUN apt-get update && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    nodejs \
    chromium \
    libasound2 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
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

# chrome-devtools-mcp: use system Chromium instead of Puppeteer's bundled download
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium
# Opt out of chrome-devtools-mcp telemetry
ENV CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS=true

EXPOSE 8000

ENTRYPOINT ["/bin/sh", "/app/scripts/entrypoint.sh"]
