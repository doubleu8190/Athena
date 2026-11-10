#!/usr/bin/env sh

set -eu

# MinIO 镜像没有 _FILE 支持；仅在进程启动时从只读 secret 注入 root 凭据。
MINIO_ROOT_USER="$(cat "$MINIO_ROOT_USER_FILE")"
MINIO_ROOT_PASSWORD="$(cat "$MINIO_ROOT_PASSWORD_FILE")"
export MINIO_ROOT_USER MINIO_ROOT_PASSWORD
unset MINIO_ROOT_USER_FILE MINIO_ROOT_PASSWORD_FILE

exec /usr/bin/docker-entrypoint.sh "$@"
