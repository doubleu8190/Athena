#!/usr/bin/env bash
# ==============================================================================
# Athena v2 Docker 容器化测试环境启动与运维脚本
# ==============================================================================
# 业务功能：
#   一键管理基于 Docker 容器化的 Athena 全栈测试环境（frontend, backend-api, backend-worker）。
#   自动检测并拉起底层外部中间件依赖（PostgreSQL, Redis, MinIO），执行数据库迁移并健康探活。
#   采用 Base Image 架构：自动检查/构建 athena-backend-base:latest 固化第三方依赖，
#   使后续每次业务代码变更重构均可达到秒级构建。
#
# 支持命令：
#   up          (默认) 一键拉起依赖、检查/构建 Base 镜像、执行迁移、启动业务容器并执行健康检查
#               选项: --build-base 强制重新构建 athena-backend-base 基础依赖镜像
#   build-base  单独执行构建 athena-backend-base:latest 基础依赖镜像
#   down        停止 Athena 业务测试容器；添加 --all 可一并停止底层依赖中间件
#   status      查看当前测试环境各容器运行状态与健康度
#   logs        追踪查看 Athena 测试业务容器日志（支持透传参数如 -f 或指定服务名）
# ==============================================================================

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_PROJECT="athena-test"
ENV_TEST_FILE=".env.test"
ENV_TEST_EXAMPLE=".env.test.example"
DEPS_COMPOSE_FILE="compose.test.yml"

# 显式清理可能从宿主机终端环境继承而来的带有 127.0.0.1 的本地环境变量，
# 避免其覆盖 .env.test 中容器所需的 host.docker.internal 导致 ConnectionRefusedError 启动报错
unset ATHENA_DATABASE_URL ATHENA_TEST_DATABASE_URL ATHENA_REDIS_URL
unset ATHENA_KNOWLEDGE_MINIO_ENDPOINT ATHENA_TEST_MINIO_ENDPOINT
unset ATHENA_FILE_MINIO_ENDPOINT ATHENA_FILE_MINIO_ACCESS_KEY ATHENA_FILE_MINIO_SECRET_KEY ATHENA_FILE_MINIO_BUCKET ATHENA_FILE_MINIO_SECURE
unset ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY ATHENA_KNOWLEDGE_MINIO_SECRET_KEY ATHENA_KNOWLEDGE_MINIO_BUCKET ATHENA_KNOWLEDGE_MINIO_SECURE
unset ATHENA_MCP_SECRET_KEY ATHENA_MCP_SECRET_KEY_VERSION ATHENA_MCP_ALLOWED_PRIVATE_HOSTS ATHENA_MCP_STDIO_BASE_ENV_NAMES
unset ATHENA_PUBLIC_ORIGIN ATHENA_COOKIE_SECURE ATHENA_LOG_LEVEL
unset ATHENA_DATABASE_POOL_SIZE ATHENA_DATABASE_MAX_OVERFLOW
unset ATHENA_RUN_WORKER_CONCURRENCY ATHENA_RUN_LEASE_SECONDS ATHENA_RUN_HEARTBEAT_SECONDS ATHENA_RUN_EXECUTION_SECONDS
unset ATHENA_RUN_CANCEL_POLL_SECONDS ATHENA_RUN_QUEUE_POLL_SECONDS ATHENA_RUN_RECLAIM_SECONDS
unset ATHENA_SKILL_TEMP_ROOT ATHENA_SKILL_TEMP_STALE_SECONDS ATHENA_FILE_ORPHAN_RETENTION_SECONDS ATHENA_FILE_MAINTENANCE_INTERVAL_SECONDS
unset ATHENA_FILES_ENABLED ATHENA_KNOWLEDGE_ENABLED ATHENA_INSTALL_KNOWLEDGE
unset PORT HF_ENDPOINT

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
  scripts/test-docker.sh [命令] [选项]

命令列表:
  up          一键拉起依赖、检查/构建 Base 镜像、执行迁移并启动测试业务容器 (默认命令)
              选项: --build-base 强制重新构建 athena-backend-base 基础依赖镜像
  build-base  单独构建 athena-backend-base:latest 基础依赖镜像
  down        停止并移除测试业务容器
              选项: --all 同时停止外部基础设施容器 (PostgreSQL, Redis, MinIO)
  status      查看测试业务容器与基础设施容器的运行状态
  logs        查看测试业务容器日志 (支持透传参数，如 -f 或指定服务名)
  -h, --help  显示本帮助说明

