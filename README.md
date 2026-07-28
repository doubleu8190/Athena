# Athena — Personal AI Assistant

Athena is a personal AI assistant powered by large language models, built on LangGraph with a tool-calling agent architecture. It supports multi-IM-channel access (Telegram, WeChat, Web Console), a deterministic safety harness, a pluggable tool/skill ecosystem via the Model Context Protocol (MCP), and a built-in RAG memory system with automatic conversation insight extraction.

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│                        IM Gateway Layer                           │
│           ┌──────────┐  ┌──────────┐  ┌──────────────┐            │
│           │ Telegram │  │  WeChat  │  │  Web Console │            │
│           └────┬─────┘  └────┬─────┘  └──────┬───────┘            │
└────────────────┼─────────────┼───────────────┼────────────────────┘
                 │             │               │
                 └─────────────┼───────────────┘
                               ▼
┌───────────────────────────────────────────────────────────────────┐
│                        Athena Core                                │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │                    Agent Graph (LangGraph)                  │  │
│  │  summarize → agent → precheck → confirm → tools → loop      │  │
│  └───────────────────────┬─────────────────────────────────────┘  │
│                          │                                        │
│   ┌──────────┐  ┌────────▼────────┐  ┌──────────────────────┐     │
│   │  Context │  │   Harness Eng.  │  │  Resilience Manager  │     │
│   │  Manager │  │ (safety rules)  │  │ retry + CB + LLM dec │     │
│   └──────────┘  └─────────────────┘  └──────────┬───────────┘     │
│                                                 │                 │
│   ┌──────────────────┐  ┌──────────────────┐    │                 │
│   │  LLM Provider    │  │   RAG / Memory   │    │                 │
│   │(OpenAI/Anthropic)│  │    (ChromaDB)    │    │                 │
│   └──────────────────┘  └──────────────────┘    │                 │
└─────────────────────────────────────────────────┼─────────────────┘
                                                  │
                         ┌────────────────────────┘
                         ▼
