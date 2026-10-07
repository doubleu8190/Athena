"""阶段 3 低风险查询 Controller 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.src.application.common.query_services import (
    MemoryQueryService,
    ProviderQueryService,
    RetrievalQueryService,
    SettingsQueryService,
)
from backend.src.application.knowledge import KnowledgeBaseQueryService
from backend.src.application.memory import MemoryService
from backend.src.application.tools import ToolQueryService
from backend.src.bootstrap import create_app
from backend.src.domain.files import KnowledgeBase
from backend.src.domain.memory import MemoryPage, MemoryRecord
from backend.src.domain.tools import (
    JsonSchema,
    RiskLevel,
    ToolConfig,
    ToolExecutionMode,
)


class ToolRepository:
    async def list_all(self):
        return [
            ToolConfig(
                tool_name="search",
                execution_mode=ToolExecutionMode.NATIVE,
                server_name=None,
                remote_name=None,
                description="Search",
                parameters=JsonSchema({"type": "object"}),
                risk_level=RiskLevel.LOW,
                require_approval=False,
                enabled=True,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]

    async def get(self, tool_name):
        return None


class KnowledgeRepository:
    async def list_all(self):
        return [
            KnowledgeBase(
                id="kb-1",
                name="Docs",
                description="Knowledge",
                document_count=2,
                ready_document_count=1,
                total_size_bytes=100,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
        ]

    async def get(self, knowledge_base_id):
        return None


class ProviderPort:
    async def list_providers(self):
        return [{"name": "primary", "provider": "openai", "model": "gpt-4o"}]


class SettingsPort:
    async def get_settings(self):
        return {"host": "127.0.0.1", "port": 8000}


class MemoryPort:
    async def search(self, **kwargs):
        return [{"id": "memory-1", "content": "hello"}]

    async def list(self, **kwargs):
        return {"items": [], "total": 0, "expired": 0}

    async def get(self, memory_id):
        return {"id": memory_id}

    async def revisions(self, memory_id):
        return [{"id": "revision-1"}]


class TargetMemoryPort:
    async def initialize(self):
        return None

    async def search(self, request):
        return [MemoryRecord("target-memory", request.query, score=0.9)]

    async def list(self, request):
        return MemoryPage((MemoryRecord("target-memory", "hello"),), 1, {"total": 1})

    async def get(self, memory_id):
        return MemoryRecord(memory_id, "hello")

    async def revisions(self, memory_id):
        return [MemoryRecord(memory_id, "hello")]

    async def add(self, command):
        return "target-memory"

    async def revise(self, *args, **kwargs):
        return "target-memory-v2"

    async def set_validity(self, *args, **kwargs):
        return True

    async def delete(self, memory_id):
        return None

    async def flush_access_stats(self):
        return 0


class RetrievalPort:
    async def list_runs(self, **kwargs):
        return ([{"run_id": "retrieval-1"}], 1)

    async def get_run(self, run_id):
        return {"run_id": run_id, "candidates": []}


def test_low_risk_queries_use_application_services() -> None:
    client = TestClient(
        create_app(
            tool_query_factory=lambda: ToolQueryService(ToolRepository()),
            provider_query_factory=lambda: ProviderQueryService(ProviderPort()),
            settings_query_factory=lambda: SettingsQueryService(SettingsPort()),
            memory_query_factory=lambda: MemoryQueryService(MemoryPort()),
            retrieval_query_factory=lambda: RetrievalQueryService(RetrievalPort()),
            knowledge_query_factory=lambda: KnowledgeBaseQueryService(KnowledgeRepository()),
        )
    )

    assert client.get("/api/tools").json()["items"][0]["name"] == "search"
    assert client.get("/api/tools").json()["calls_today"] == 0
    assert client.get("/api/providers").json()[0]["model"] == "gpt-4o"
    assert client.get("/api/settings").json()["port"] == 8000
    assert client.get("/api/knowledge-bases").json()[0]["id"] == "kb-1"
    assert client.post("/api/memory/search", json={"query": "hello"}).json()[0]["id"] == "memory-1"
    assert client.get("/api/memory").json()["total"] == 0
    assert client.get("/api/memory/memory-1").json()["id"] == "memory-1"
    assert client.get("/api/memory/memory-1/revisions").json()[0]["id"] == "revision-1"
    assert client.get("/api/retrieval/runs").json()["total"] == 1
    assert client.get("/api/retrieval/runs/retrieval-1").json()["run_id"] == "retrieval-1"


def test_memory_queries_can_use_target_memory_service() -> None:
    client = TestClient(
        create_app(memory_service_factory=lambda: MemoryService(TargetMemoryPort()))
    )

    assert client.post("/api/memory/search", json={"query": "hello"}).json()[0]["id"] == "target-memory"
    assert client.get("/api/memory").json()["total"] == 1
    assert client.get("/api/memory/target-memory").json()["content"] == "hello"
