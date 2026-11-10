#!/usr/bin/env bash

set -Eeuo pipefail
# 给每个后台服务建立独立进程组，停止时可覆盖 npm/Vite 和 Uvicorn reload 子进程。
set -m

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATE=false
KNOWLEDGE_ENABLED=false
ENV_FILE=".env"
ENV_FILE_REQUIRED=false
CHILD_PIDS=()

usage() {
  cat <<'EOF'
用法：scripts/dev.sh [--env-file <file>] [--migrate]

启动本地 API、Run Worker、前端 Vite；ATHENA_KNOWLEDGE_ENABLED=true 时也启动知识库 Worker。
--env-file 指定从项目根目录解析的 shell 格式配置文件；默认使用项目根目录 .env。
--migrate 先执行 Alembic upgrade head。
EOF
}

fail() {
  printf '错误：%s\n' "$1" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --migrate) MIGRATE=true ;;
    --env-file)
      (($# >= 2)) || fail "--env-file 需要一个文件路径。"
      ENV_FILE="$2"
      ENV_FILE_REQUIRED=true
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "未知参数：$1" ;;
  esac
  shift
done

cd "$ROOT_DIR"

if [[ "$ENV_FILE" != /* ]]; then
  ENV_FILE="$ROOT_DIR/$ENV_FILE"
fi
if [[ -f "$ENV_FILE" ]]; then
  # .env 使用 shell 赋值格式；导出其中配置供 API、Worker 和前端子进程读取。
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
else
  [[ "$ENV_FILE_REQUIRED" == false ]] || fail "配置文件不存在：$ENV_FILE"
fi

[[ -x .venv/bin/python ]] || fail "未找到 .venv/bin/python；请先按 README 安装 Python 依赖。"
[[ -x .venv/bin/alembic ]] || fail "未找到 .venv/bin/alembic；请先安装后端依赖。"
[[ -x .venv/bin/uvicorn ]] || fail "未找到 .venv/bin/uvicorn；请先安装后端依赖。"
command -v npm >/dev/null 2>&1 || fail "未找到 npm；请安装 Node.js。"
[[ -d frontend/node_modules ]] || fail "未找到 frontend/node_modules；请先在 frontend 执行 npm install。"
[[ -n "${ATHENA_DATABASE_URL:-}" ]] || fail "必须在 .env 中配置 ATHENA_DATABASE_URL。"
[[ -n "${ATHENA_MCP_SECRET_KEY:-}" ]] || fail "必须在 .env 中配置 ATHENA_MCP_SECRET_KEY（生成方法见 .env.example）。"

# 与后端 env_flag 保持相同语义：去除首尾空白并接受大小写布尔值。
knowledge_flag="$(printf '%s' "${ATHENA_KNOWLEDGE_ENABLED:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
case "$knowledge_flag" in
  1|true|yes|on) KNOWLEDGE_ENABLED=true ;;
  ''|0|false|no|off) KNOWLEDGE_ENABLED=false ;;
  *) fail "ATHENA_KNOWLEDGE_ENABLED 值无效；请使用 true/false、1/0、yes/no 或 on/off。" ;;
esac

if [[ "$MIGRATE" == true ]]; then
  .venv/bin/alembic -c backend/alembic.ini upgrade head
fi

cleanup() {
  local pid
  local attempt
  local local_running
  trap - EXIT INT TERM
  # 负 PID 向该服务进程组发送信号，后代退出不影响信号覆盖范围。
  for pid in "${CHILD_PIDS[@]}"; do
    kill -TERM -- "-$pid" 2>/dev/null || true
  done
  # 最多等待五秒优雅退出，避免异常子进程让脚本永久卡在 wait。
  for ((attempt = 0; attempt < 5; attempt++)); do
    local_running=false
    for pid in "${CHILD_PIDS[@]}"; do
      if jobs -pr | grep -qx "$pid"; then local_running=true; fi
    done
    [[ "$local_running" == false ]] && break
    sleep 1
  done
  for pid in "${CHILD_PIDS[@]}"; do
    # 超时后强制结束仍存活的整个服务进程组。
    kill -KILL -- "-$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  done
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
  printf '已启动 %s（PID %s）\n' "$label" "$pid"
}

start_service "API http://127.0.0.1:8000" .venv/bin/python -m uvicorn main:create_app --factory --app-dir backend/src
start_service "Run Worker" .venv/bin/python -m run_worker
if [[ "$KNOWLEDGE_ENABLED" == true ]]; then
  start_service "Knowledge Worker" .venv/bin/python -m knowledge_worker
fi
start_service "前端 http://localhost:5173" npm --prefix frontend run dev -- --host localhost --strictPort

printf '开发服务已启动；按 Ctrl+C 停止全部服务。\n'

# Bash 3.2（macOS 默认版本）没有 wait -n，轮询 jobs 保持脚本可移植。
while true; do
  for pid in "${CHILD_PIDS[@]}"; do
    if ! jobs -pr | grep -qx "$pid"; then
      status=0
      wait "$pid" || status=$?
      printf '服务进程 %s 已退出（状态 %s），正在停止其余服务。\n' "$pid" "$status" >&2
      exit "$status"
    fi
  done
  sleep 1
done
