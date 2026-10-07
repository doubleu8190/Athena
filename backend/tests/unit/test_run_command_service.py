"""阶段 5 首个切片的运行命令 Service 测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.runs import (
    CommandNotFoundError,
    CommandRejectedError,
    RunCommandService,
    RunNotFoundError,
)
from application.sessions import SessionNotFoundError
from domain.runs import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    CommandType,
    RunSummary,
)
from domain.sessions import Session


class Sessions:
    def __init__(self, present: bool = True) -> None:
        self.value = (
            Session.create(
                session_id="session-1",
                title="Session",
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            if present
            else None
        )

    async def get(self, session_id: str):
        return self.value if session_id == "session-1" else None


class Runs:
    def __init__(self) -> None:
        self.value = RunSummary(
            run_id="run-1",
            session_id="session-1",
            status="running",
            root_thread_id="thread-1",
        )

    async def get(self, run_id: str):
        return self.value if run_id == "run-1" else None

    async def get_active_for_session(self, session_id: str):
        return self.value if session_id == "session-1" else None

    async def list_for_session(self, session_id: str):
        return [self.value]


class Commands:
    def __init__(self) -> None:
        self.values: dict[str, CommandStatusRecord] = {}
        self.received = []
        self.reject: str | None = None

    async def enqueue_command(self, command):
        self.received.append(command)
        if self.reject:
            raise ValueError(self.reject)
        current = self.values.get(command.command_id)
        if current is not None:
            return CommandEnqueueResult(
                command_id=current.command_id,
                run_id=current.run_id,
                message_id="message-original",
                attachment_ids=("file-original",),
                status=current.status,
                deduplicated=True,
            )
        self.values[command.command_id] = CommandStatusRecord(
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
        return self.values.get(command_id)


def make_service(*, present: bool = True):
    commands = Commands()
    ids = iter(["message-1", "run-1", "cancel-1", "cancel-2"])
    service = RunCommandService(
        Sessions(present),
        commands,
        Runs(),
        id_factory=lambda: next(ids),
        clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return service, commands


@pytest.mark.asyncio
async def test_submit_is_idempotent_and_preserves_original_payload() -> None:
    service, commands = make_service()

    first = await service.submit(
        "session-1",
        command_id="command-1",
        message="hello",
        attachment_ids=["file-1"],
    )
    second = await service.submit(
        "session-1",
        command_id="command-1",
        message="retry",
        attachment_ids=["file-2"],
    )

    assert first.deduplicated is False
    assert second.deduplicated is True
    assert second.run_id == first.run_id
    assert len(commands.received) == 2


@pytest.mark.asyncio
async def test_submit_maps_busy_rejection() -> None:
    service, commands = make_service()
    commands.reject = "session_busy"

    with pytest.raises(CommandRejectedError) as error:
        await service.submit("session-1", command_id="command-1", message="hello")

    assert error.value.code == "session_busy"


@pytest.mark.asyncio
async def test_cancel_run_and_command_status() -> None:
    service, _ = make_service()

    accepted = await service.cancel_run("run-1")
    status = await service.get_command(accepted.command_id)

    assert accepted.run_id == "run-1"
    assert accepted.status == CommandStatus.QUEUED
    assert status.command_type == CommandType.RUN_CANCEL


@pytest.mark.asyncio
async def test_missing_resources_are_rejected() -> None:
    service, _ = make_service(present=False)
    with pytest.raises(SessionNotFoundError):
        await service.submit("session-1", command_id="command-1", message="hello")

    service, _ = make_service()
    with pytest.raises(RunNotFoundError):
        await service.cancel_run("missing")
    with pytest.raises(CommandNotFoundError):
        await service.get_command("missing")
