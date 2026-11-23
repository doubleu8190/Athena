#!/usr/bin/env bash

# 用法：source scripts/load-test-env.sh；配置仅进入当前 shell 及其子进程。
# 容器 Compose 入口在 source 前设置 ATHENA_TEST_ENV_TARGET=container，连接地址会改用
# host.docker.internal，并指向测试库、测试桶。
_athena_clear_test_credentials() {
  unset ATHENA_DATABASE_URL ATHENA_TEST_DATABASE_URL ATHENA_REDIS_URL
  unset ATHENA_STORAGE_ENDPOINT ATHENA_STORAGE_BUCKET
  unset ATHENA_STORAGE_ACCESS_KEY ATHENA_STORAGE_SECRET_KEY
  unset ATHENA_TEST_MINIO_ENDPOINT ATHENA_TEST_MINIO_BUCKET
  unset ATHENA_TEST_MINIO_ACCESS_KEY ATHENA_TEST_MINIO_SECRET_KEY ATHENA_MCP_SECRET_KEY
  # 同步清理旧版分桶环境变量，防止宿主 shell 残留配置污染。
  unset ATHENA_KNOWLEDGE_MINIO_ENDPOINT ATHENA_KNOWLEDGE_MINIO_BUCKET
  unset ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY ATHENA_KNOWLEDGE_MINIO_SECRET_KEY
  unset ATHENA_KNOWLEDGE_MINIO_SECURE
  unset ATHENA_FILE_MINIO_ENDPOINT ATHENA_FILE_MINIO_BUCKET
  unset ATHENA_FILE_MINIO_ACCESS_KEY ATHENA_FILE_MINIO_SECRET_KEY
  unset ATHENA_FILE_MINIO_SECURE
}

_athena_clear_test_credentials
_ATHENA_TEST_ENV_TARGET="${ATHENA_TEST_ENV_TARGET:-host}"
case "$_ATHENA_TEST_ENV_TARGET" in
  host)
    _ATHENA_TEST_DATABASE_HOST="127.0.0.1"
    ;;
  container)
    _ATHENA_TEST_DATABASE_HOST="host.docker.internal"
    ;;
  *)
    printf 'ATHENA_TEST_ENV_TARGET 仅支持 host 或 container。\n' >&2
    unset _ATHENA_TEST_ENV_TARGET
    return 1
    ;;
esac
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

# Compose 基础设施参数以 docker/test.env 为准；先移除终端同名值，旧配置缺少
# 新字段时再使用模板默认值，避免宿主 shell 隐式覆盖测试环境。
unset POSTGRES_DB POSTGRES_TEST_DB POSTGRES_PORT REDIS_PORT
unset MINIO_API_PORT MINIO_CONSOLE_PORT
unset MINIO_DEV_BUCKET MINIO_TEST_BUCKET
unset ATHENA_STORAGE_SECURE ATHENA_TEST_MINIO_SECURE

set -a
# shellcheck disable=SC1091
source "$_ATHENA_TEST_ROOT/docker/test.env"
set +a
# 模板可能由旧版本升级而来，连接密钥一律由下方 secret 文件重新生成。
_athena_clear_test_credentials
POSTGRES_DB="${POSTGRES_DB:-athena}"
POSTGRES_TEST_DB="${POSTGRES_TEST_DB:-athena_test}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
REDIS_PORT="${REDIS_PORT:-6379}"
MINIO_API_PORT="${MINIO_API_PORT:-9000}"
MINIO_CONSOLE_PORT="${MINIO_CONSOLE_PORT:-9001}"
MINIO_DEV_BUCKET="${MINIO_DEV_BUCKET:-athena-storage-dev}"
MINIO_TEST_BUCKET="${MINIO_TEST_BUCKET:-athena-storage-test}"
# 默认值在 `set -a` 之后补入，必须在两个 Python 子进程开始前显式导出。
export POSTGRES_DB POSTGRES_TEST_DB POSTGRES_PORT REDIS_PORT
export MINIO_API_PORT MINIO_CONSOLE_PORT
export MINIO_DEV_BUCKET MINIO_TEST_BUCKET
case "$_ATHENA_TEST_ENV_TARGET" in
  host)
    _ATHENA_TEST_DATABASE_NAME="${POSTGRES_DB}"
    _ATHENA_TEST_STORAGE_BUCKET="${MINIO_DEV_BUCKET:-athena-storage-dev}"
    ;;
  container)
    _ATHENA_TEST_DATABASE_NAME="${POSTGRES_TEST_DB}"
    _ATHENA_TEST_STORAGE_BUCKET="${MINIO_TEST_BUCKET:-athena-storage-test}"
    ;;
