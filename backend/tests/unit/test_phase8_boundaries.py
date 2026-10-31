"""Boundary matrix for migrated ports and HTTP error mappings."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bootstrap import create_app
from bootstrap.check import check_app
from application.approval import ApprovalService
from application.files import AttachmentNotFoundError, SessionNotFoundError
from application.knowledge import KnowledgeBaseNotFoundError, KnowledgeDocumentNotFoundError
from configuration import LLMProviderConfig, Settings

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_bootstrap_probe_and_unconfigured_query_routes(monkeypatch) -> None:
    settings = Settings(
        llm_providers=[LLMProviderConfig(provider="openai", api_key="test-key")],
        embedding_provider="echo",
        embedding_dimension=2,
    )
    monkeypatch.setattr("configuration.get_settings", lambda: settings)
    check_app()
    from fastapi import FastAPI
    from  interfaces.http.controllers.query_controller import build_query_router
    app = FastAPI()
    app.include_router(build_query_router())
    client = TestClient(app)
    for path, method in [
        ("/tools", "get"), ("/providers", "get"), ("/settings", "get"),
        ("/knowledge-bases", "get"), ("/memory", "get"), ("/retrieval/runs", "get"),
        ("/retrieval/runs/r", "get"), ("/memory/m", "get"), ("/memory/m/revisions", "get"),
    ]:
        assert getattr(client, method)(path).status_code == 503
    assert client.post("/memory/search", json={"query": "q"}).status_code == 503


def test_controller_error_mappings_for_approval_tools_files_and_knowledge() -> None:
    class Approval:
        async def pending(self, session_id=None): raise RuntimeError("x")
        async def history(self, *args, **kwargs): return []
        async def resolve(self, *args, **kwargs): return False
    class Tool:
        async def list_tools(self): return []
        async def update_governance(self, *args, **kwargs): raise ValueError("bad risk")
    class Files:
        async def list_for_session(self, key): raise SessionNotFoundError(key)
        async def supported_extensions(self, key): raise SessionNotFoundError(key)
        async def get(self, *args): raise AttachmentNotFoundError("a")
        async def delete(self, *args): raise AttachmentNotFoundError("a")
    class Knowledge:
        async def list_knowledge_bases(self): return []
        async def supported_extensions(self): return []
        async def update_knowledge_base(self, *args): raise KnowledgeBaseNotFoundError("k")
        async def delete_knowledge_base(self, *args): raise KnowledgeBaseNotFoundError("k")
        async def list_documents(self, *args): raise KnowledgeBaseNotFoundError("k")
        async def list_versions(self, *args): raise KnowledgeDocumentNotFoundError("a")
        async def delete_document(self, *args): raise KnowledgeDocumentNotFoundError("a")
    from  interfaces.http.controllers.files_controller import build_files_router
    client = TestClient(create_app(
        approval_service_factory=lambda: Approval(),
        tool_service_factory=lambda: Tool(),
        attachment_service_factory=lambda: Files(),
        knowledge_service_factory=lambda: Knowledge(),
    ))
    assert client.get("/api/sessions/s/attachments").status_code == 404
    assert client.get("/api/sessions/s/attachment-types").status_code == 404
    assert client.get("/api/sessions/s/attachments/a").status_code == 404
    assert client.delete("/api/sessions/s/attachments/a").status_code == 404
    assert client.patch("/api/tools/x", json={"risk_level": "high"}).status_code == 400
    assert client.patch("/api/tools/x", json={}).status_code == 400
    assert client.patch("/api/knowledge-bases/k", json={"name": "x"}).status_code == 404
    assert client.delete("/api/knowledge-bases/k").status_code == 404
    assert client.get("/api/knowledge-bases/k/documents").status_code == 404
    assert client.get("/api/knowledge-bases/k/documents/a/versions").status_code == 404
    assert client.delete("/api/knowledge-bases/k/documents/a").status_code == 404
