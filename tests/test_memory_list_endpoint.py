"""记忆管理端点测试 — GET /memory 列表/统计 + PATCH /memory/{id} 编辑."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core.memory.memory import MemoryManager
from athena.db.database import Database


class _FakeCollection:
    """最小化的 Chroma collection 假实现 — 记录 add/update，支持 get."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    def add(self, ids=None, documents=None, metadatas=None) -> None:
        metas = list(metadatas or [])
        for i, doc in zip(ids or [], documents or []):
            meta = metas[len(self._store)] if len(self._store) < len(metas) else {}
            self._store[i] = {"document": doc, "metadata": meta}

    def update(self, ids=None, documents=None, metadatas=None) -> None:
        for i, doc in zip(ids or [], documents or []):
            if i in self._store:
                self._store[i]["document"] = doc

    def delete(self, ids=None) -> None:
        for i in ids or []:
            self._store.pop(i, None)

    def get(self, ids=None):
        out: dict[str, list] = {"ids": [], "documents": [], "metadatas": []}
        for i in ids or []:
            if i in self._store:
                out["ids"].append(i)
                out["documents"].append(self._store[i]["document"])
                out["metadatas"].append(self._store[i]["metadata"])
        return out


class _FakeClient:
    def __init__(self) -> None:
        self.collection = _FakeCollection()

    def get_or_create_collection(self, name=None, metadata=None):
        return self.collection


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
    """MemoryManager 指向同一全局 SQLite 引擎；Chroma 用假 collection 规避."""
    return MemoryManager(chroma_client=_FakeClient())


@pytest.fixture
def client(manager):
    from athena.gateway.routes.memory import router
    import athena.gateway.routes.memory as memory_mod

    app = FastAPI()
    app.include_router(router)

    original = memory_mod._get_memory_manager
    memory_mod._get_memory_manager = AsyncMock(return_value=manager)

    yield TestClient(app)

    memory_mod._get_memory_manager = original


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

    fetched = await manager.get(memory_id)
    assert fetched is not None
    assert fetched["content"] == "新内容"


@pytest.mark.asyncio
async def test_patch_memory_not_found(client):
    resp = client.patch("/memory/no_such_id", json={"content": "x"})
    assert resp.status_code == 404


def test_patch_memory_empty_content(client):
    resp = client.patch("/memory/whatever", json={"content": "  "})
    assert resp.status_code == 400
