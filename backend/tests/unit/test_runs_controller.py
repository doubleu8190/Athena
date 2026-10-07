"""阶段 5 运行命令 HTTP 契约测试。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from application.runs import RunCommandService
from application.sessions import SessionNotFoundError
from bootstrap import create_app
from domain.runs import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    CommandType,
    RunSummary,
)
from domain.sessions import Session


class Sessions:
    async def get(self, session_id: str):
        if session_id != "session-1":
            return None
        return Session.create(
            session_id=session_id,
            title="Session",
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


class Runs:
    async def get(self, run_id: str):
        if run_id != "run-1":
            return None
        return RunSummary("run-1", "session-1", "running", "thread-1")

    async def get_active_for_session(self, session_id: str):
        return await self.get("run-1") if session_id == "session-1" else None

    async def list_for_session(self, session_id: str):
        return []


class Commands:
    def __init__(self) -> None:
        self.items: dict[str, CommandStatusRecord] = {}

    async def enqueue_command(self, command):
        self.items[command.command_id] = CommandStatusRecord(
            command_id=command.command_id,
            session_id=command.session_id,
            run_id=command.run_id,
            command_type=command.command_type,
            status=CommandStatus.QUEUED,
        )
        return CommandEnqueueResult(
            command_id=command.command_id,
            run_id=command.run_id,
            message_id=command.payload.get("message_id"),
            attachment_ids=tuple(command.payload.get("attachment_ids", ())),
            status=CommandStatus.QUEUED,
            deduplicated=False,
        )

    async def get_command(self, command_id: str):
        return self.items.get(command_id)


def make_client() -> TestClient:
    commands = Commands()
    service = RunCommandService(
        Sessions(),
        commands,
        Runs(),
        id_factory=iter(["message-1", "run-1", "cancel-1"]).__next__,
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return TestClient(create_app(run_command_factory=lambda: service))


def test_run_command_routes_preserve_contract() -> None:
    client = make_client()

    submitted = client.post(
        "/api/sessions/session-1/runs",
        json={"command_id": "command-1", "message": "hello"},
    )
    assert submitted.status_code == 202
    assert submitted.json()["status"] == "queued"
    assert submitted.json()["deduplicated"] is False

    command_id = submitted.json()["command_id"]
    status = client.get(f"/api/commands/{command_id}")
    assert status.status_code == 200
    assert status.json()["command_type"] == "run.start"

    cancelled = client.post("/api/runs/run-1/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json()["run_id"] == "run-1"


def test_run_command_routes_map_missing_resources() -> None:
    client = make_client()
    assert client.post(
        "/api/sessions/missing/runs",
        json={"command_id": "command-1", "message": "hello"},
    ).status_code == 404
    assert client.post("/api/runs/missing/cancel").status_code == 404
    assert client.get("/api/commands/missing").status_code == 404
