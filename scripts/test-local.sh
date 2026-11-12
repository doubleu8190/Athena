#!/usr/bin/env bash
# ==============================================================================
# Athena v2 宿主机原生测试环境启动与进程管理脚本
# ==============================================================================
# 业务功能：
#   在宿主机原生 Python 虚拟环境与 Node 环境中并行启动 Athena 全栈开发与联调服务：
#     1. FastAPI 后端 API 服务 (http://127.0.0.1:8000)
#     2. 独立 Run Worker 异步任务调度进程
#     3. (可选) 独立 Knowledge Worker 知识库解析进程
#     4. 前端 Vite 开发服务器 (http://localhost:5173)
#   自动自愈检查底层基础设施容器（PostgreSQL、Redis、MinIO），自动加载测试安全凭据，
#   并在退出时通过独立进程组（kill -TERM -- "-$pid"）完整清理所有子进程，杜绝孤儿进程。
#
# 支持参数：
#   --no-migrate    跳过启动前的 Alembic 数据库结构升级迁移
#   --no-knowledge  跳过知识库 Worker 进程（适用于轻量无 OCR/Embedding 调试场景）
#   -h, --help      查看本帮助说明
# ==============================================================================

set -Eeuo pipefail
# 为后台服务创建独立进程组，保证在信号传递或异常退出时能级联覆盖 npm/Vite 与 Uvicorn 子进程
set -m

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DEPS_COMPOSE_FILE="compose.test.yml"
RUN_MIGRATE=true
RUN_KNOWLEDGE=true
CHILD_PIDS=()

# 终端输出样式辅助函数
info() {
  printf '\033[1;34m[INFO]\033[0m %s\n' "$1"
}

success() {
  printf '\033[1;32m[SUCCESS]\033[0m %s\n' "$1"
}

warn() {
  printf '\033[1;33m[WARN]\033[0m %s\n' "$1"
}

fail() {
  printf '\033[1;31m[ERROR]\033[0m %s\n' "$1" >&2
  exit 1
}

usage() {
  cat <<'EOF'
用法:
  scripts/test-local.sh [选项]

选项列表:
  --no-migrate    跳过数据库迁移 (默认会在启动前执行 Alembic upgrade head)
  --no-knowledge  跳过知识库 Worker (仅运行 API、Run Worker 与前端)
  -h, --help      显示帮助说明

示例:
  scripts/test-local.sh                 # 完整启动测试环境（含迁移与知识库）
  scripts/test-local.sh --no-migrate    # 跳过迁移快速启动
  scripts/test-local.sh --no-knowledge  # 轻量模式（不启动知识库 OCR/Embedding）
EOF
}

# 解析命令行参数
while (($#)); do
  case "$1" in
    --no-migrate)
      RUN_MIGRATE=false
      ;;
    --no-knowledge)
      RUN_KNOWLEDGE=false
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "未知参数: $1"
      ;;
  esac
  shift
done

# 1. 前置依赖自检
info "检查本地运行环境依赖..."
[[ -x .venv/bin/python ]] || fail "未找到 .venv/bin/python；请先执行 'python3.12 -m venv .venv && pip install -e .' 安装依赖。"
[[ -x .venv/bin/uvicorn ]] || fail "未找到 .venv/bin/uvicorn；请先在虚拟环境中安装 uvicorn。"
if [[ "$RUN_MIGRATE" == true ]]; then
  [[ -x .venv/bin/alembic ]] || fail "未找到 .venv/bin/alembic；请先在虚拟环境中安装 alembic。"
fi
command -v npm >/dev/null 2>&1 || fail "未找到 npm 命令；请先安装 Node.js (建议 Node 22+)。"
[[ -d frontend/node_modules ]] || fail "未找到 frontend/node_modules；请先进入 frontend 目录执行 'npm install' 或 'npm ci'。"

# 2. 基础设施依赖检测与自动自愈 (检查 5432, 6379, 9000 端口)
check_port() {
  local port="$1"
  python3 -c "import socket; s = socket.socket(); s.settimeout(0.5); s.connect(('127.0.0.1', $port)); s.close()" >/dev/null 2>&1
}

