"""记忆管理端点测试 — GET /memory 列表/统计 + PATCH /memory/{id} 编辑."""

from __future__ import annotations

import os
import tempfile
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.config.settings import Settings
from athena.core.memory.long_term_memory import LongTermMemoryService
from athena.infrastructure.postgre.database import Database
from athena.infrastructure.postgre.repositories.memory_repository import PostgresMemoryRepository
from tests.fakes import install_runtime


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
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
async def db(db_path):
    database = Database(db_path)
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
        content="用户偏好暗色主题", metadata={"session_id": "s1"}, pinned=True
    )
    await manager.add_memory(
        content="项目使用 FastAPI", metadata={"session_id": "s2"}
    )

    resp = client.get("/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["pinned"] == 1
    assert data["expired"] == 0
    assert data["recent_week"] == 2
    assert len(data["items"]) == 2
    # 新→旧
    assert data["items"][0]["content"] == "项目使用 FastAPI"
    assert data["items"][0]["pinned"] is False
    assert data["items"][1]["pinned"] is True
    assert data["items"][0]["metadata"]["session_id"] == "s2"


@pytest.mark.asyncio
async def test_list_memories_pinned_filter(manager, client):
    await manager.add_memory(content="A", metadata={"session_id": "s1"})
    await manager.add_memory(content="B", metadata={"session_id": "s1"}, pinned=True)

    resp = client.get("/memory", params={"pinned": "true"})
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["content"] == "B"


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


@pytest.mark.asyncio
async def test_patch_memory_updates_content(manager, client):
    memory_id = await manager.add_memory(
        content="旧内容", metadata={"session_id": "s1"}
    )

    resp = client.patch(f"/memory/{memory_id}", json={"content": "新内容"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    revised_memory_id = resp.json()["memory_id"]
    assert resp.json()["revised_from"] == memory_id

    fetched = await manager.get_memory(revised_memory_id)
    assert fetched is not None
    assert fetched["content"] == "新内容"


@pytest.mark.asyncio
async def test_patch_memory_not_found(client):
    resp = client.patch("/memory/no_such_id", json={"content": "x"})
    assert resp.status_code == 404


def test_patch_memory_empty_content(client):
    resp = client.patch("/memory/whatever", json={"content": "  "})
    assert resp.status_code == 400
