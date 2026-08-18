"""会话重命名端点测试 — PATCH /sessions/{session_id}."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.infrastructure.sqlite.database import Database
from athena.utils.ids import generate_session_id
from tests.fakes import install_runtime


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
def client(db):
    from athena.gateway.routes.sessions import router

    app = FastAPI()
    app.include_router(router)
    install_runtime(app, db=db)

    yield TestClient(app)


@pytest.mark.asyncio
async def test_rename_session(db, client):
    created = await db.sessions.create(generate_session_id(), "旧标题")
    resp = client.patch(f"/sessions/{created.id}", json={"title": "新标题"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == created.id
    assert body["title"] == "新标题"

    # 持久化生效
    session = await db.sessions.get(created.id)
    assert session is not None
    assert session.title == "新标题"


def test_rename_not_found(client):
    resp = client.patch("/sessions/nope", json={"title": "x"})
    assert resp.status_code == 404


def test_rename_empty_title(client):
    resp = client.patch("/sessions/whatever", json={"title": "   "})
    assert resp.status_code == 400
