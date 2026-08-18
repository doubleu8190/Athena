"""MCP 服务器端点测试 — 注册 / 列出 / 注销 / 启动恢复.

使用 tests/fixtures/mcp_stub.py（最小 stdio MCP server，暴露 1 个 echo 工具）
以子进程方式驱动真实 MCPManager。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core.tools.builtin.registry import register_builtin_tools
from athena.core.tools.catalog import ToolCatalogService
from athena.core.tools.manager import UnifiedToolManager
from athena.core.tools.mcp.adapter import MCPToolAdapter
from athena.core.tools.mcp.manager import MCPManager
from athena.infrastructure.sqlite.database import Database
from athena.models.mcp import McpServerConfig
from tests.fakes import install_runtime, make_tool_manager

STUB_PATH = Path(__file__).resolve().parent / "fixtures" / "mcp_stub.py"


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
def mcp_manager(db, manager) -> MCPManager:
    adapter = MCPToolAdapter(manager, ToolCatalogService(db.tools))
    return MCPManager(tool_manager=manager, db=db, adapter=adapter)


@pytest.fixture
def client(db, mcp_manager):
    """构造通过 RuntimeContainer 注入依赖的 MCP 测试应用."""
    from athena.gateway.routes.mcp import router

    app = FastAPI()
    app.include_router(router)
    install_runtime(app, db=db, mcp_manager=mcp_manager)

    # 必须用 with 进入：TestClient 只有作为上下文管理器时才共享单个 portal/
    # 事件循环（否则每个请求新建 loop，跨请求的连接状态/后台任务会失效）。
    # 生产环境（uvicorn）为单一事件循环，与 with 语义一致。
    with TestClient(app) as client:
        yield client


def _register_payload(server_name: str, command: str, args: list[str], env: dict | None = None):
    return {"mcpServers": {server_name: {"command": command, "args": args, "env": env or {}}}}


def test_register_list_and_tools(client, manager):
    """POST 注册 stub server → connected；列表可见；工具出现在 tool manager."""
    resp = client.post(
        "/mcp/servers",
        json=_register_payload(
            "@org/stub",
            sys.executable,
            [str(STUB_PATH)],
            {"SERVER_KEY": "abcdefgh"},
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["registered"] == 1
    assert body["failed"] == 0
    result = body["results"][0]
    assert result["status"] == "connected"
    assert result["tool_count"] == 1
    assert result["error"] is None

    # 工具注册进 tool manager，前缀清洗掉 @ /
    names = {t.schema.name for t in manager.list_tools()}
    assert "mcp__org_stub_echo" in names

    # 列表：env 仅掩码
    listing = client.get("/mcp/servers").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["name"] == "@org/stub"
    assert item["status"] == "connected"
    assert item["tool_count"] == 1
    assert item["env_masked"] == {"SERVER_KEY": "••••efgh"}
    assert "created_at" in item


def test_register_multiple_servers(client):
    """一次注册多个服务器，逐台返回状态."""
    payload = {
        "mcpServers": {
            "srv-a": {"command": sys.executable, "args": [str(STUB_PATH)], "env": {}},
            "srv-b": {"command": sys.executable, "args": [str(STUB_PATH)], "env": {}},
        }
    }
    body = client.post("/mcp/servers", json=payload).json()
    assert body["total"] == 2
    assert body["registered"] == 2
    assert {r["name"] for r in body["results"]} == {"srv-a", "srv-b"}


def test_register_failure_persists(client):
    """连接失败的服务器：POST 返回 failed，但配置仍持久化并显示在列表中."""
    resp = client.post(
        "/mcp/servers",
        json=_register_payload("bad-server", "/nonexistent-cmd-xyz", []),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["registered"] == 0
    assert body["failed"] == 1
    assert body["results"][0]["status"] == "failed"
    assert body["results"][0]["error"]

    listing = client.get("/mcp/servers").json()
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["name"] == "bad-server"
    assert item["status"] == "failed"
    assert item["error"]


def test_delete_server(client, manager):
    """注销后列表清空、工具移除."""
    client.post(
        "/mcp/servers",
        json=_register_payload("@org/stub", sys.executable, [str(STUB_PATH)]),
    )
    assert any(t.schema.name == "mcp__org_stub_echo" for t in manager.list_tools())

    resp = client.delete("/mcp/servers/@org/stub")
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "name": "@org/stub"}

    assert client.get("/mcp/servers").json()["total"] == 0
    assert not any(t.schema.name == "mcp__org_stub_echo" for t in manager.list_tools())


def test_delete_unknown_404(client):
    resp = client.delete("/mcp/servers/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_load_persisted_recovers(db, mcp_manager, manager):
    """模拟重启：新 MCPManager.load_persisted() 从 DB 恢复工具.

    注册后配置已持久化到 DB；旧 manager 的实时状态视为随进程消失，
    新 manager 直接 load_persisted() 应重新注册工具。
    """
    result = await mcp_manager.register_server(
        "persist-me",
        McpServerConfig(command=sys.executable, args=[str(STUB_PATH)]),
    )
    assert result["status"] == "connected"
    assert len(await db.mcp_servers.list_all()) == 1

    adapter = MCPToolAdapter(manager, ToolCatalogService(db.tools))
    fresh = MCPManager(tool_manager=manager, db=db, adapter=adapter)
    await fresh.load_persisted()
    names = {t.schema.name for t in manager.list_tools()}
    assert "mcp_persist-me_echo" in names
