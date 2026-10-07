"""记忆管理端点测试 — GET /memory 列表和统计."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.config.settings import Settings
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.infrastructure.postgre.database import Database
from athena.infrastructure.postgre.repositories.memory_repository import PostgresMemoryRepository
from tests.fakes import install_runtime
from tests.fakes import FakeMemoryGraphStore


class _FakeVectorStore:
    """最小化的存储无关向量替身。"""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def initialize(self) -> None:
        return None

    async def add(self, memory_id, content, metadata) -> None:
        self._store[memory_id] = {"document": content, "metadata": dict(metadata)}

    async def update(self, memory_id, content=None, metadata=None) -> None:
        if memory_id in self._store:
            if content is not None:
                self._store[memory_id]["document"] = content
            self._store[memory_id]["metadata"].update(metadata or {})

    async def delete(self, memory_ids) -> None:
        for i in memory_ids:
            self._store.pop(i, None)

    async def get(self, memory_id):
        out: dict[str, list] = {"ids": [], "documents": [], "metadatas": []}
        for i in [memory_id]:
            if i in self._store:
                out["ids"].append(i)
                out["documents"].append(self._store[i]["document"])
                out["metadatas"].append(self._store[i]["metadata"])
        return out


@pytest.fixture
async def db(postgres_url):
    database = Database(postgres_url)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def manager(db):
    """为端点测试显式注入 PostgreSQL 和向量存储端口."""
    settings = Settings(_env_file=None)
    return LongTermMemoryService(
        settings=settings,
        repository=PostgresMemoryRepository(),
        vector_store=_FakeVectorStore(),
        graph_store=FakeMemoryGraphStore(),
    )


@pytest.fixture
def client(manager):
    from athena.gateway.routes.memory import router

    app = FastAPI()
    app.include_router(router)
    install_runtime(app, memory_service=manager)

    yield TestClient(app)


@pytest.mark.asyncio
async def test_list_memories_and_stats(manager, client):
    await manager.add_memory(
        content="用户偏好暗色主题", metadata={"session_id": "s1"}
    )
    await manager.add_memory(
        content="项目使用 FastAPI", metadata={"session_id": "s2"}
    )

    resp = client.get("/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["expired"] == 0
    assert data["recent_week"] == 2
    assert len(data["items"]) == 2
    # 新→旧
    assert data["items"][0]["content"] == "项目使用 FastAPI"
    assert data["items"][0]["metadata"]["session_id"] == "s2"


@pytest.mark.asyncio
async def test_list_memories_pagination(manager, client):
    for i in range(5):
        await manager.add_memory(content=f"记忆{i}", metadata={"session_id": "s1"})

    resp = client.get("/memory", params={"limit": 2, "offset": 1})
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["content"] == "记忆3"
    assert data["items"][1]["content"] == "记忆2"
    assert data["total"] == 5


def test_memory_mutation_routes_are_not_exposed(client):
    assert client.patch("/memory/whatever", json={"content": "x"}).status_code == 405
    assert client.post("/memory/whatever/pin").status_code == 405