示例:
  scripts/test-docker.sh                 # 一键启动完整测试环境 (自动按需预构建 Base)
  scripts/test-docker.sh up --build-base # 强制重新构建 Base 依赖镜像并启动
  scripts/test-docker.sh build-base      # 单独预构建 Base 镜像
  scripts/test-docker.sh down            # 仅停止业务测试容器
  scripts/test-docker.sh down --all      # 停止所有容器（包括基础设施）
  scripts/test-docker.sh logs -f         # 实时跟踪业务容器日志
EOF
}

# 1. 检查 Docker 守护进程可用性
check_docker() {
  command -v docker >/dev/null 2>&1 || fail "未检测到 docker 命令，请先安装 Docker。"
  docker info >/dev/null 2>&1 || fail "Docker 守护进程未运行或无法连接，请先启动 Docker Desktop / Docker 引擎。"
}

# 2. 检查并确保 .env.test 存在
ensure_env_file() {
  if [[ ! -f "$ENV_TEST_FILE" ]]; then
    if [[ -f "$ENV_TEST_EXAMPLE" ]]; then
      info "未检测到 $ENV_TEST_FILE，正在从 $ENV_TEST_EXAMPLE 自动初始化..."
      cp "$ENV_TEST_EXAMPLE" "$ENV_TEST_FILE"
      success "已创建 $ENV_TEST_FILE 配置文件。"
    else
      fail "缺少环境配置文件模板 $ENV_TEST_EXAMPLE，无法初始化测试配置。"
    fi
  fi
}

# 3. 构建/检查后端基础依赖镜像 (athena-backend-base:latest)
build_base_image() {
  info "正在构建后端基础依赖镜像 athena-backend-base:latest..."
  local install_knowledge="false"
  if grep -E '^ATHENA_KNOWLEDGE_ENABLED=true' "$ENV_TEST_FILE" >/dev/null 2>&1 || \
     grep -E '^ATHENA_INSTALL_KNOWLEDGE=true' "$ENV_TEST_FILE" >/dev/null 2>&1; then
    install_knowledge="true"
  fi
  info "Base 镜像构建参数: INSTALL_KNOWLEDGE=${install_knowledge}"
  docker build \
    -f docker/Dockerfile.backend-base \
    --build-arg INSTALL_KNOWLEDGE="$install_knowledge" \
    -t athena-backend-base:latest .
  success "基础依赖镜像 athena-backend-base:latest 构建完成！"
}

ensure_base_image() {
  local force_rebuild="${1:-false}"
  if [[ "$force_rebuild" == true ]] || ! docker image inspect athena-backend-base:latest >/dev/null 2>&1; then
    if [[ "$force_rebuild" == true ]]; then
      info "收到强制重新构建 Base 镜像参数 (--build-base)..."
    else
      info "本地未检测到 athena-backend-base:latest 镜像，正在执行首次一键预构建..."
    fi
    build_base_image
  else
    info "检测到已存在 athena-backend-base:latest 基础依赖镜像，直接复用秒级构建业务镜像。"
  fi
}

# 4. 检查并自动拉起外部基础设施依赖 (compose.test.yml)
ensure_dependencies() {
  info "检查外部测试基础设施状态 (PostgreSQL, Redis, MinIO)..."
  # 拉起外部依赖容器
  docker compose -f "$DEPS_COMPOSE_FILE" up -d

  # 等待 minio-init 初始化完成测试存储桶（athena-knowledge-test, athena-files-test）
  info "等待底层基础设施就绪及 MinIO 测试存储桶初始化..."
  docker compose -f "$DEPS_COMPOSE_FILE" wait minio-init >/dev/null 2>&1 || true

  # 探活验证基础设施健康状态
  local attempt=0
  local max_attempts=30
  local ready=false
  while (( attempt < max_attempts )); do
    if docker compose -f "$DEPS_COMPOSE_FILE" ps postgres 2>/dev/null | grep -Eq "healthy|Up" && \
       docker compose -f "$DEPS_COMPOSE_FILE" ps redis 2>/dev/null | grep -Eq "healthy|Up" && \
       docker compose -f "$DEPS_COMPOSE_FILE" ps minio 2>/dev/null | grep -Eq "healthy|Up"; then
      ready=true
      break
    fi
    sleep 1
    (( attempt++ ))
  done

  if [[ "$ready" == true ]]; then
    success "外部测试基础设施已全部就绪！"
  else
    warn "部分基础设施健康检查耗时较长，继续尝试启动流程..."
  fi
}

