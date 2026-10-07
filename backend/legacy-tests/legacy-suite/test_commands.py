"""命令路由测试。"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.gateway.routes.commands import run_router
from athena.contracts.commands import CommandType
from athena.contracts.statuses import AgentCommandStatus
from tests.fakes import install_runtime


@dataclass
class FakeRun:
    session_id: str
    status: str = "running"


def make_client(run: FakeRun | None) -> tuple[TestClient, AsyncMock]:
    app = FastAPI()
    app.include_router(run_router)
    run_store = AsyncMock()
    run_store.get_run.return_value = run
    command_store = AsyncMock()
    command_store.enqueue_command.return_value = True
    install_runtime(app, run_store=run_store, command_store=command_store)
    return TestClient(app), command_store


def test_cancel_run_enqueues_cancel_command() -> None:
    client, command_store = make_client(FakeRun(session_id="session-1"))

    response = client.post("/runs/run-1/cancel")

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] == "run-1"
    assert body["status"] == AgentCommandStatus.QUEUED
    assert body["command_id"].startswith("cmd_")
    command_store.enqueue_command.assert_awaited_once()
    command = command_store.enqueue_command.await_args.args[0]
    assert command.command_type == CommandType.RUN_CANCEL
    assert command.command_id == body["command_id"]
    assert command.session_id == "session-1"
    assert command.run_id == "run-1"


def test_cancel_run_returns_not_found_without_enqueue() -> None:
    client, command_store = make_client(None)

    response = client.post("/runs/missing-run/cancel")

    assert response.status_code == 404
    command_store.enqueue_command.assert_not_awaited()
