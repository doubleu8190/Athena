#!/bin/sh
# Health check for Athena Core container.
# Verifies the API is responsive.

curl -sf http://localhost:8000/api/v1/health || exit 1