┌───────────────────────────────────────────────────────────────────┐
│                     MCP Layer (Tool / Skill)                      │
│   ┌──────────────────┐  ┌──────────────────┐  ┌────────────────┐  │
│   │  Built-in Tools  │  │  Docker Skills   │  │  External MCP  │  │
│   │  (filesystem,    │  │  (isolated,      │  │  Servers       │  │
│   │   weather, RAG)  │  │   Squid proxy)   │  │  (HTTP/SSE)    │  │
│   └──────────────────┘  └──────────────────┘  └────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────────────┐
│                       Storage Layer                               │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐    │
│   │    SQLite    │  │    Redis     │  │     ChromaDB         │    │
│   │ (sessions,   │  │  (cache,     │  │  (vector memory,     │    │
│   │  rules, MCP) │  │   ARQ)      │  │   embeddings)        │    │
│   └──────────────┘  └──────────────┘  └──────────────────────┘    │
└───────────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────────────┐
│                   Observability (ELK Stack)                       │
│   structlog → Filebeat → Logstash → Elasticsearch → Kibana        │
│   Prometheus metrics endpoint                                     │
└───────────────────────────────────────────────────────────────────┘
```

## Key Features

### Agent Architecture
- **LangGraph-Powered Agent**: Tool-calling agent state graph with `summarize → agent → precheck → confirm → tools` loop
- **Context-Aware Summarization**: Automatic message compression via fast/cheap LLM when approaching token limits
- **Human-in-the-Loop**: Interrupt-based confirmation flow for high-risk operations (LangGraph `interrupt()`)
- **Checkpoint Persistence**: SQLite-backed checkpointer for conversation state persistence and resume

### Safety & Harness
- **Deterministic Rule Engine**: Independent of LLM — evaluates every tool call before execution
- **Rule Types**: Static blacklist, dynamic blacklist, path_permission (glob-based), cooling-off periods, quota
- **Risk Levels**: `low` / `medium` / `high` / `critical` with automatic confirmation requirements
- **Hot-Reload**: Rules cached in memory, auto-reloaded every 30s via DB poll; admin endpoint for instant reload

### Tool Resilience
- **Retry with Exponential Backoff**: Configurable max retries, jitter, retryable/non-retryable error lists
- **Circuit Breaker**: Sliding-window failure rate detection with open/half-open/closed states
- **LLM-Based Recovery Decision**: On failure, LLM can recommend retry-with-adjustment, fallback tool, user intervention, or abort
- **Fallback Resolution**: Deterministic 5-level tiebreaker (capability match → risk → server → source → name)

### MCP Ecosystem
- **Multi-Transport Support**: `stdio` (built-in, skills), `http`, `sse` (streaming)
- **Tool Registry**: State machine with `active` / `stale` / `disabled` states
- **Heartbeat & Reconnect**: Periodic health checks with automatic reconnection and tool list refresh
- **Tool Fallback**: Capability-tag based fallback resolution

### Skills System
- **Docker-Isolated Containers**: Each skill runs in its own container (non-root, read-only FS, dropped capabilities)
- **Network Isolation**: Default `network_mode: none`; domain-whitelisted access via Squid proxy when needed
- **Resource Limits**: Memory (512MB) and CPU (0.5 core) limits per skill
- **MCP Stdio Protocol**: Communication via Docker attach over stdin/stdout

### Memory & RAG
- **Local Embeddings**: `all-MiniLM-L6-v2` via sentence-transformers (no data leaves the machine)
- **ChromaDB Vector Store**: Persistent, cosine-similarity search
- **Automatic Extraction**: ARQ task extracts atomic facts and paragraph summaries from conversations
- **Semantic Deduplication**: New memories compared against existing ones before write

### Multi-Channel IM
- **Unified Message Format**: All channels normalized to `UnifiedMessage`
- **Web Console**: SSE streaming with real-time agent events (thinking, tool calls, confirmations)
- **Telegram**: Long-polling adapter (python-telegram-bot)
- **WeChat**: iLink adapter (experimental)

### Device Control
- **Android ADB**: Shell commands, screenshots, APK installation, tap simulation
- **Host Agent**: WebSocket-based with PSK challenge-response authentication

### Observability
- **Structured Logging**: structlog JSON logs
- **ELK Integration**: Filebeat → Logstash → Elasticsearch → Kibana
- **Prometheus Metrics**: `/metrics` endpoint with HTTP latency, MCP server status, tool call duration, LLM decisions
- **Audit Logs**: All important operations logged with cursor-based pagination API

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend Framework** | Python 3.14+, FastAPI 0.137 |
| **Agent Framework** | LangGraph 0.4+, LangChain Core 0.3+ |
| **LLM Providers** | OpenAI, Anthropic, DeepSeek, Mimo (any OpenAI-compatible) |
| **Database** | SQLite (aiosqlite + SQLAlchemy 2.0), Alembic migrations |
| **Cache / Broker** | Redis 7+ |
| **Vector Store** | ChromaDB 1.5+ |
| **Embeddings** | sentence-transformers (all-MiniLM-L6-v2, local) |
| **Task Queue** | ARQ 0.26+ |
| **MCP** | fastmcp 3.4+, custom MCP client |
| **Containerization** | Docker, Docker Compose |
| **Frontend** | React 18, TypeScript 5.6, Vite 6, Tailwind CSS 4, Zustand 5 |
| **Logging** | structlog, ELK Stack (Elasticsearch 9.4, Logstash, Kibana, Filebeat) |
| **Monitoring** | Prometheus client |
| **Security** | cryptography, Docker secrets, PSK device auth |

## Quick Start

### Prerequisites

- Python 3.14+
- Redis 7+
- Docker (for Skills and optional ELK stack)
- LLM API key (OpenAI-compatible / Anthropic / DeepSeek / Mimo)

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd Athena

# Install dependencies
pip install -e ".[dev]"

# Run database migrations
alembic upgrade head

# Start Redis
redis-server

# Start Athena Core
uvicorn athena.main:app --host 0.0.0.0 --port 8000
```

### Frontend Development

```bash
cd frontend
npm install
npm run dev
```

The frontend runs on `http://localhost:5173` by default.

### CLI Entry Points

```bash
# Start API server
athena-core

# Start ARQ worker (background tasks)
athena-worker
```

## Configuration

Configuration is loaded from YAML files in the `data/` directory and environment variables / Docker secrets.

### Configuration Files

Copy and edit configuration files in `data/`:

| File | Required | Purpose |
|------|----------|---------|
| `llm.yaml` | Yes | LLM provider settings (API keys via env vars/secrets) |
| `user.yaml` | Yes | User identity per channel |
| `athena.yaml` | No | System behavior (defaults shown below) |
| `mcp_servers.json` | No | MCP server seed data (Claude Desktop format) |

### llm.yaml — LLM Providers

