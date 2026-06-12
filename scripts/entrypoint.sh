#!/bin/sh
set -e

# Run database migrations
echo "Running Alembic migrations..."
alembic upgrade head

# Start Athena Core
echo "Starting Athena Core..."
exec uvicorn athena.main:app --host 0.0.0.0 --port 8000
