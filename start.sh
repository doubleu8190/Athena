#!/usr/bin/env bash
# =============================================================================
# Athena 项目启动脚本
# =============================================================================
# 功能总览：
#   1. 环境变量自动加载（支持 .env 覆盖默认值）
#   2. 依赖健康检查（python3 / node / npm / pip 模块 / npm 模块）
#   3. 命令行参数驱动的多模式启动：
#        start|all      — 同时启动后端 + 前端（生产开发默认）
#        backend        — 仅启动后端 FastAPI
#        frontend|desktop — 仅启动 Electron 前端
#        stop           — 停止所有由本脚本启动的进程
#        restart        — 停止后再启动
#        status         — 查看进程与端口状态
#        build          — 构建后端测试 + 前端生产产物 + Electron 打包（可选）
#        test           — 运行后端 pytest 测试套件
#        install|setup  — 安装/校验后端 + 前端依赖
#        logs           — 实时查看日志（tail -f）
#        help           — 显示使用说明
#   4. 进程与端口冲突检测：PID 文件管理 + 占用端口的自动清理提示
#   5. 彩色结构化日志：INFO / WARN / ERROR / STEP
#   6. 信号陷阱（SIGINT/SIGTERM/EXIT）：优雅终止所有子进程
#
# 用法示例：
#   ./start.sh                       # 默认：同时启动前后端（开发模式）
#   ./start.sh backend               # 仅启动后端
#   ./start.sh frontend              # 仅启动前端
#   ./start.sh stop                  # 停止所有
#   ./start.sh restart               # 重启
#   ./start.sh status                # 状态检查
#   ./start.sh install               # 安装前后端依赖
#   ./start.sh test                  # 运行后端测试
#   ./start.sh build                 # 生产构建
#   DEBUG=true ./start.sh backend    # 强制 debug 模式（.env 之外再加临时覆盖）
#
# macOS / Linux bash 环境可直接执行；Windows 请使用 Git Bash / WSL。
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# 0. 基础环境：确定项目根目录（无论从哪里调用脚本都能定位正确路径）
# ---------------------------------------------------------------------------

# 脚本所在绝对目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 项目根 = 脚本所在目录（此脚本应放在 Athena 根目录）
PROJECT_ROOT="${SCRIPT_DIR}"

# 各子路径常量
BACKEND_DIR="${PROJECT_ROOT}"                              # 后端 Python 项目根
FRONTEND_DIR="${PROJECT_ROOT}/desktop"                     # Electron 前端根（方案 B 后的目录名）
RUN_DIR="${PROJECT_ROOT}/.run"                             # PID / 锁 / 临时文件
LOG_DIR="${PROJECT_ROOT}/logs"                             # 运行日志
ENV_FILE="${PROJECT_ROOT}/.env"                            # 用户环境变量
ENV_EXAMPLE="${PROJECT_ROOT}/.env.example"                 # 环境变量参考

PID_BACKEND="${RUN_DIR}/backend.pid"
PID_FRONTEND="${RUN_DIR}/frontend.pid"
LOG_BACKEND="${LOG_DIR}/backend.log"
LOG_FRONTEND="${LOG_DIR}/frontend.log"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

# ---------------------------------------------------------------------------
# 1. 颜色定义 / 日志函数
# ---------------------------------------------------------------------------

if [[ -t 1 ]]; then
  C_RED=$'\033[0;31m'
  C_GREEN=$'\033[0;32m'
  C_YELLOW=$'\033[1;33m'
  C_BLUE=$'\033[0;34m'
  C_CYAN=$'\033[0;36m'
  C_BOLD=$'\033[1m'
  C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""; C_BOLD=""; C_RESET=""
fi

log_info()    { echo -e "${C_BLUE}[INFO]${C_RESET}    $*"; }
log_step()    { echo -e "${C_CYAN}[STEP]${C_RESET}    ${C_BOLD}$*${C_RESET}"; }
log_success() { echo -e "${C_GREEN}[OK]${C_RESET}      $*"; }
log_warn()    { echo -e "${C_YELLOW}[WARN]${C_RESET}    $*" >&2; }
log_error()   { echo -e "${C_RED}[ERROR]${C_RESET}   $*" >&2; }

