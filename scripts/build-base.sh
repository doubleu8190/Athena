#!/bin/sh
# scripts/build-base.sh — Build the Athena base image
#
# Run this whenever pyproject.toml dependencies or system packages change.
# The base image contains all system deps + ~170 Python packages.
# It is referenced by Dockerfile as FROM athena-base:latest.
#
# Usage:  sh scripts/build-base.sh [tag]

set -e

TAG="${1:-latest}"
IMAGE="athena-base:${TAG}"

echo "Building Athena base image: ${IMAGE}..."
docker build -f Dockerfile.base -t "${IMAGE}" .

echo ""
echo "Base image built: ${IMAGE}"
echo "To rebuild the application image, run: docker compose build"
