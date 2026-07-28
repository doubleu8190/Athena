#!/bin/bash
set -e

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT="$SCRIPT_DIR"
PID_FILE="${PROJECT_ROOT}/.athena.pids"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

cleanup() {
    log "Cleaning up..."
    
    if [ -f "$PID_FILE" ]; then
        while read -r pid name; do
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                log "Stopping $name (PID: $pid)..."
                kill "$pid" 2>/dev/null || true
                sleep 1
                if kill -0 "$pid" 2>/dev/null; then
                    kill -9 "$pid" 2>/dev/null || true
                fi
            fi
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    fi
    
    log_success "All services stopped."
}

kill_port() {
    local port=$1
    local pids=$(lsof -ti:"$port" 2>/dev/null)
    if [ -n "$pids" ]; then
        log_warn "Port $port is occupied, killing processes..."
        kill -9 $pids 2>/dev/null || true
        sleep 1
    fi
}

install_deps() {
    if [ ! -d "${PROJECT_ROOT}/electron" ]; then
        log_error "Electron directory not found."
        exit 1
    fi
    
    log "Installing Electron dependencies..."
    
    # Use mirror for Electron binary download (faster in China)
    export ELECTRON_MIRROR="https://npmmirror.com/mirrors/electron/"
    
    cd "${PROJECT_ROOT}/electron" && npm install -q
    cd "${PROJECT_ROOT}/electron/renderer" && npm install -q
    cd "${PROJECT_ROOT}/electron/preload" && npm install -q 2>/dev/null || true
    log_success "Electron dependencies installed."
}

start_electron() {
    log "Building Electron app..."
    
    cd "${PROJECT_ROOT}/electron"
    
    # Build TypeScript (main + preload)
    npm run build:main
    npm run build:preload
    
    # Build renderer
    cd "${PROJECT_ROOT}/electron/renderer" && npm run build
    cd "${PROJECT_ROOT}/electron"
    
    log_success "Build completed."
    
    log "Starting Electron app..."
    
    npm run start:electron > /dev/null 2>&1 &
    local pid=$!
    echo "$pid electron" >> "$PID_FILE"
    log_success "Electron app started (PID: $pid)"
}

show_help() {
    echo "Usage: $0 [command]"
    echo ""
    echo "Commands:"
    echo "  electron     Start Electron app (default)"
    echo "  install      Install Electron dependencies"
    echo "  stop         Stop all running services"
    echo "  restart      Restart Electron app"
    echo "  status       Show status of running services"
    echo "  help         Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 electron         # Start Electron app"
    echo "  $0 install          # Install dependencies"
    echo "  $0 stop             # Stop all services"
}

show_status() {
    if [ ! -f "$PID_FILE" ]; then
        log_warn "No services running."
        return 0
    fi
    
    log "Running services:"
    echo "------------------"
    local has_running=false
    
    while read -r pid name; do
        if [ -n "$pid" ]; then
            if kill -0 "$pid" 2>/dev/null; then
                echo -e "${GREEN}✓${NC} $name (PID: $pid)"
                has_running=true
            else
                echo -e "${RED}✗${NC} $name (PID: $pid - not running)"
            fi
        fi
    done < "$PID_FILE"
    
    if [ "$has_running" = false ]; then
        log_warn "No services are currently running."
        rm -f "$PID_FILE"
    fi
}

main() {
    local command="${1:-electron}"
    
    case "$command" in
        electron)
            log "Starting Athena Electron app..."
            trap cleanup EXIT INT TERM
            
            kill_port 8000
            
            rm -f "$PID_FILE"
            
            start_electron
            
            log_success "Athena Electron app is running!"
            log "Press Ctrl+C to stop."
            wait
            ;;
        
        install)
            log "Installing Electron dependencies..."
            install_deps
            log_success "All dependencies installed successfully!"
            ;;
        
        stop)
            cleanup
            ;;
        
        restart)
            cleanup
            sleep 2
            main "electron"
            ;;
        
        status)
            show_status
            ;;
        
        help)
            show_help
            ;;
        
        *)
            log_error "Unknown command: $command"
            show_help
            exit 1
            ;;
    esac
}

main "$@"
