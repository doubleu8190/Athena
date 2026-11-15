#!/usr/bin/env bash
# ==============================================================================
# Athena v2 后端通用容器入口调度脚本
# ==============================================================================
# 业务意图：
# 1. 默认或传入 "api" 时，启动 FastAPI Web API 服务（Uvicorn 多协程应用服务器）。
# 2. 传入 "worker" 时，启动独立 Run Worker 消费后台大模型与工具执行任务。
# 3. 传入 "knowledge-worker" 时，启动可选的本地知识库异步解析 Worker。
# 4. 传入 "memory-worker" 时，启动可选的记忆生命周期 Worker；开关关闭时正常退出。
# 5. 传入 "migrate" 时，执行 Alembic 数据库架构自动迁移至最新 head 版本。
# 6. 传入其他任意命令时，透传执行（支持自定义命令、python 脚本或交互式 shell）。
# ==============================================================================

set -e

case "$1" in
  api|"")
    # 默认启动 FastAPI API 服务
    # 使用 --factory 延迟组装生命周期，绑定 0.0.0.0:8000 供外部与 Nginx 反向代理访问；
    # 开启 --proxy-headers 并信任容器网段反向代理（--forwarded-allow-ips='*'），确保正确解析 X-Forwarded-* 标头
    shift $(($# > 0 ? 1 : 0))
    exec uvicorn main:create_app --factory --app-dir backend/src --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' "$@"
    ;;
  worker)
    # 启动独立 Run Worker 进程
    shift
    exec python -m run_worker "$@"
    ;;
  knowledge-worker)
    # 启动可选的知识库解析 Worker 进程
    shift
    exec python -m knowledge_worker "$@"
    ;;
  memory-worker)
    # 先同时解析总开关与生命周期开关，避免总开关关闭时掩盖非法配置或矛盾开关组合。
    memory_flag="$(printf '%s' "${ATHENA_MEMORY_ENABLED:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
    case "$memory_flag" in
      ''|0|false|no|off) memory_flag=false ;;
      1|true|yes|on) memory_flag=true ;;
      *)
        printf '%s\n' "ATHENA_MEMORY_ENABLED is invalid." >&2
        exit 2
        ;;
    esac
    lifecycle_flag="$(printf '%s' "${ATHENA_MEMORY_LIFECYCLE_ENABLED:-}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
    case "$lifecycle_flag" in
      ''|0|false|no|off) lifecycle_flag=false ;;
      1|true|yes|on) lifecycle_flag=true ;;
      *)
        printf '%s\n' "ATHENA_MEMORY_LIFECYCLE_ENABLED is invalid." >&2
        exit 2
        ;;
    esac
    # 总开关关闭时禁止单独启用生命周期开关，明确报错并快速失败。
    if [ "$memory_flag" = "false" ] && [ "$lifecycle_flag" = "true" ]; then
      printf '%s\n' "ATHENA_MEMORY_LIFECYCLE_ENABLED=true requires ATHENA_MEMORY_ENABLED=true" >&2
      exit 2
    fi
    # 总开关或生命周期开关关闭时正常退出，允许同一 Compose 模板按环境变量独立启停。
    if [ "$memory_flag" = "false" ]; then
      printf '%s\n' "Memory Worker disabled; exiting."
      exit 0
    fi
    if [ "$lifecycle_flag" = "false" ]; then
      printf '%s\n' "Memory Worker lifecycle disabled; exiting."
      exit 0
    fi
    shift
    exec python -m memory_worker "$@"
    ;;
  migrate)
    # 运行 Alembic 执行数据库在线迁移
    shift
    exec alembic -c backend/alembic.ini upgrade head "$@"
    ;;
  *)
    # 允许透传任意命令（例如自定义 uvicorn 参数、python 脚本或 bash 交互调试）
    exec "$@"
    ;;
esac