# 带退出码的 fatal
fatal() { log_error "$*"; exit 1; }

# ---------------------------------------------------------------------------
# 2. 加载环境变量与默认值
# ---------------------------------------------------------------------------

# 若存在 .env 则按顺序加载：系统环境变量 > 用户 .env > 脚本默认值
# shellcheck disable=SC1090
[[ -f "${ENV_FILE}" ]] && set -a && source "${ENV_FILE}" && set +a || true

# --- Python 解释器选择（优先使用 .venv，否则系统 python3）---
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python3"
if [[ -x "${PYTHON_BIN}" ]]; then
  log_info "检测到虚拟环境 .venv，使用 ${PYTHON_BIN}"
else
  PYTHON_BIN="$(command -v python3 || echo python3)"
  log_warn "未检测到 .venv，使用系统 ${PYTHON_BIN}（若依赖缺失请先 ./start.sh install）"
fi
export PYTHON_BIN

# --- 默认值（在未设置时生效）---
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"
export DEBUG="${DEBUG:-true}"
# 前端端口对齐 electron.vite.config.ts (默认 5173)
export FRONTEND_PORT="${FRONTEND_PORT:-5173}"
# LLM 默认值
export LLM_PROVIDER="${LLM_PROVIDER:-openai}"
export LLM_MODEL="${LLM_MODEL:-gpt-4o}"
export LLM_API_KEY="${LLM_API_KEY:-}"
export LLM_BASE_URL="${LLM_BASE_URL:-}"
# 数据库默认路径（必须相对于 Athena 根，因此用绝对路径规范化）
export SQLITE_DB_PATH="${SQLITE_DB_PATH:-./data/athena.db}"
export CHROMADB_PATH="${CHROMADB_PATH:-./data/chromadb}"
# 确保 data 目录存在（SQLite 目录不存在会导致首次启动失败）
mkdir -p "${PROJECT_ROOT}/data"

# ---------------------------------------------------------------------------
# 3. 通用工具函数
# ---------------------------------------------------------------------------

# 3.1 端口占用检测：返回占用 PID（没有则空）
port_pid() {
  local p="$1"
  # macOS / Linux 兼容：lsof 在两边都有
  lsof -iTCP:"${p}" -sTCP:LISTEN -t 2>/dev/null || true
}

# 3.2 健康等待：最多 N 秒，循环等待条件命令返回 0
wait_for() {
  local timeout="$1"; shift
  local cmd="$*"
  local waited=0
  while ! eval "${cmd}" >/dev/null 2>&1; do
    if (( waited >= timeout )); then
      return 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  return 0
}

# 3.3 后端健康检查：通过 HTTP /api/health 接口
# 注意：后端 health 路由注册在 api_router 下（prefix=/api），
# 因此完整路径为 /api/health，而非 /health。
BACKEND_HEALTH_PATH="${BACKEND_HEALTH_PATH:-/api/health}"
backend_healthy() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS "http://${HOST}:${PORT}${BACKEND_HEALTH_PATH}" >/dev/null 2>&1
}

# 3.4 进程存活（PID 文件）：PID 存在且在运行
pid_alive() {
  local pidfile="$1"
  [[ -f "${pidfile}" ]] || return 1
  local pid
  pid="$(cat "${pidfile}" 2>/dev/null || true)"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null
}

# 3.5 优雅停止某个 PID（发送 SIGTERM，等待最多 10s，再 SIGKILL）
stop_pid() {
  local pidfile="$1" name="$2"
  if pid_alive "${pidfile}"; then
    local pid
    pid="$(cat "${pidfile}")"
    log_step "停止 ${name} (PID=${pid})"
    kill -TERM "${pid}" 2>/dev/null || true
    # 等待退出，最多 10 秒
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "${pid}" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "${pid}" 2>/dev/null; then
      log_warn "${name} 未在 10s 内退出，发送 SIGKILL"
      kill -KILL "${pid}" 2>/dev/null || true
    fi
    rm -f "${pidfile}"
    log_success "${name} 已停止"
  else
    rm -f "${pidfile}"  # 清理陈旧 PID 文件
    log_info "${name} 未运行"
  fi
}