```yaml
default_provider: mimo-v2.5-pro          # Main agent model
default_summarize_provider: deepseek-v4-flash  # Fast model for summarization/extraction

providers:
  mimo-v2.5-pro:
    api_key_env: mimo_api_key            # Env var / secret name
    base_url: https://api.xiaomimimo.com/v1
    model: mimo-v2.5-pro
    context_window: 131072
    format: openai                       # "openai" or "anthropic"
    max_tokens: 4096
    temperature: 0.7
  deepseek-v4-flash:
    api_key_env: deepseek_api_key
    base_url: https://api.deepseek.com/v1
    model: deepseek-v4-flash
    context_window: 131072
    format: openai
```

### user.yaml — Channel Identity

```yaml
user:
  telegram:
    user_id: "123456789"                 # Telegram numeric user ID
  web:
    user_id: "admin"                     # Web console identity
  wechat:
    user_id: ""                          # WeChat wxid (empty = disabled)
```

Channels with empty `user_id` are automatically disabled.

### athena.yaml — System Configuration (optional)

All values shown are defaults. Omit the file to use defaults.

```yaml
system:
  max_fallback_depth: 1                  # Max tool fallback chain depth
  global_max_concurrent_tasks: 8         # Global concurrent task limit
  session_idle_timeout_minutes: 30       # Redis session TTL
  session_expire_hours: 24               # Session expiration

harness:
  circuit_breaker_threshold: 3
  cooling_off_defaults:
    low: 0
    medium: 15
    high: 30
    critical: 60

mcp:
  heartbeat_interval_seconds: 30         # MCP server health check interval
  reconnect_backoff_max_seconds: 60      # Max reconnect backoff
  tools_list_refresh_on_reconnect: true  # Refresh tool list after reconnect

extraction:
  min_messages_since_last: 6             # Min new messages to trigger extraction
  max_messages_for_extraction: 100       # Max messages per extraction run

tool_resilience:
  retry:
    max_retries: 5
    initial_interval_ms: 200
    max_interval_ms: 30000
    backoff_multiplier: 2.0
    jitter: true
    retryable_errors:
      - TimeoutError
      - ConnectionError
      - ServiceUnavailable
      - RateLimitExceeded
      - InternalServerError
    non_retryable_errors:
      - AuthenticationError
      - InvalidParameters
      - ToolNotFound
  circuit_breaker:
    failure_rate_threshold: 0.5
    min_requests: 20
    open_duration_seconds: 30
    half_open_max_calls: 3
    half_open_success_threshold: 2
    sliding_window_size: 100
  llm_decision:
    enabled: true
    max_decisions: 3
    decision_timeout_seconds: 30
    allowed_decisions:
      - retry_with_adjustment
      - fallback_tool
      - user_intervention
      - abort
```

### Environment Variables / Secrets

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `/data` | Path to data directory |
| `SQLITE_DB_PATH` | `/data/athena.db` | SQLite database path |
| `CHROMA_PERSIST_DIR` | `/data/chroma` | ChromaDB persistence directory |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `ARQ_BROKER_URL` | `redis://localhost:6379/1` | ARQ broker URL |
| `ADMIN_API_KEY` | *(empty)* | Admin API key (X-API-Key header) |
| `DEVICE_PSK` | *(empty)* | Device WebSocket pre-shared key |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |
| `PROMETHEUS_ENABLED` | `true` | Enable Prometheus metrics endpoint |
| `CORS_ORIGINS` | `*` | Comma-separated CORS origins |
| `HF_HOME` | `/data/huggingface` | HuggingFace model cache |
| `SYSTEM_CONFIG_PATH` | `$DATA_DIR/athena.yaml` | System config path |
| `USER_CONFIG_PATH` | `$DATA_DIR/user.yaml` | User config path |
| `LLM_CONFIG_PATH` | `$DATA_DIR/llm.yaml` | LLM config path |
| `MCP_SERVERS_CONFIG` | `$DATA_DIR/mcp_servers.json` | MCP servers seed |

Secrets can also be provided as Docker secrets in `/run/secrets/<name>`.

### MCP Servers Seed (mcp_servers.json)

