"""阶段 7 目标 Controller 注入契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.approval import ApprovalService
from application.tools import MCPService, ToolService
from bootstrap import create_app
from domain.tools import JsonSchema, RiskLevel, ToolConfig, ToolExecutionMode, ToolExecutionResult


class ApprovalRepo:
    async def create(self, value): return True
    async def get(self, approval_id): return None
    async def list_pending(self, session_id=None): return []
    async def list_history(self, session_id=None, *, limit=50, offset=0): return []
    async def resolve(self, *args, **kwargs): return True
    async def resolve_batch(self, *args, **kwargs): return True


class ApprovalEvents:
    async def required(self, value): return None


class ToolRepo:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.value = ToolConfig("search", ToolExecutionMode.NATIVE, None, None, "Search", JsonSchema(), RiskLevel.LOW, False, True, now, now)
    async def list_all(self): return [self.value]
    async def get(self, name): return self.value if name == "search" else None
    async def upsert(self, value): self.value = value; return value
    async def update(self, value): self.value = value; return value


class ToolExec:
    async def execute(self, request): return ToolExecutionResult("success")


class MCPPort:
    async def register(self, name, config): return {"name": name, "status": "connected", "tool_count": 0, "error": None}
    async def unregister(self, name): return None
    async def list_servers(self): return []
    async def shutdown(self): return None


def test_target_approval_tools_and_mcp_controllers_are_wired() -> None:
    approval = ApprovalService(ApprovalRepo(), ApprovalEvents(), id_factory=lambda: "a-1")
    tools = ToolService(ToolRepo(), ToolExec())
    mcp = MCPService(MCPPort())
    client = TestClient(create_app(approval_service_factory=lambda: approval, tool_service_factory=lambda: tools, mcp_service_factory=lambda: mcp))

    assert client.get("/api/approvals").json() == []
    assert client.get("/api/tools").json()["items"][0]["name"] == "search"
    assert client.patch("/api/tools/search", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/mcp/servers").json()["total"] == 0
    result = client.post("/api/mcp/servers", json={"local": {"command": "python"}}).json()
    assert result["registered"] == 1
