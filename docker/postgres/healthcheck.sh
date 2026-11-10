#!/usr/bin/env sh

set -eu

# TCP 检查避开初始化阶段仅开放 Unix socket 的临时 PostgreSQL。
postgres_user="$(cat /run/secrets/postgres_user)"
postgres_password="$(cat /run/secrets/postgres_password)"
pg_isready -h 127.0.0.1 -U "$postgres_user" -d "$POSTGRES_DB"

has_extensions() {
  database="$1"
  extension_count="$(PGPASSWORD="$postgres_password" psql -X -h 127.0.0.1 \
    -U "$postgres_user" -d "$database" -v ON_ERROR_STOP=1 -Atqc \
    "SELECT count(*) FROM pg_extension WHERE extname IN ('vector','zhparser')")" || return 1
  [ "$extension_count" = 2 ]
}

# 两个应用数据库都必须可连接且具备迁移依赖的扩展，才能报告服务健康。
has_extensions "$POSTGRES_DB"
has_extensions "$POSTGRES_TEST_DB"