# 执行 up 命令核心流程
cmd_up() {
  check_docker
  ensure_env_file

  local force_build_base=false
  while (($#)); do
    case "$1" in
      --build-base) force_build_base=true ;;
      *) fail "up 命令不支持的参数: $1" ;;
    esac
    shift
  done

  ensure_base_image "$force_build_base"
  ensure_dependencies

  info "执行测试数据库结构自动迁移 (Alembic migration)..."
  docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_TEST_FILE" run --rm backend-migration

  info "构建并启动 Athena 业务测试容器 (frontend, backend-api, backend-worker)..."
  docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_TEST_FILE" up -d --build

  # 从 .env.test 中提取暴露端口（缺省为 8080）
  local port="8080"
  if grep -E '^PORT=' "$ENV_TEST_FILE" >/dev/null 2>&1; then
    port=$(grep -E '^PORT=' "$ENV_TEST_FILE" | cut -d'=' -f2 | tr -d ' "' | tr -d '\r')
  fi
  port="${port:-8080}"

  info "等待 Athena 前端网关与健康探针就绪 (http://127.0.0.1:${port}/health)..."
  local attempt=0
  local max_attempts=40
  local is_healthy=false

  while (( attempt < max_attempts )); do
    if curl -s -f "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      is_healthy=true
      break
    fi
    sleep 1
    (( attempt++ ))
  done

  if [[ "$is_healthy" == true ]]; then
    printf '\n'
    success "================================================================="
    success " Athena v2 Docker 测试环境启动成功！"
    success " Web 访问入口: http://localhost:${port}"
    success " 健康检查接口: http://localhost:${port}/health"
    success "================================================================="
    printf '\n'
    info "常用运维命令说明:"
    info "  查看业务容器实时日志: scripts/test-docker.sh logs"
    info "  查看测试环境容器状态: scripts/test-docker.sh status"
    info "  重新构建 Base 依赖:   scripts/test-docker.sh up --build-base"
    info "  停止 Athena 业务容器: scripts/test-docker.sh down"
    info "  停止全部容器(含依赖): scripts/test-docker.sh down --all"
    printf '\n'
  else
    warn "健康检查在 ${max_attempts} 秒内未返回成功，容器可能仍在初始化模型或执行检查。"
    warn "请运行 'scripts/test-docker.sh logs' 查看详细容器启动日志。"
  fi
}

# 执行 down 命令
cmd_down() {
  check_docker
  local stop_all=false
  while (($#)); do
    case "$1" in
      --all) stop_all=true ;;
      *) fail "down 命令不支持的参数: $1" ;;
    esac
    shift
  done

  ensure_env_file
  info "正在停止并清理 Athena 业务测试容器 ($COMPOSE_PROJECT)..."
  docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_TEST_FILE" down

  if [[ "$stop_all" == true ]]; then
    info "正在停止并清理外部测试基础设施容器 ($DEPS_COMPOSE_FILE)..."
    docker compose -f "$DEPS_COMPOSE_FILE" down
    success "已成功停止业务容器与底层基础设施容器。"
  else
    success "已停止 Athena 业务测试容器。"
    info "底层基础设施 (PostgreSQL/Redis/MinIO) 保持运行中。若需一并停止，请执行: scripts/test-docker.sh down --all"
  fi
}

# 执行 status 命令
cmd_status() {
  check_docker
  ensure_env_file
  printf '\n\033[1;36m=== Athena 业务测试容器状态 (%s) ===\033[0m\n' "$COMPOSE_PROJECT"
  docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_TEST_FILE" ps || true

  printf '\n\033[1;36m=== 外部基础设施容器状态 (%s) ===\033[0m\n' "$DEPS_COMPOSE_FILE"
  docker compose -f "$DEPS_COMPOSE_FILE" ps || true
  printf '\n'
}

# 执行 logs 命令
cmd_logs() {
  check_docker
  ensure_env_file
  docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_TEST_FILE" logs -f "$@"
}

# 主入口调度
COMMAND="${1:-up}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$COMMAND" in
  up)
    cmd_up "$@"
    ;;
  build-base)
    check_docker
    ensure_env_file
    build_base_image
    ;;
  down)
    cmd_down "$@"
    ;;
  status)
    cmd_status "$@"
    ;;
  logs)
    cmd_logs "$@"
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    fail "未知命令: $COMMAND"
    ;;
esac
