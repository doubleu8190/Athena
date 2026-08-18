"""Supported attachment type endpoint tests."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core.files.registry import AdapterRegistry


def test_supported_attachment_types_come_from_adapter_registry(monkeypatch):
    from athena.gateway.routes.files import router
    import athena.gateway.routes.files as files_module

    async def mock_session_exists(_session_id: str):
        return object()

    class Runtime:
        adapters = AdapterRegistry()

    monkeypatch.setattr(files_module, "_session_exists", mock_session_exists)
    monkeypatch.setattr(files_module, "get_file_runtime", lambda: Runtime())

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/sessions/session/attachment-types")

    assert response.status_code == 200
    extensions = response.json()["extensions"]
    assert extensions == sorted(extensions)
    assert {".pdf", ".docx", ".xlsx", ".png", ".py", ".txt"} <= set(extensions)
    assert not {".zip", ".tar", ".tgz", ".tar.gz"} & set(extensions)
