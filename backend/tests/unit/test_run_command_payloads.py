from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from domain.runs import (
    CommandType,
    RoundCompletedResumeValue,
    RunCancelPayload,
    RunCommand,
    RunResumePayload,
    RunStartPayload,
    parse_run_command_payload,
)
from infrastructure.persistence.postgres.repositories.run_repository import (
    PostgresRunRepository,
    _run_start_payload_or_none,
)


def test_start_payload_round_trips_json_arrays_as_immutable_ids() -> None:
    payload = RunStartPayload(
        message="hello",
        message_id="message-1",
        attachment_ids=("file-1", "file-2"),
    )

    persisted = payload.model_dump(mode="json", exclude_none=True)
    restored = parse_run_command_payload(CommandType.RUN_START, persisted)

    assert isinstance(restored, RunStartPayload)
    assert restored.attachment_ids == ("file-1", "file-2")
    assert persisted["attachment_ids"] == ["file-1", "file-2"]
    assert parse_run_command_payload(
        CommandType.RUN_START, payload.model_dump_json()
    ) == payload


def test_resume_payload_validates_the_root_resume_identity() -> None:
    payload = RunResumePayload(
        resume_value=RoundCompletedResumeValue(
            type="round_completed",
            run_id="run-1",
            plan_id="plan-1",
            round_id="round-1",
            plan_version=1,
            root_wait_generation=2,
            interrupt_id="interrupt-1",
        ),
    )

    restored = parse_run_command_payload(
        CommandType.RUN_RESUME, payload.model_dump(mode="json")
    )

    assert isinstance(restored, RunResumePayload)
    assert restored.resume_value.root_wait_generation == 2
    assert payload.model_dump(mode="json") == {
        "resume_value": {
            "type": "round_completed",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "round_id": "round-1",
            "plan_version": 1,
            "root_wait_generation": 2,
            "interrupt_id": "interrupt-1",
        }
    }


def test_run_command_rejects_a_payload_for_another_command_type() -> None:
    with pytest.raises(ValueError, match="run.cancel requires RunCancelPayload"):
        RunCommand(
            command_id="command-1",
            command_type=CommandType.RUN_CANCEL,
            session_id="session-1",
            run_id="run-1",
            payload=RunStartPayload(message="hello", message_id="message-1"),
            issued_at=datetime.now(timezone.utc),
        )


def test_cancel_payload_has_an_explicit_default_reason() -> None:
    persisted = RunCancelPayload().model_dump(mode="json")

    assert parse_run_command_payload(CommandType.RUN_CANCEL, persisted) == RunCancelPayload(
        reason="USER_REQUEST"
    )


def test_invalid_persisted_payload_fails_at_deserialization() -> None:
    with pytest.raises(ValueError, match="invalid_payload_for_run.start"):
        parse_run_command_payload(
            CommandType.RUN_START,
            {"message": "hello", "attachment_ids": [1]},
        )


def test_postgres_command_result_uses_validated_payload_fields() -> None:
    row = SimpleNamespace(
        command_id="command-1",
        run_id="run-1",
        command_type=CommandType.RUN_START.value,
        payload_json=(
            '{"message":"hello","message_id":"message-1",'
            '"attachment_ids":["file-1"]}'
        ),
        status="queued",
    )

    result = PostgresRunRepository._result(row, False)

    assert result.message_id == "message-1"
    assert result.attachment_ids == ("file-1",)


def test_empty_run_request_is_absent_but_populated_request_is_typed() -> None:
    assert _run_start_payload_or_none("{}") is None
    request = _run_start_payload_or_none(
        '{"message":"hello","message_id":"message-1","attachment_ids":[]}'
    )

    assert request is not None
    assert request.message == "hello"
    assert request.message_id == "message-1"
