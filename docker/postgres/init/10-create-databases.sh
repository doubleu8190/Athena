#!/usr/bin/env bash
set -Eeuo pipefail

test_database="${POSTGRES_TEST_DB:-athena_test}"

# 初始化事务按独立数据库创建，测试迁移与开发库互不共享 schema 或数据。
createdb --username="$POSTGRES_USER" --owner="$POSTGRES_USER" "$test_database"
for database in "$POSTGRES_DB" "$test_database"; do
  psql --username="$POSTGRES_USER" --dbname="$database" --set=ON_ERROR_STOP=1 \
    --command='CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS zhparser;'
done
