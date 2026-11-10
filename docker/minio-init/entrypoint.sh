#!/usr/bin/env sh

set -eu

# 桶初始化只用 root secret 执行管理操作，避免把访问凭据写进容器环境配置。
export AWS_ACCESS_KEY_ID="$(cat /run/secrets/minio_root_user)"
export AWS_SECRET_ACCESS_KEY="$(cat /run/secrets/minio_root_password)"

exec /bin/sh -ec "$@"
