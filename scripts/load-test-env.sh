#!/usr/bin/env bash

# 用法：source scripts/load-test-env.sh；配置仅进入当前 shell 及其子进程。
_athena_clear_test_credentials() {
  unset ATHENA_DATABASE_URL ATHENA_TEST_DATABASE_URL ATHENA_REDIS_URL
  unset ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY ATHENA_KNOWLEDGE_MINIO_SECRET_KEY
  unset ATHENA_TEST_MINIO_ACCESS_KEY ATHENA_TEST_MINIO_SECRET_KEY ATHENA_MCP_SECRET_KEY
  unset ATHENA_KNOWLEDGE_MINIO_ENDPOINT ATHENA_TEST_MINIO_ENDPOINT
  unset ATHENA_KNOWLEDGE_MINIO_BUCKET ATHENA_TEST_MINIO_BUCKET
}

_athena_clear_test_credentials
if [[ -n "${ZSH_VERSION:-}" ]]; then
  _ATHENA_TEST_SOURCE="${(%):-%x}"
else
  _ATHENA_TEST_SOURCE="${BASH_SOURCE[0]}"
fi
_ATHENA_TEST_ROOT="$(cd "$(dirname "$_ATHENA_TEST_SOURCE")/.." && pwd)"
if [[ ! -f "$_ATHENA_TEST_ROOT/docker/test.env" ]]; then
  printf '缺少 docker/test.env；先复制模板并运行 scripts/bootstrap-test-secrets.py。\n' >&2
  return 1
fi

set -a
# shellcheck disable=SC1091
source "$_ATHENA_TEST_ROOT/docker/test.env"
set +a
# 模板可能由旧版本升级而来，连接密钥一律由下方 secret 文件重新生成。
_athena_clear_test_credentials

_athena_read_secret() {
  local secret_path="$_ATHENA_TEST_ROOT/secrets/$1"
  if [[ ! -s "$secret_path" ]]; then
    printf '缺少本地 secret 文件：%s\n' "${secret_path##*/}" >&2
    return 1
  fi
  cat "$secret_path"
}

_ATHENA_TEST_MINIO_USER="$(_athena_read_secret minio_root_user)" || {
  _athena_clear_test_credentials
  return 1
}
_ATHENA_TEST_MINIO_PASSWORD="$(_athena_read_secret minio_root_password)" || {
  _athena_clear_test_credentials
  return 1
}
ATHENA_MCP_SECRET_KEY="$(_athena_read_secret mcp_secret_key)" || {
  _athena_clear_test_credentials
  return 1
}

# URL 凭据必须逐段百分号编码，避免密码中的 @、:、/ 等字符改写连接地址。
ATHENA_DATABASE_URL="$(ATHENA_TEST_SECRET_DIRECTORY="$_ATHENA_TEST_ROOT/secrets" \
  python3 -c 'import os; from pathlib import Path; from urllib.parse import quote; root=Path(os.environ["ATHENA_TEST_SECRET_DIRECTORY"]); user=(root/"postgres_user").read_text(); password=(root/"postgres_password").read_text(); print("postgresql+asyncpg://" + quote(user, safe="") + ":" + quote(password, safe="") + "@127.0.0.1:" + os.environ["POSTGRES_PORT"] + "/" + quote(os.environ["POSTGRES_DB"], safe=""))')" || {
  _athena_clear_test_credentials
  return 1
}
ATHENA_TEST_DATABASE_URL="$(ATHENA_TEST_SECRET_DIRECTORY="$_ATHENA_TEST_ROOT/secrets" \
  python3 -c 'import os; from pathlib import Path; from urllib.parse import quote; root=Path(os.environ["ATHENA_TEST_SECRET_DIRECTORY"]); user=(root/"postgres_user").read_text(); password=(root/"postgres_password").read_text(); print("postgresql+asyncpg://" + quote(user, safe="") + ":" + quote(password, safe="") + "@127.0.0.1:" + os.environ["POSTGRES_PORT"] + "/" + quote(os.environ["POSTGRES_TEST_DB"], safe=""))')" || {
  _athena_clear_test_credentials
  return 1
}
ATHENA_REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
ATHENA_KNOWLEDGE_MINIO_ENDPOINT="127.0.0.1:${MINIO_API_PORT}"
ATHENA_TEST_MINIO_ENDPOINT="127.0.0.1:${MINIO_API_PORT}"
ATHENA_KNOWLEDGE_MINIO_BUCKET="${MINIO_DEV_BUCKET:-athena-knowledge-dev}"
ATHENA_TEST_MINIO_BUCKET="${MINIO_TEST_BUCKET:-athena-knowledge-test}"
ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY="$_ATHENA_TEST_MINIO_USER"
ATHENA_KNOWLEDGE_MINIO_SECRET_KEY="$_ATHENA_TEST_MINIO_PASSWORD"
ATHENA_TEST_MINIO_ACCESS_KEY="$_ATHENA_TEST_MINIO_USER"
ATHENA_TEST_MINIO_SECRET_KEY="$_ATHENA_TEST_MINIO_PASSWORD"
export ATHENA_DATABASE_URL ATHENA_TEST_DATABASE_URL ATHENA_REDIS_URL
export ATHENA_KNOWLEDGE_MINIO_ENDPOINT ATHENA_TEST_MINIO_ENDPOINT
export ATHENA_KNOWLEDGE_MINIO_BUCKET ATHENA_TEST_MINIO_BUCKET
export ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY ATHENA_KNOWLEDGE_MINIO_SECRET_KEY
export ATHENA_TEST_MINIO_ACCESS_KEY ATHENA_TEST_MINIO_SECRET_KEY ATHENA_MCP_SECRET_KEY

unset _ATHENA_TEST_ROOT
unset _ATHENA_TEST_MINIO_USER _ATHENA_TEST_MINIO_PASSWORD