Claude Desktop compatible format:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "python",
      "args": ["-m", "my_mcp_server"],
      "env": { "API_KEY": "..." }
    },
    "remote-server": {
      "url": "https://mcp.example.com/sse"
    }
  }
}
```

Transport is auto-detected: `url` present → `sse`/`http`, otherwise `stdio`.

## Docker Compose

```bash
# First, create secret files
mkdir -p secrets
echo "your-admin-key" > secrets/admin_api_key
echo "your-deepseek-key" > secrets/deepseek_api_key
echo "your-mimo-key" > secrets/mimo_api_key
chmod 600 secrets/*

# Build base image (required for athena-core and arq-worker)
sh scripts/build-base.sh

# Start all services
docker compose up -d --build
```

### Services

| Service | Port | Description |
|---------|------|-------------|
| `athena-gateway` (Nginx) | 80 | Reverse proxy for frontend + API |
| `athena-core` | 8000 | FastAPI + LangGraph agent engine |
| `athena-frontend` | — | React + Vite SPA |
| `arq-worker` | — | Background tasks (conversation extraction) |
| `redis` | — | Session cache + ARQ broker |
| `skill-proxy` (Squid) | 3128 | Skill network egress proxy |

### Scripts

```bash
scripts/build-base.sh    # Build the base Docker image (Python + deps)
scripts/entrypoint.sh    # Container entry point
scripts/healthcheck.sh   # Health check script
scripts/backup.sh        # Backup script
```

## API Endpoints

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | Health check |
| GET | `/metrics` | Prometheus metrics (if enabled) |

### IM — Web Console

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/im/web/message` | Send message, receive SSE stream of agent events |
| POST | `/api/v1/im/web/message/confirm` | Confirm/reject pending operation (resumes graph) |
| GET | `/api/v1/im/web/sessions` | List web sessions |
| GET | `/api/v1/im/web/history` | Get message history for a session |
| DELETE | `/api/v1/im/web/session/{chat_id}` | Soft-delete a session |

**SSE Event Types**:

| Event | Description |
|-------|-------------|
| `agent_thinking` | LLM is generating a response |
| `text_delta` | LLM text output (streamed or full) |
| `tool_call_start` | Tool execution started |
| `tool_call_result` | Tool execution completed |
| `confirm_required` | High-risk operation needs user approval |
| `confirm_timeout` | Confirmation timed out |
| `task_completed` | Agent finished successfully |
| `task_failed` | Agent failed |
| `error` | Execution error |

### Admin (requires `X-API-Key` header)

#### MCP Servers

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/admin/mcp-servers` | List all MCP servers |
| POST | `/api/v1/admin/mcp-servers` | Register a new MCP server |
| DELETE | `/api/v1/admin/mcp-servers/{server_id}` | Remove an MCP server |
| PUT | `/api/v1/admin/mcp-servers/{server_id}/status` | Enable/disable an MCP server |

#### Skills

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/admin/skills` | List all installed skills |
| POST | `/api/v1/admin/skills` | Install a new skill (Docker image) |
| DELETE | `/api/v1/admin/skills/{skill_id}` | Uninstall a skill |

#### Devices

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/admin/devices` | List all registered devices |
| POST | `/api/v1/admin/devices` | Register a new device |
| DELETE | `/api/v1/admin/devices/{device_id}` | Deregister a device |

#### Harness Rules

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/admin/harness/rules` | List all harness rules |
| POST | `/api/v1/admin/harness/rules` | Create a new harness rule |
| PUT | `/api/v1/admin/harness/rules/{rule_id}` | Update a harness rule |
| POST | `/api/v1/admin/harness/reload` | Force immediate rule reload |

**Rule Types**:
- `blacklist` — regex-based deny patterns for tool name and arguments
- `path_permission` — glob-based path access control with read/write permissions
- `cooling_off` — mandatory wait periods before confirmation

#### Audit Logs

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/admin/audit-logs` | Search audit logs (cursor-based pagination) |

#### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/admin/dashboard` | Real-time dashboard metrics summary |
| GET | `/api/v1/admin/im/status` | IM channel adapter statuses |

### Memory

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/memory/` | List user memories |
| POST | `/api/v1/memory/search` | Semantic search memories |
| POST | `/api/v1/memory/` | Create a memory |
| PUT | `/api/v1/memory/{memory_id}` | Update a memory |
| DELETE | `/api/v1/memory/{memory_id}` | Delete a memory |

### Device

| Method | Path | Description |
|--------|------|-------------|
| WS | `/api/v1/ws/device/{device_id}` | Device agent WebSocket (PSK auth) |

## Project Structure

```
athena/                          # Backend Python package
├── main.py                      # FastAPI app factory + entry point
├── config.py                    # YAML + env config loading
├── logging_config.py            # structlog setup
├── arq_worker.py                # ARQ worker configuration
├── worker.py                    # ARQ worker entry point
├── middleware.py                # FastAPI middleware
│
├── core/                        # Core engine
│   ├── graph/                   # LangGraph agent graph
│   │   ├── agent_graph.py       # Graph builder + checkpointer factory
│   │   ├── agent_state.py       # AgentState TypedDict
│   │   ├── agent_routing.py     # Conditional edge routing
│   │   └── nodes/               # Graph nodes
│   │       ├── summarize.py     # Context-aware summarization
│   │       ├── agent.py         # LLM agent node (tool calling)
│   │       ├── precheck.py      # Harness pre-check
│   │       ├── confirm.py       # Confirmation (HITL interrupt)
│   │       └── tools.py         # Tool execution with resilience
│   ├── llm_provider/            # LLM provider abstraction
│   │   └── manager.py           # Multi-provider manager
│   ├── resilience/              # Tool resilience layer
│   │   ├── manager.py           # ResilienceManager (orchestrator)
│   │   ├── retry.py             # Retry with exponential backoff
│   │   ├── circuit_breaker.py   # Circuit breaker
│   │   ├── llm_decision.py      # LLM-based recovery decision
│   │   └── error_collector.py   # Structured error collection
│   ├── context.py               # Session lifecycle (Redis+SQLite)
│   ├── harness.py               # Safety rule engine
│   ├── rag.py                   # ChromaDB + local embeddings
│   ├── message.py               # UnifiedMessage dataclass
│   ├── prompt_loader.py         # Prompt template loading
│   └── secrets.py               # Docker secrets + env fallback
│
├── gateway/                     # IM channel adapters
│   ├── base.py                  # BaseIMAdapter ABC
│   ├── manager.py               # GatewayManager (lifecycle + routing)
│   ├── web.py                   # Web console SSE adapter
│   ├── telegram.py              # Telegram long-poll adapter
│   ├── wechat.py                # WeChat iLink adapter
│   └── confirmation.py          # Confirmation request dataclass
│
├── mcp_client/                  # MCP protocol client
│   ├── client.py                # Connection lifecycle + tool registry
│   ├── transports.py            # stdio / HTTP / SSE transports
│   ├── seed_loader.py           # Auto-register built-in servers
│   ├── tool_loader.py           # Tool cache invalidation
│   └── auth.py                  # MCP authentication
│
├── tools/                       # Built-in MCP server
│   ├── server.py                # Stdio MCP server entry point
│   ├── filesystem.py            # file_read/write/delete/search
│   ├── weather.py               # Weather tool
│   ├── weather_server.py        # Weather MCP server
│   └── rag_server.py            # RAG memory MCP server
│
├── skills/                      # Docker Skill manager
│   ├── manager.py               # Skill lifecycle (install/uninstall)
│   └── proxy.py                 # Squid proxy ACL management
│
├── devices/                     # Device control
│   ├── adb_manager.py           # Android ADB manager
│   ├── host_agent.py            # Host agent utilities
│   └── commands.py              # Device command definitions
│
├── models/                      # SQLAlchemy models (7 tables)
│   ├── base.py                  # Base + engine + session factory
│   ├── redis.py                 # Redis client singleton
│   ├── session.py               # Conversation sessions
│   ├── mcp_server.py            # MCP server registrations
│   ├── skill.py                 # Installed skills
│   ├── device.py                # Registered devices
│   ├── harness_rule.py          # Safety harness rules
│   └── audit_log.py             # Audit log entries
│
├── api/                         # FastAPI routes
│   ├── health.py                # Health check
│   ├── im.py                    # Web IM + SSE streaming
│   ├── admin.py                 # Admin endpoints (MCP, skills, etc.)
│   ├── memory.py                # Memory CRUD + search
│   ├── device.py                # Device WebSocket
│   ├── metrics.py               # Prometheus metrics definitions
│   ├── deps.py                  # Dependency injection helpers
│   └── response.py              # Standard response format
│
├── tasks/                       # ARQ tasks
│   └── conversation_extract.py  # Auto-extract insights from conversations
│
└── migrations/                  # Alembic migrations
    └── versions/                # Migration scripts
    └── env.py                   # Alembic environment

frontend/                       # React + TypeScript frontend
├── src/
│   ├── api/                    # API client + SSE
│   ├── components/             # React components
│   │   ├── chat/               # Chat UI (messages, input, sessions)
│   │   ├── dashboard/          # Dashboard widgets
│   │   ├── layout/             # Layout (header, sidebar)
│   │   └── ui/                 # Reusable UI components
│   ├── pages/                  # Page components
│   │   ├── ChatPage.tsx
│   │   ├── DashboardPage.tsx
│   │   ├── DevicesPage.tsx
│   │   ├── HarnessPage.tsx
│   │   ├── MCPServersPage.tsx
│   │   ├── MemoriesPage.tsx
│   │   ├── SkillsPage.tsx
│   │   └── AuditPage.tsx
│   ├── stores/                 # Zustand state stores
│   ├── hooks/                  # Custom React hooks
│   └── main.tsx                # Entry point
├── package.json
└── vite.config.ts

data/                           # Runtime data + config
├── athena.yaml                 # System config
├── llm.yaml                    # LLM provider config
└── user.yaml                   # User identity config

deploy/                         # Deployment configurations
├── filebeat/                   # Filebeat config
├── logstash/                   # Logstash pipelines + config
├── kibana/                     # Kibana config + dashboards
└── squid/                      # Squid proxy config

tests/                          # Test suite
├── unit/                       # Unit tests
├── integration/                # Integration tests
├── security/                   # Security tests (harness adversarial)
└── conftest.py                 # Pytest fixtures

scripts/                        # Utility scripts
doc/                            # Design documents
```

## Development

### Setup

```bash
# Install dev dependencies
pip install -e ".[dev]"
```

### Testing

```bash
# Run all tests
pytest tests/ -v

# Run unit tests only
pytest tests/unit/ -v

# Run with coverage
pytest tests/ --cov=athena --cov-report=term-missing

# Run specific test file
pytest tests/unit/test_harness.py -v
```

### Linting

```bash
# Ruff linting
ruff check athena/

# Auto-fix
ruff check athena/ --fix
```

### Database Migrations

```bash
# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback last migration
alembic downgrade -1
```

### Frontend Development

```bash
cd frontend

# Install dependencies
npm install

# Dev server with HMR
npm run dev

# Type check + build
npm run build

# Lint
npm run lint
```

## Agent Graph Flow

The agent execution follows this LangGraph state machine:

```
START
  │
  ▼
summarize ── token check: compress old messages if needed
  │
  ▼
agent ────── LLM decides: respond directly or call tools
  │
  ├── (no tool_calls) ──────────────────────────────→ END
  │
  └── (has tool_calls)
        │
        ▼
     precheck ── Harness evaluates: allowed? risk level? confirmation needed?
        │
        ▼
     confirm ── If confirmation required → interrupt() (HITL)
        │         User approves/rejects → resume
        │
        └── (approved tools)
              │
              ├── Send(tools, call_1) ─┐
              ├── Send(tools, call_2) ─┼→ parallel execution
              └── Send(tools, call_N) ─┘
                                          │
                                          ▼
                                       summarize → agent (loop back)
```

## Troubleshooting

### Common Issues

**Database connection failed**
- Ensure the `data/` directory exists and is writable
- Check `SQLITE_DB_PATH` points to a valid location
- Verify file permissions

**Redis connection refused**
- Start Redis: `redis-server`
- Check `REDIS_URL` environment variable
- Verify Redis is running on the correct host/port

**MCP server not connecting**
- Check the server's `connection_config` in admin panel
- For stdio servers: verify the command and args are correct
- For HTTP/SSE servers: verify the URL is reachable
- Check application logs for connection errors

**Skill container won't start**
- Verify Docker daemon is running
- Check the image URI and version
- Ensure Docker socket is mounted (`/var/run/docker.sock`)
- Check `docker logs athena-skill-<id>` for container errors

**Conversation extraction not working**
- Verify ARQ worker is running: `arq athena.arq_worker.WorkerSettings`
- Check `default_summarize_provider` is configured in `llm.yaml`
- Ensure at least `min_messages_since_last` new messages exist
- Check worker logs for extraction errors

**Frontend can't connect to API**
- Verify `athena-core` is running on port 8000
- Check CORS configuration (`CORS_ORIGINS` env var)
- For Docker: ensure both services are on the same network

**Harness rules not updating**
- Rules auto-reload every 30s from database
- Force immediate reload: `POST /api/v1/admin/harness/reload`
- Verify rule `enabled` is `true` and `revision` was incremented

### Logs

- **Application logs**: stdout/stderr (JSON format via structlog)
- **Docker logs**: `docker logs athena-core`
- **ELK Stack**: Access Kibana at `http://localhost:5601`
- **Audit logs**: `GET /api/v1/admin/audit-logs` (structured, queryable)

### Debug Mode

```bash
# Enable debug logging
export LOG_LEVEL=DEBUG
uvicorn athena.main:app --reload
```

## License

MIT
