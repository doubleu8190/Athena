"""工具管理端点测试 — GET /tools 列表 + PATCH /tools/{name} 启停."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core.tools.builtin.registry import register_builtin_tools
from athena.core.tools.manager import UnifiedToolManager
from athena.infrastructure.sqlite.database import Database
from tests.fakes import install_runtime, make_tool_manager


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
    m = make_tool_manager()
    register_builtin_tools(m)
    return m


@pytest.fixture
def client(db, manager):
    """构造通过 RuntimeContainer 注入依赖的 tools 测试应用."""
    from athena.core.tools.catalog import ToolCatalogService
    from athena.gateway.routes.tools import router

    app = FastAPI()
    app.include_router(router)
    install_runtime(
        app,
        db=db,
        tool_manager=manager,
        tool_catalog=ToolCatalogService(db.tools),
    )

    yield TestClient(app)


def test_list_tools_includes_enabled_and_stats(client):
    resp = client.get("/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    assert data["enabled"] == data["total"]
    assert isinstance(data["high_risk"], int)
    assert isinstance(data["calls_today"], int)
    names = {item["name"] for item in data["items"]}
    assert "read_local_file" in names
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
    resp = client.patch("/tools/read_local_file", json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "status": "updated",
        "name": "read_local_file",
        "enabled": False,
        "risk_level": "low",
        "require_approval": False,
    }

    data = client.get("/tools").json()
    item = next(i for i in data["items"] if i["name"] == "read_local_file")
    assert item["enabled"] is False
    assert data["enabled"] == data["total"] - 1


def test_patch_reenable(client):
    client.patch("/tools/read_local_file", json={"enabled": False})
    resp = client.patch("/tools/read_local_file", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    data = client.get("/tools").json()
    item = next(i for i in data["items"] if i["name"] == "read_local_file")
    assert item["enabled"] is True


def test_patch_unknown_tool_404(client):
    resp = client.patch("/tools/does_not_exist", json={"enabled": False})
    assert resp.status_code == 404


def test_list_tools_requires_runtime_container():
    """缺少应用运行时容器时应立即暴露配置错误."""
    from athena.gateway.routes.tools import router

    app = FastAPI()
    app.include_router(router)
    with pytest.raises(RuntimeError, match="runtime is not initialized"):
        TestClient(app).get("/tools")
