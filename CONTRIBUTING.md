# Contributing to Athena

## Development Setup

1. Fork and clone the repository
2. Create a virtual environment: `python3.14 -m venv .venv && source .venv/bin/activate`
3. Install dev dependencies: `pip install -e ".[dev]"`
4. Copy config templates from `data/` and customize
5. Run tests: `pytest tests/ -v`

## Code Conventions

- Python 3.14+ with type hints
- Async/await for all I/O operations
- structlog for structured logging (JSON format)
- Ruff for linting and formatting
- 100 character line length

## Testing

- Unit tests: `tests/unit/` — fast, no external dependencies
- Integration tests: `tests/integration/` — SQLite, Redis
- Security tests: `tests/security/` — adversarial harness testing

Run all tests: `pytest tests/ --cov=athena`

## Architecture Principles

1. **Safety First**: All tool calls go through Harness Engine
2. **Unified Messages**: Channels normalized to UnifiedMessage; Core never sees channel logic
3. **Tool State Machine**: active/stale/disabled with admin-disables-survive-reconnect
4. **Fallback Chain**: static > dynamic > abort (max depth 1)
5. **Snapshot Authority**: context_snapshot is the authoritative recovery source