info "检查底层中间件依赖 (PostgreSQL:5432, Redis:6379, MinIO:9000)..."
if ! check_port 5432 || ! check_port 6379 || ! check_port 9000; then
  info "检测到底层依赖端口未全部监听，正在通过 $DEPS_COMPOSE_FILE 自动拉起依赖容器..."
  command -v docker >/dev/null 2>&1 || fail "未检测到 Docker，且底层中间件未运行，无法提供测试数据库/缓存支持。"
  docker compose -f "$DEPS_COMPOSE_FILE" up -d
  
  info "等待外部中间件端口开放并初始化测试存储桶..."
  docker compose -f "$DEPS_COMPOSE_FILE" wait minio-init >/dev/null 2>&1 || true

  local attempt=0
  local max_attempts=30
  local all_ready=false
  while (( attempt < max_attempts )); do
    if check_port 5432 && check_port 6379 && check_port 9000; then
      all_ready=true
      break
    fi
    sleep 1
    (( attempt++ ))
  done

  if [[ "$all_ready" == false ]]; then
    fail "等待底层中间件超时 (5432/6379/9000 未就绪)，请检查 docker compose -f $DEPS_COMPOSE_FILE ps 日志。"
  fi
  success "底层测试中间件已自愈启动就绪！"
else
  info "底层测试中间件 (PostgreSQL, Redis, MinIO) 均已就绪。"
fi

# 3. 凭据加载与环境变量注入 (严格使用 127.0.0.1 环回地址与测试库 athena_test)
read_secret_or_default() {
  local file="$1"
  local default="$2"
  if [[ -s "$ROOT_DIR/secrets/$file" ]]; then
    cat "$ROOT_DIR/secrets/$file"
  else
    printf '%s' "$default"
  fi
}

PG_USER="$(read_secret_or_default postgres_user athena_dev)"
PG_PASSWORD="$(read_secret_or_default postgres_password athena-local-only)"
MCP_KEY="$(read_secret_or_default mcp_secret_key WhIuQwJmerxc/fLQ6OdU7BYcke37vPTbFmiyePbwqq4=)"
MINIO_USER="$(read_secret_or_default minio_root_user athena-local)"
MINIO_PASSWORD="$(read_secret_or_default minio_root_password bp5cU6NnzsS0Ag8xy0aMIja1_yghqgndUuYsfqoojDQ)"

# URL 凭据严格转义，杜绝特殊字符影响连接串解析
ATHENA_DATABASE_URL="$(python3 -c "
import urllib.parse
user = urllib.parse.quote('''$PG_USER''', safe='')
pwd = urllib.parse.quote('''$PG_PASSWORD''', safe='')
print(f'postgresql+asyncpg://{user}:{pwd}@127.0.0.1:5432/athena_test')
")"

export ATHENA_DATABASE_URL
export ATHENA_REDIS_URL="redis://127.0.0.1:6379/0"
export ATHENA_DATABASE_POOL_SIZE=5
export ATHENA_DATABASE_MAX_OVERFLOW=5
export ATHENA_COOKIE_SECURE="false"
# 本地前端 Vite 运行在 5173 端口，允许来自 Vite 代理与直连的 Origin
export ATHENA_PUBLIC_ORIGIN="http://localhost:5173"
export ATHENA_LOG_LEVEL="DEBUG"

export ATHENA_MCP_SECRET_KEY="$MCP_KEY"
export ATHENA_MCP_SECRET_KEY_VERSION="1"
export ATHENA_MCP_ALLOWED_PRIVATE_HOSTS=""
export ATHENA_MCP_STDIO_BASE_ENV_NAMES=""

# 文件与技能私有对象存储
export ATHENA_FILES_ENABLED="true"
export ATHENA_FILE_MINIO_ENDPOINT="127.0.0.1:9000"
export ATHENA_FILE_MINIO_ACCESS_KEY="$MINIO_USER"
export ATHENA_FILE_MINIO_SECRET_KEY="$MINIO_PASSWORD"
export ATHENA_FILE_MINIO_BUCKET="athena-files-test"
export ATHENA_FILE_MINIO_SECURE="false"

export ATHENA_SKILL_TEMP_ROOT="./var/skill-executions"
export ATHENA_SKILL_TEMP_STALE_SECONDS=60
export ATHENA_FILE_ORPHAN_RETENTION_SECONDS=86400
export ATHENA_FILE_MAINTENANCE_INTERVAL_SECONDS=300