esac

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
  ATHENA_TEST_DATABASE_HOST="$_ATHENA_TEST_DATABASE_HOST" \
  ATHENA_TEST_DATABASE_NAME="$_ATHENA_TEST_DATABASE_NAME" \
  python3 -c 'import os; from pathlib import Path; from urllib.parse import quote; root=Path(os.environ["ATHENA_TEST_SECRET_DIRECTORY"]); user=(root/"postgres_user").read_text(); password=(root/"postgres_password").read_text(); print("postgresql+asyncpg://" + quote(user, safe="") + ":" + quote(password, safe="") + "@" + os.environ["ATHENA_TEST_DATABASE_HOST"] + ":" + os.environ["POSTGRES_PORT"] + "/" + quote(os.environ["ATHENA_TEST_DATABASE_NAME"], safe=""))')" || {
  _athena_clear_test_credentials
  return 1
}
ATHENA_TEST_DATABASE_URL="$(ATHENA_TEST_SECRET_DIRECTORY="$_ATHENA_TEST_ROOT/secrets" \
  ATHENA_TEST_DATABASE_HOST="$_ATHENA_TEST_DATABASE_HOST" \
  python3 -c 'import os; from pathlib import Path; from urllib.parse import quote; root=Path(os.environ["ATHENA_TEST_SECRET_DIRECTORY"]); user=(root/"postgres_user").read_text(); password=(root/"postgres_password").read_text(); print("postgresql+asyncpg://" + quote(user, safe="") + ":" + quote(password, safe="") + "@" + os.environ["ATHENA_TEST_DATABASE_HOST"] + ":" + os.environ["POSTGRES_PORT"] + "/" + quote(os.environ["POSTGRES_TEST_DB"], safe=""))')" || {
  _athena_clear_test_credentials
  return 1
}
ATHENA_REDIS_URL="redis://${_ATHENA_TEST_DATABASE_HOST}:${REDIS_PORT}/0"
ATHENA_STORAGE_ENDPOINT="${_ATHENA_TEST_DATABASE_HOST}:${MINIO_API_PORT}"
ATHENA_STORAGE_BUCKET="$_ATHENA_TEST_STORAGE_BUCKET"
ATHENA_STORAGE_ACCESS_KEY="$_ATHENA_TEST_MINIO_USER"
ATHENA_STORAGE_SECRET_KEY="$_ATHENA_TEST_MINIO_PASSWORD"
ATHENA_STORAGE_SECURE="${ATHENA_STORAGE_SECURE:-${ATHENA_TEST_MINIO_SECURE:-true}}"
ATHENA_TEST_MINIO_ENDPOINT="${_ATHENA_TEST_DATABASE_HOST}:${MINIO_API_PORT}"
ATHENA_TEST_MINIO_BUCKET="${MINIO_TEST_BUCKET:-athena-storage-test}"
ATHENA_TEST_MINIO_ACCESS_KEY="$_ATHENA_TEST_MINIO_USER"
ATHENA_TEST_MINIO_SECRET_KEY="$_ATHENA_TEST_MINIO_PASSWORD"
export ATHENA_DATABASE_URL ATHENA_TEST_DATABASE_URL ATHENA_REDIS_URL
export ATHENA_STORAGE_ENDPOINT ATHENA_STORAGE_BUCKET ATHENA_STORAGE_SECURE
export ATHENA_STORAGE_ACCESS_KEY ATHENA_STORAGE_SECRET_KEY
export ATHENA_TEST_MINIO_ENDPOINT ATHENA_TEST_MINIO_BUCKET
export ATHENA_TEST_MINIO_ACCESS_KEY ATHENA_TEST_MINIO_SECRET_KEY ATHENA_MCP_SECRET_KEY

unset _ATHENA_TEST_ROOT
unset _ATHENA_TEST_MINIO_USER _ATHENA_TEST_MINIO_PASSWORD
unset _ATHENA_TEST_ENV_TARGET _ATHENA_TEST_DATABASE_HOST
unset _ATHENA_TEST_DATABASE_NAME _ATHENA_TEST_STORAGE_BUCKET
