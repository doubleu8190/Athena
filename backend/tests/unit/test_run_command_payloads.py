from __future__ import annotations

from datetime import datetime, timezone

import pytest

from domain.runs import (
    CommandType,
    RoundCompletedResumeValue,
    RunCommand,
    RunResumePayload,
    RunStartPayload,
)
from application.contracts.runs import (
    RoundCompletedResumeValueSchema,
    RunResumePayloadSchema,
    RunStartPayloadSchema,
    parse_run_command_payload,
)


def test_start_payload_round_trips_sequence_and_attachment_ids():
    payload = RunStartPayload(
        message="hello", message_id="message-1", attachment_ids=("file-1",), sequence=4
    )
    restored = parse_run_command_payload(
        CommandType.RUN_START,
        RunStartPayloadSchema.from_domain(payload).model_dump(mode="json"),
    )
    assert isinstance(restored, RunStartPayload)
    assert restored.sequence == 4
    assert restored.attachment_ids == ("file-1",)


def test_resume_payload_validates_the_root_resume_identity():
    payload = RunResumePayload(
        resume_value=RoundCompletedResumeValue(
            type="round_completed", run_id="run-1", plan_id="plan-1",
            round_id="round-1", plan_version=1, root_wait_generation=2,
            interrupt_id="interrupt-1",
        )
    )
    restored = parse_run_command_payload(
        CommandType.RUN_RESUME,
        RunResumePayloadSchema.from_domain(payload).model_dump(mode="json"),
    )
    assert isinstance(restored, RunResumePayload)
    assert restored.resume_value.root_wait_generation == 2


def test_internal_invocation_rejects_a_payload_for_another_type():
    with pytest.raises(ValueError, match="run.start requires RunStartPayload"):
        RunCommand(
            command_id="invoke-1", command_type=CommandType.RUN_START,
            session_id="session-1", run_id="run-1",
            payload=RunResumePayload(
                resume_value=RoundCompletedResumeValue(
                    type="round_completed", run_id="run-1", plan_id="plan-1",
                    round_id="round-1", plan_version=1, root_wait_generation=1,
                    interrupt_id="interrupt-1",
                )
            ),
            issued_at=datetime.now(timezone.utc),
        )


def test_invalid_persisted_payload_fails_at_deserialization():
    with pytest.raises(ValueError, match="invalid_payload_for_run.start"):
        parse_run_command_payload(
            CommandType.RUN_START,
            {"message": "hello", "message_id": "m1", "attachment_ids": [1]},
        )
