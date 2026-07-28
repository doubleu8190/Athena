#!/bin/sh
set -e

# Run database migrations
echo "Running Alembic migrations..."
alembic upgrade head

# If command arguments are provided, execute them (e.g., arq worker)
if [ $# -gt 0 ]; then
    echo "Starting with command: $*"
    exec "$@"
else
    # Start Athena Core (default)
    echo "Starting Athena Core..."
    exec uvicorn athena.main:app --host 0.0.0.0 --port 8000
fi
