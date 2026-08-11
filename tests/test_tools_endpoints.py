"""工具管理端点测试 — GET /tools 列表 + PATCH /tools/{name} 启停."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core.tools.builtin.registry import register_builtin_tools
from athena.core.tools.manager import UnifiedToolManager
from athena.db.database import Database


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
def manager() -> UnifiedToolManager:
    m = UnifiedToolManager()
    register_builtin_tools(m)
    return m


@pytest.fixture
def client(db, manager):
    """构造只含 tools 路由的测试应用，patch _get_db 与 get_tool_manager."""
    from athena.gateway.routes.tools import router
    import athena.gateway.routes.tools as tools_mod

    async def mock_get_db():
        return db

    app = FastAPI()
    app.include_router(router)

    original_get_db = tools_mod._get_db
    original_get_tool_manager = tools_mod.get_tool_manager
    tools_mod._get_db = mock_get_db
    tools_mod.get_tool_manager = lambda: manager

    yield TestClient(app)

    tools_mod._get_db = original_get_db
    tools_mod.get_tool_manager = original_get_tool_manager


def test_list_tools_includes_enabled_and_stats(client):
    resp = client.get("/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    assert data["enabled"] == data["total"]
    assert isinstance(data["high_risk"], int)
    assert isinstance(data["calls_today"], int)
    names = {item["name"] for item in data["items"]}
    assert "read_file" in names
    assert all(item["enabled"] is True for item in data["items"])
    # 每条含治理字段
    first = data["items"][0]
    for field in (
        "name",
        "description",
        "risk_level",
        "execution_mode",
        "require_approval",
        "enabled",
        "parameters",
        "last_called_at",
    ):
        assert field in first


def test_patch_toggle_disables(client):
    resp = client.patch("/tools/read_file", json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "updated", "name": "read_file", "enabled": False}

    data = client.get("/tools").json()
    item = next(i for i in data["items"] if i["name"] == "read_file")
    assert item["enabled"] is False
    assert data["enabled"] == data["total"] - 1


def test_patch_reenable(client):
    client.patch("/tools/read_file", json={"enabled": False})
    resp = client.patch("/tools/read_file", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    data = client.get("/tools").json()
    item = next(i for i in data["items"] if i["name"] == "read_file")
    assert item["enabled"] is True


def test_patch_unknown_tool_404(client):
    resp = client.patch("/tools/does_not_exist", json={"enabled": False})
    assert resp.status_code == 404


def test_list_tools_manager_none_503(db):
    """未初始化工具管理器时返回 503."""
    from athena.gateway.routes.tools import router
    import athena.gateway.routes.tools as tools_mod

    async def mock_get_db():
        return db

    app = FastAPI()
    app.include_router(router)

    original_get_db = tools_mod._get_db
    original_get_tool_manager = tools_mod.get_tool_manager
    tools_mod._get_db = mock_get_db
    tools_mod.get_tool_manager = lambda: None

    try:
        resp = TestClient(app).get("/tools")
        assert resp.status_code == 503
    finally:
        tools_mod._get_db = original_get_db
        tools_mod.get_tool_manager = original_get_tool_manager