# ---------------------------------------------------------------------------
# 4. 依赖检查
# ---------------------------------------------------------------------------

check_dependencies_core() {
  log_step "基础命令依赖检查"
  local missing=()
  command -v python3 >/dev/null 2>&1 || missing+=("python3")
  command -v pip3    >/dev/null 2>&1 || missing+=("pip3")
  command -v node    >/dev/null 2>&1 || missing+=("node")
  command -v npm     >/dev/null 2>&1 || missing+=("npm")
  if (( ${#missing[@]} > 0 )); then
    fatal "缺少必需命令：${missing[*]}。请先安装后再运行此脚本。"
  fi
  log_success "基础命令就绪：python3=$(python3 --version | awk '{print $2}')  node=$(node --version)"
}

# 后端 Python 依赖是否就绪（通过 import athena.main 判断是否有缺包）
check_dependencies_backend() {
  log_step "后端 Python 依赖检查"
  if ! PYTHONPATH="${PROJECT_ROOT}" "${PYTHON_BIN}" -c "import athena.main, fastapi, uvicorn, pydantic_settings, structlog, langchain" >/dev/null 2>&1; then
    log_warn "后端依赖缺失，建议先执行：${C_BOLD}./start.sh install${C_RESET}"
    return 1
  fi
  log_success "后端 Python 依赖就绪"
}

# 前端 npm 依赖是否就绪（简单看 node_modules 是否存在）
check_dependencies_frontend() {
  log_step "前端 npm 依赖检查"
  if [[ ! -d "${FRONTEND_DIR}/node_modules" ]]; then
    log_warn "前端 node_modules 缺失，建议先执行：${C_BOLD}./start.sh install${C_RESET}"
    return 1
  fi
  log_success "前端 npm 依赖就绪"
}

# LLM API Key 存在性提示（非阻塞）
check_llm_key() {
  if [[ -z "${LLM_API_KEY}" && "${LLM_PROVIDER}" != "ollama" ]]; then
    log_warn "未设置 LLM_API_KEY（当前 Provider=${LLM_PROVIDER}）。"
    log_warn "  建议复制 ${ENV_EXAMPLE} -> ${ENV_FILE} 并填入有效密钥。"
    log_warn "  否则后端 LLM 调用将失败。"
  else
    log_success "LLM 配置：Provider=${LLM_PROVIDER}, Model=${LLM_MODEL}"
  fi
}

# ---------------------------------------------------------------------------
# 5. 停止命令（stop）
# ---------------------------------------------------------------------------

cmd_stop() {
  log_step "停止所有 Athena 相关进程"
  stop_pid "${PID_FRONTEND}" "Electron 前端"
  stop_pid "${PID_BACKEND}"  "FastAPI 后端"
  # 清理可能仍在占用端口的遗留进程（仅针对默认端口）
  local bpid fpid
  bpid="$(port_pid "${PORT}")"
  fpid="$(port_pid "${FRONTEND_PORT}")"
  [[ -n "${bpid}" ]] && log_warn "端口 ${PORT} 仍被 PID=${bpid} 占用（非本脚本启动？需手动清理）"
  [[ -n "${fpid}" ]] && log_warn "端口 ${FRONTEND_PORT} 仍被 PID=${fpid} 占用（非本脚本启动？需手动清理）"
  log_success "停止完成"
}

# ---------------------------------------------------------------------------
# 6. 状态命令（status）
# ---------------------------------------------------------------------------

cmd_status() {
  echo ""
  echo -e "${C_BOLD}========== Athena 运行状态 ==========${C_RESET}"
  echo ""
  echo -e "  项目根:       ${PROJECT_ROOT}"
  echo -e "  .env 文件:    ${ENV_FILE}$([[ -f "${ENV_FILE}" ]] && echo -e " ${C_GREEN}(存在)${C_RESET}" || echo -e " ${C_YELLOW}(未生成，使用默认值)${C_RESET}")"
  echo ""
  echo -e "  ${C_BOLD}[后端 FastAPI]${C_RESET}"
  if pid_alive "${PID_BACKEND}"; then
    echo -e "    进程:       ${C_GREEN}运行中 (PID=$(cat "${PID_BACKEND}"))${C_RESET}"
  else
    echo -e "    进程:       ${C_YELLOW}未运行${C_RESET}"
  fi
  echo -e "    地址:       http://${HOST}:${PORT}"
  echo -e "    健康检查:   " | tr -d '\n'
  if backend_healthy; then
    echo -e "${C_GREEN}${BACKEND_HEALTH_PATH} OK${C_RESET}"
  else
    echo -e "${C_YELLOW}未响应${C_RESET}"
  fi
  local bpid
  bpid="$(port_pid "${PORT}")"
  echo -e "    端口 ${PORT}:  " | tr -d '\n'
  [[ -n "${bpid}" ]] && echo -e "${C_YELLOW}被占用 (PID=${bpid})${C_RESET}" || echo -e "${C_GREEN}空闲${C_RESET}"
  echo -e "    日志:       ${LOG_BACKEND}"
  echo ""
  echo -e "  ${C_BOLD}[前端 Electron]${C_RESET}"
  if pid_alive "${PID_FRONTEND}"; then
    echo -e "    进程:       ${C_GREEN}运行中 (PID=$(cat "${PID_FRONTEND}"))${C_RESET}"
  else
    echo -e "    进程:       ${C_YELLOW}未运行${C_RESET}"
  fi
  echo -e "    目录:       ${FRONTEND_DIR}"
  echo -e "    Vite 端口:  ${FRONTEND_PORT}"
  local fpid
  fpid="$(port_pid "${FRONTEND_PORT}")"
  echo -e "    端口 ${FRONTEND_PORT}: " | tr -d '\n'
  [[ -n "${fpid}" ]] && echo -e "${C_YELLOW}被占用 (PID=${fpid})${C_RESET}" || echo -e "${C_GREEN}空闲${C_RESET}"
  echo -e "    日志:       ${LOG_FRONTEND}"
  echo ""
  echo -e "${C_BOLD}=====================================${C_RESET}"
  echo ""
}

# ---------------------------------------------------------------------------
# 7. 安装命令（install / setup）
# ---------------------------------------------------------------------------

cmd_install() {
  log_step "安装 / 校验后端 Python 依赖"
  check_dependencies_core
  # 优先使用虚拟环境（若存在 .venv / venv），否则用户全局 pip
  local py_pip="pip3"
  if [[ -x "${PROJECT_ROOT}/.venv/bin/pip" ]]; then
    py_pip="${PROJECT_ROOT}/.venv/bin/pip"
    log_info "检测到虚拟环境 .venv，使用 ${py_pip}"
  fi
  ${py_pip} install -e "${PROJECT_ROOT}[dev]" \
    || fatal "后端依赖安装失败，请检查 pip 源或网络。"
  log_success "后端依赖安装完成"

  log_step "安装 / 校验前端 npm 依赖"
  (
    cd "${FRONTEND_DIR}"
    npm install --no-audit --no-fund || fatal "前端依赖安装失败。"
  )
  log_success "前端依赖安装完成"

  log_step "生成 .env 模板（若不存在）"
  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    log_success "已生成 ${ENV_FILE}，请按需填写 LLM_API_KEY 等配置。"
  else
    log_info "${ENV_FILE} 已存在，跳过覆盖。"
  fi

  log_success "依赖安装全部完成，可运行 ${C_BOLD}./start.sh${C_RESET} 启动。"
}

# ---------------------------------------------------------------------------
# 8. 测试命令（test）
# ---------------------------------------------------------------------------

cmd_test() {
  log_step "运行后端测试套件 (pytest)"
  check_dependencies_core
  check_dependencies_backend || true
  (
    cd "${BACKEND_DIR}"
    PYTHONPATH="${BACKEND_DIR}" "${PYTHON_BIN}" -m pytest tests/ -v --tb=short \
      || fatal "测试失败，请查看上方输出。"
  )
  log_success "后端测试全部通过"
}

# ---------------------------------------------------------------------------
# 9. 构建命令（build）
# ---------------------------------------------------------------------------

cmd_build() {
  log_step "开始生产构建（后端测试 + 前端构建 + 可选 Electron 打包）"
  # 9.1 先跑后端测试（快速回归）
  cmd_test

  # 9.2 前端构建
  log_step "前端生产构建：electron-vite build"
  check_dependencies_frontend || fatal "前端依赖缺失，请先 ./start.sh install"
  (
    cd "${FRONTEND_DIR}"
    npm run typecheck || fatal "TypeScript 类型检查失败。"
    npm run build || fatal "前端构建失败。"
  )
  log_success "前端构建完成 -> ${FRONTEND_DIR}/out"

  # 9.3 可选：electron-builder 打包（需要交互，默认跳过，提供明确提示）
  log_info "如需进一步生成桌面安装包，可手动执行："
  log_info "  ${C_BOLD}cd ${FRONTEND_DIR} && npm run electron:build${C_RESET}"
  log_success "构建完成"
}

# ---------------------------------------------------------------------------
# 10. 后端启动（cmd_backend）
# ---------------------------------------------------------------------------

start_backend() {
  log_step "启动后端 FastAPI（host=${HOST}, port=${PORT}, debug=${DEBUG}）"

  # 端口占用检查
  local bpid
  bpid="$(port_pid "${PORT}")"
  if [[ -n "${bpid}" ]]; then
    fatal "端口 ${PORT} 已被 PID=${bpid} 占用。请先执行 ${C_BOLD}./start.sh stop${C_RESET} 或手动释放该端口。"
  fi

  check_dependencies_backend || return 1
  check_llm_key

  # 以前台 + 日志文件双重方式运行（teeing），然后用 nohup 分离
  (
    cd "${BACKEND_DIR}"
    # 脚本子 shell 退出后父进程仍存活：使用 exec nohup 形式
    nohup env PYTHONPATH="${BACKEND_DIR}" \
      HOST="${HOST}" PORT="${PORT}" DEBUG="${DEBUG}" \
      LLM_PROVIDER="${LLM_PROVIDER}" LLM_MODEL="${LLM_MODEL}" \
      LLM_API_KEY="${LLM_API_KEY}" LLM_BASE_URL="${LLM_BASE_URL}" \
      SQLITE_DB_PATH="${SQLITE_DB_PATH}" CHROMADB_PATH="${CHROMADB_PATH}" \
      "${PYTHON_BIN}" -u athena/main.py \
      >> "${LOG_BACKEND}" 2>&1 &
    echo $! > "${PID_BACKEND}"
  )

  # 等待健康
  local timeout=30
  log_info "等待后端就绪（最多 ${timeout}s）..."
  if wait_for "${timeout}" backend_healthy; then
    log_success "后端已就绪 -> http://${HOST}:${PORT}  (PID=$(cat "${PID_BACKEND}"))"
    log_info "    健康检查: http://${HOST}:${PORT}${BACKEND_HEALTH_PATH}"
    log_info "    API 文档: http://${HOST}:${PORT}/docs"
    log_info "    实时日志: ${C_BOLD}./start.sh logs backend${C_RESET}"
  else
    log_error "后端未能在 ${timeout}s 内就绪。以下为最近日志片段："
    tail -n 40 "${LOG_BACKEND}" >&2 || true
    return 1
  fi
}

cmd_backend() { start_backend; }

# ---------------------------------------------------------------------------
# 11. 前端启动（cmd_frontend / cmd_desktop）
# ---------------------------------------------------------------------------

start_frontend() {
  log_step "启动 Electron 前端 (electron-vite dev)"

  # 如果后端没启动，给出警告但不阻塞（允许用户分开启动）
  if ! backend_healthy; then
    log_warn "后端尚未就绪（http://${HOST}:${PORT}/health 无响应）。"
    log_warn "  前端仍会启动，但 API 调用会失败，直到后端可用。"
  fi

  check_dependencies_frontend || return 1

  (
    cd "${FRONTEND_DIR}"
    # electron-vite dev 会启动 Vite(端口 5173) 并拉起 Electron 主进程
    nohup env \
      HOST="${HOST}" PORT="${PORT}" \
      API_BASE="http://${HOST}:${PORT}" \
      WS_BASE="ws://${HOST}:${PORT}" \
      npm run dev \
      >> "${LOG_FRONTEND}" 2>&1 &
    echo $! > "${PID_FRONTEND}"
  )

  # 给前端最多 15 秒启动窗口：检查 Vite 端口是否打开
  local timeout=15
  log_info "等待前端就绪（最多 ${timeout}s）..."
  if wait_for "${timeout}" "lsof -iTCP:${FRONTEND_PORT} -sTCP:LISTEN -t >/dev/null 2>&1"; then
    log_success "前端已就绪 (PID=$(cat "${PID_FRONTEND}"))"
    log_info "    Vite 地址:  http://localhost:${FRONTEND_PORT}"
    log_info "    Electron 窗口应由系统自动弹出（首次启动可能需要几秒冷启动）"
    log_info "    实时日志:   ${C_BOLD}./start.sh logs frontend${C_RESET}"
  else
    log_warn "前端 Vite 端口未在 ${timeout}s 内就绪。请检查日志：tail -f ${LOG_FRONTEND}"
  fi
}

cmd_frontend() { start_frontend; }
cmd_desktop()  { start_frontend; }

# ---------------------------------------------------------------------------
# 12. 全栈启动（cmd_start / cmd_all）
# ---------------------------------------------------------------------------

cmd_start() {
  # 先启动后端，再启动前端，保证依赖顺序
  start_backend || fatal "后端启动失败，已中止全栈启动。"
  start_frontend || log_warn "前端启动失败，你仍可以单独用 ./start.sh frontend 排查。"
  echo ""
  log_success "🎉 Athena 全栈启动完成！"
  cmd_status
}
cmd_all() { cmd_start; }

# ---------------------------------------------------------------------------
# 13. 重启命令（restart）
# ---------------------------------------------------------------------------

cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

# ---------------------------------------------------------------------------
# 14. 日志查看（logs）
# ---------------------------------------------------------------------------

cmd_logs() {
  local target="${1:-all}"
  case "${target}" in
    backend)
      log_info "实时跟踪后端日志：${LOG_BACKEND}"
      [[ -f "${LOG_BACKEND}" ]] || { log_warn "日志不存在，等待生成..."; touch "${LOG_BACKEND}"; }
      exec tail -f "${LOG_BACKEND}"
      ;;
    frontend|desktop)
      log_info "实时跟踪前端日志：${LOG_FRONTEND}"
      [[ -f "${LOG_FRONTEND}" ]] || { log_warn "日志不存在，等待生成..."; touch "${LOG_FRONTEND}"; }
      exec tail -f "${LOG_FRONTEND}"
      ;;
    all)
      log_info "同时跟踪两份日志（Ctrl+C 退出）"
      [[ -f "${LOG_BACKEND}"  ]] || touch "${LOG_BACKEND}"
      [[ -f "${LOG_FRONTEND}" ]] || touch "${LOG_FRONTEND}"
      # -F 处理日志轮转，在 macOS/linux tail 都支持
      exec tail -F "${LOG_BACKEND}" "${LOG_FRONTEND}"
      ;;
    *)
      fatal "logs 目标未知：${target}。有效值：backend | frontend | all"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# 15. 帮助（help）
