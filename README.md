# Athena — Personal AI Assistant

Athena is a personal AI assistant powered by large language models, supporting multi-IM-channel access (Telegram, WeChat, Web Console), with a built-in safety harness, pluggable tool/skill ecosystem, and standardized external tool integration via the Model Context Protocol (MCP).

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────────┐
│  IM Gateway  │────▶│  Athena Core │────▶│  MCP Layer (Tool/Skill) │
│  Telegram    │     │  Context Mgr │     │  Built-in   Skill  Ext. │
│  WeChat      │     │  Planner     │     │  Tools      Container   │
│  Web Console │     │  Executor    │     └─────────────────────────┘
└─────────────┘     │  Harness     │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │   Storage    │
                    │ SQLite+Redis │
                    └──────────────┘
```

## Quick Start

### Prerequisites

- Python 3.14+
- Redis 7+
- Docker (for Skills)
- LLM API key (OpenAI / Anthropic / DeepSeek)

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

### Configuration

1. Copy and edit configuration files in `data/`:
   - `llm.yaml` — LLM provider settings (API keys via env vars)
   - `user.yaml` — User identity per channel
   - `athena.yaml` — System behavior (optional, defaults work)
   - `mcp_servers.yaml` — MCP server seed data

2. Set environment variables:
   ```bash
   export OPENAI_API_KEY=sk-...
   export ADMIN_API_KEY=your-admin-key
   ```

### Docker Compose

```bash
docker compose up -d
```

Starts: Redis, Athena Core, Celery Worker, Elasticsearch, Logstash, Kibana, Filebeat (+ Skill Proxy).

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=athena

# Lint
ruff check athena/
```

## Project Structure

```
athena/
├── core/              # Context Manager, Planner, Executor, Harness
│   ├── llm_provider/  # OpenAI, Anthropic, LiteLLM abstraction
│   ├── context.py     # Session lifecycle, token window mgmt
│   ├── planner.py     # LLM-powered task plan generation
│   ├── executor.py    # Subtask execution with retry/fallback
│   ├── harness.py     # Deterministic safety rule engine
│   └── memory.py      # User memory with vector sync
├── gateway/           # IM channel adapters
│   ├── base.py        # BaseIMAdapter ABC
│   ├── manager.py     # GatewayManager lifecycle
│   ├── telegram.py    # Telegram Long Poll adapter
│   ├── wechat.py      # WeChat iLink adapter (experimental)
│   └── web.py         # Web console SSE adapter
├── mcp_client/        # MCP protocol client
│   ├── client.py      # Connection mgmt, tool call
│   ├── registry.py    # Tool state machine, fallback resolve
│   └── transports.py  # stdio, HTTP, SSE transports
├── tools/             # Built-in MCP server
│   ├── server.py      # stdio MCP server entry point
│   ├── filesystem.py  # file_read/write/delete
│   └── web_search.py  # Web search tool
├── skills/            # Docker Skill manager
├── devices/           # Device control (host + ADB)
├── models/            # SQLAlchemy models (10 tables)
├── api/               # FastAPI routes (IM, admin, device)
├── tasks/             # Celery tasks (execution, recovery, sync)
├── migrations/        # Alembic migrations
└── deploy/            # ELK stack configs (Filebeat, Logstash, Kibana)
```

## Key Features

- **Multi-Channel IM**: Telegram, WeChat, Web Console with unified message format
- **LLM-Powered Planning**: Task decomposition with native Function Calling
- **Safety Harness**: Deterministic rule engine — blacklists, path boundaries, quotas, cooling-off
- **MCP Protocol**: Standardized tool integration with tool state machine and fallback resolution
- **Fault Recovery**: Idempotency keys, optimistic locking, snapshot-based recovery
- **ELK Observability**: structlog → Filebeat → Logstash → Elasticsearch → Kibana
- **Async Memory Sync**: SQLite → Celery → Vector Store → SQLite writeback

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/v1/health` | Health check |
| POST | `/api/v1/im/web/message` | Web console message (SSE) |
| POST | `/api/v1/im/web/message/confirm` | Confirm/reject operation |
| POST | `/api/v1/admin/mcp-servers` | Register MCP server |
| POST | `/api/v1/admin/skills/install` | Install skill |
| GET | `/api/v1/admin/dashboard` | Dashboard metrics |
| GET | `/api/v1/admin/audit-logs` | Search audit logs |
| WS | `/api/v1/ws/device/{id}` | Device agent WebSocket |

## License

MIT
