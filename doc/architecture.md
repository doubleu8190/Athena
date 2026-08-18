# Athena architecture

Athena is a modular monolith with one dependency direction:

```text
desktop -> gateway -> core ports <- infrastructure adapters
                         ^
                         |
                     domain models
```

## Tool boundary

All agent-visible declarations are `ToolSpec` values under
`athena/core/tools`. Providers adapt file and agent services without moving
their business logic into the tool layer. `ToolRegistry` installs specs and
`ToolCatalogService` is the only owner of persisted governance reconciliation.
Trusted invocation identifiers are available through `ToolContext`; they are
not model arguments.

## Storage boundary

`athena/core/memory` contains lifecycle, retrieval orchestration and the
dual-write compensation policy. It depends on `MemoryRepository` and
`MemoryVectorStore` ports. Implementations live at the same level:

```text
athena/infrastructure/
├── sqlite/       # SQLAlchemy, SQLite and FTS5 repositories
└── chroma/       # ChromaDB vector adapter
```

Database implementations are imported from `athena.infrastructure.sqlite`.
Core services receive storage dependencies explicitly from the composition
root; no legacy database import facade is maintained.

## Runtime composition

`athena/main.py` is the composition root. It creates a `RuntimeContainer` and
attaches it to `FastAPI.app.state`. HTTP and WebSocket production paths read
dependencies from the request/application rather than module-level service
locators. No module-level runtime service locators are maintained.
