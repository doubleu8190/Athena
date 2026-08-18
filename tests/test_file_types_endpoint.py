"""Supported attachment type endpoint tests."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock

from athena.core.files.registry import AdapterRegistry
from tests.fakes import install_runtime


def test_supported_attachment_types_come_from_adapter_registry():
    from athena.gateway.routes.files import router

    class Runtime:
        adapter_registry = AdapterRegistry()

    app = FastAPI()
    app.include_router(router)
    db = MagicMock()
    db.sessions.get = AsyncMock(return_value=object())
    install_runtime(app, db=db, file_runtime=Runtime(), file_worker=MagicMock())
    response = TestClient(app).get("/sessions/session/attachment-types")

    assert response.status_code == 200
    extensions = response.json()["extensions"]
    assert extensions == sorted(extensions)
    assert {".pdf", ".docx", ".xlsx", ".png", ".py", ".txt"} <= set(extensions)
    assert not {".zip", ".tar", ".tgz", ".tar.gz"} & set(extensions)