# ---------------------------------------------------------------------------

cmd_help() {
  cat <<'HELP'
  ___   _   _                     
 / _ \ | |_| |__   ___ _ __   __ _
| | | || __| '_ \ / _ \ '_ \ / _` |
| |_| || |_| | | |  __/ | | | (_| |
 \___/  \__|_| |_|\___|_| |_|\__,_|

  Athena 自主 AI Agent 桌面应用 — 统一启动脚本

USAGE:
  ./start.sh [COMMAND] [OPTIONS]

COMMANDS:
  start | all          同时启动后端 + 前端（默认命令，不传参即为此）
  backend              仅启动后端 FastAPI
  frontend | desktop   仅启动 Electron 前端
  stop                 停止所有由本脚本启动的进程
  restart              停止后再启动
  status               显示进程 / 端口 / 健康状态
  build                后端测试 + 前端类型检查 + 生产构建
  test                 仅运行后端 pytest 测试套件
  install | setup      安装/校验后端 + 前端依赖；无 .env 时自动生成
  logs [TARGET]        实时查看日志：TARGET=backend|frontend|all (默认 all)
  help                 显示本帮助

ENVIRONMENT (可在 .env 中配置，或执行时临时覆盖):
  HOST                 后端监听地址            默认 127.0.0.1
  PORT                 后端监听端口            默认 8000
  DEBUG                后端 debug / reload     默认 true
  FRONTEND_PORT        Vite dev server 端口    默认 5173
  LLM_PROVIDER         LLM 提供商               默认 openai
  LLM_MODEL            LLM 模型                 默认 gpt-4o
  LLM_API_KEY          LLM API Key             默认 "" (强烈建议填写)
  LLM_BASE_URL         自定义 API 基址 (可选)
  SQLITE_DB_PATH       SQLite 文件路径         默认 ./data/athena.db
  CHROMADB_PATH        ChromaDB 存储路径       默认 ./data/chromadb

EXAMPLES:
  ./start.sh                         # 一键启动前后端（日常开发）
  ./start.sh install                 # 首次克隆后，安装依赖 + 生成 .env
  ./start.sh test                    # 跑后端测试
  ./start.sh build                   # 生产构建
  DEBUG=true ./start.sh backend      # 仅后端调试
  LLM_PROVIDER=ollama ./start.sh     # 本地 Ollama 全栈启动
  ./start.sh logs backend            # 实时看后端日志
  ./start.sh stop                    # 停止所有

EXIT CODES:
  0   成功
  1   通用错误（缺依赖 / 端口占用 / 启动超时 等）
  2   参数错误

HELP
}

# ---------------------------------------------------------------------------
# 16. 信号陷阱：Ctrl+C / 退出时优雅清理子进程
# ---------------------------------------------------------------------------

cleanup_on_exit() {
  local rc=$?
  # 只有前台模式（脚本未手动 stop）才做提示；不重复 kill 以免误杀
  if (( rc != 0 )) && pid_alive "${PID_BACKEND}"; then
    log_warn "脚本异常退出 (code=${rc})，但后端进程仍在运行。"
    log_warn "  如需停止，请执行 ${C_BOLD}./start.sh stop${C_RESET}"
  fi
  exit ${rc}
}
trap cleanup_on_exit EXIT

# 用户在前台 Ctrl+C — 这种情况脚本是交互式在跑，应主动清理前后端
handle_interrupt() {
  echo ""
  log_warn "收到中断信号，清理所有由本脚本启动的进程..."
  stop_pid "${PID_FRONTEND}" "Electron 前端" >/dev/null 2>&1 || true
  stop_pid "${PID_BACKEND}"  "FastAPI 后端"  >/dev/null 2>&1 || true
  exit 130
}
trap handle_interrupt INT TERM

# ---------------------------------------------------------------------------
# 17. 命令分派
# ---------------------------------------------------------------------------

main() {
  local cmd="${1:-start}"
  # 将子命令 shift 掉（例如 logs backend -> logs 的参数是 backend）
  shift || true

  # 分派前先做核心依赖检查（除了 help/status/stop，其他都需要）
  case "${cmd}" in
    help|-h|--help)
      cmd_help
      ;;
    status)
      cmd_status
      ;;
    stop)
      cmd_stop
      ;;
    start|all)
      cmd_start
      ;;
    backend)
      cmd_backend
      ;;
    frontend|desktop)
      cmd_frontend
      ;;
    restart)
      cmd_restart
      ;;
    install|setup)
      cmd_install
      ;;
    test)
      cmd_test
      ;;
    build)
      cmd_build
      ;;
    logs)
      cmd_logs "${1:-all}"
      ;;
    *)
      log_error "未知命令：${cmd}"
      echo ""
      cmd_help
      exit 2
      ;;
  esac
}

main "$@"
