#!/usr/bin/env bash
# ==============================================================================
# Athena v2 后端通用容器入口调度脚本
# ==============================================================================
# 业务意图：
# 1. 默认或传入 "api" 时，启动 FastAPI Web API 服务（Uvicorn 多协程应用服务器）。
# 2. 传入 "worker" 时，启动独立 Run Worker 消费后台大模型与工具执行任务。
# 3. 传入 "knowledge-worker" 时，启动可选的本地知识库异步解析 Worker。
# 4. 传入 "migrate" 时，执行 Alembic 数据库架构自动迁移至最新 head 版本。
# 5. 传入其他任意命令时，透传执行（支持自定义命令、python 脚本或交互式 shell）。
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