# 知识库对象存储与模型镜像
if [[ "$RUN_KNOWLEDGE" == true ]]; then
  export ATHENA_KNOWLEDGE_ENABLED="true"
else
  export ATHENA_KNOWLEDGE_ENABLED="false"
fi
export ATHENA_KNOWLEDGE_MINIO_ENDPOINT="127.0.0.1:9000"
export ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY="$MINIO_USER"
export ATHENA_KNOWLEDGE_MINIO_SECRET_KEY="$MINIO_PASSWORD"
export ATHENA_KNOWLEDGE_MINIO_BUCKET="athena-knowledge-test"
export ATHENA_KNOWLEDGE_MINIO_SECURE="false"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

# 继承大模型配置 ATHENA_LLM_PROFILES
if [[ -z "${ATHENA_LLM_PROFILES:-}" ]]; then
  if [[ -f .env.test ]] && grep -E '^ATHENA_LLM_PROFILES=' .env.test >/dev/null 2>&1; then
    eval "$(grep -E '^ATHENA_LLM_PROFILES=' .env.test)"
    export ATHENA_LLM_PROFILES
  elif [[ -f .env ]] && grep -E '^ATHENA_LLM_PROFILES=' .env >/dev/null 2>&1; then
    eval "$(grep -E '^ATHENA_LLM_PROFILES=' .env)"
    export ATHENA_LLM_PROFILES
  fi
fi

# 4. 执行数据库结构迁移
if [[ "$RUN_MIGRATE" == true ]]; then
  info "执行测试数据库结构升级迁移 (Alembic upgrade head)..."
  .venv/bin/alembic -c backend/alembic.ini upgrade head
  success "数据库结构迁移成功。"
else
  info "跳过数据库结构迁移 (--no-migrate)。"
fi

# 5. 进程退出与垃圾回收处理 (优雅终止各进程组)
cleanup() {
  local pid
  local attempt
  local local_running
  trap - EXIT INT TERM
  printf '\n'
  info "正在安全停止所有本地测试服务进程..."
  for pid in "${CHILD_PIDS[@]}"; do
    kill -TERM -- "-$pid" 2>/dev/null || true
  done
  for ((attempt = 0; attempt < 5; attempt++)); do
    local_running=false
    for pid in "${CHILD_PIDS[@]}"; do
      if jobs -pr | grep -qx "$pid"; then local_running=true; fi
    done
    [[ "$local_running" == false ]] && break
    sleep 1
  done
  for pid in "${CHILD_PIDS[@]}"; do
    kill -KILL -- "-$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
  success "所有测试服务进程已彻底终止并清理。"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_service() {
  local label="$1"
  local pid
  shift
  "$@" &
  pid="$!"
  CHILD_PIDS+=("$pid")
  info "已启动 $label (PID: $pid)"
}

# 6. 并行拉起前后端各服务
printf '\n'
start_service "API 服务 (http://127.0.0.1:8000)" .venv/bin/python -m uvicorn main:create_app --factory --app-dir backend/src --host 127.0.0.1 --port 8000
start_service "Run Worker 任务调度" .venv/bin/python -m run_worker
if [[ "$RUN_KNOWLEDGE" == true ]]; then
  start_service "Knowledge Worker 知识库解析" .venv/bin/python -m knowledge_worker
fi
start_service "前端 Vite 开发服务 (http://localhost:5173)" npm --prefix frontend run dev -- --host localhost --port 5173 --strictPort

printf '\n'
success "================================================================="
success " Athena v2 本地宿主机测试环境已就绪！"
success " 前端界面: http://localhost:5173"
success " API 端点: http://127.0.0.1:8000"
success " 健康探针: http://127.0.0.1:8000/health"
success " 提示: 按 Ctrl+C 可停止全部测试服务并自动回收进程组"
success "================================================================="
printf '\n'

# 7. 主进程监控子进程状态
while true; do
  for pid in "${CHILD_PIDS[@]}"; do
    if ! jobs -pr | grep -qx "$pid"; then
      status=0
      wait "$pid" || status=$?
      warn "服务进程 $pid 异常退出 (状态码: $status)，正在安全停止其余服务..." >&2
      exit "$status"
    fi
  done
  sleep 1
done
