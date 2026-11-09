"""Run ORM 行、领域实体与 typed JSON 的转换。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from adapter.run.model import RunEventModel, RunInputRequestModel, RunModel, RunToolCallModel
from domain.run.model import (
    InputRequestStatus,
    Run,
    RunApprovalResumedPayload,
    RunCancelledPayload,
    RunCancelRequestedPayload,
    RunCompletedPayload,
    RunCompressionCompletedPayload,
    RunCompressionFailedPayload,
    RunCompressionStartedPayload,
    RunEvent,
    RunEventPayload,
    RunFailedPayload,
    RunInputRequest,
    RunQueuedPayload,
    RunResumedPayload,
    RunStartedPayload,
    RunStatus,
    RunToolApprovalDecidedPayload,
    RunToolCallFinishedPayload,
    RunToolCallStartedPayload,
    RunUsage,
    RunWaitingForApprovalPayload,
    RunWaitingPayload,
)
from domain.tool import (
    RuntimeToolSnapshot,
    RunToolCallRecord,
    ToolApprovalDecision,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
)

_EVENT_PAYLOAD_TYPES: dict[str, type[BaseModel]] = {
    "queued": RunQueuedPayload,
    "started": RunStartedPayload,
    "waiting_for_input": RunWaitingPayload,
    "waiting_for_approval": RunWaitingForApprovalPayload,
    "resumed": RunResumedPayload,
    "approval_resumed": RunApprovalResumedPayload,
    "tool_approval_decided": RunToolApprovalDecidedPayload,
    "cancel_requested": RunCancelRequestedPayload,
    "completed": RunCompletedPayload,
    "failed": RunFailedPayload,
    "cancelled": RunCancelledPayload,
    "compression_started": RunCompressionStartedPayload,
    "compression_completed": RunCompressionCompletedPayload,
    "compression_failed": RunCompressionFailedPayload,
    "tool_call_started": RunToolCallStartedPayload,
    "tool_call_finished": RunToolCallFinishedPayload,
}


def _as_utc(value: datetime) -> datetime:
    """兼容 SQLite 驱动返回无时区时间。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def run_to_domain(row: RunModel) -> Run:
    """将 ORM 行投影为不可变 Run 实体。"""
    usage = RunUsage.model_validate(row.usage)
    tool_snapshot = (
        None if row.tool_snapshot is None else RuntimeToolSnapshot.model_validate(row.tool_snapshot)
    )
    return Run(
        id=row.id,
        session_id=row.session_id,
        owner_id=row.owner_id,
        status=RunStatus(row.status),
        trigger_message_id=row.trigger_message_id,
        context_cutoff_sequence=row.context_cutoff_sequence,
        retry_of_run_id=row.retry_of_run_id,
        model_config_version_id=row.model_config_version_id,
        prompt_template_version=row.prompt_template_version,
        event_sequence=row.event_sequence,
        execution_epoch=row.execution_epoch,
        worker_id=row.worker_id,
        lease_expires_at=row.lease_expires_at,
        execution_started_at=row.execution_started_at,
        execution_deadline_at=row.execution_deadline_at,
        queued_at=row.queued_at,
        first_started_at=row.first_started_at,
        finished_at=row.finished_at,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
        cancel_requested_at=row.cancel_requested_at,
        safe_error_code=row.safe_error_code,
        usage=usage,
        compression_policy_version_id=row.compression_policy_version_id,
        tool_snapshot=tool_snapshot,
        auth_session_id=row.auth_session_id,
        llm_request_count=row.llm_request_count,
    )


def input_request_to_domain(row: RunInputRequestModel) -> RunInputRequest:
    """将 ORM 行投影为输入请求实体。"""
    return RunInputRequest(
        id=row.id,
        run_id=row.run_id,
        session_id=row.session_id,
        question_message_id=row.question_message_id,
        status=InputRequestStatus(row.status),
        answer_message_id=row.answer_message_id,
        created_at=_as_utc(row.created_at),
        answered_at=None if row.answered_at is None else _as_utc(row.answered_at),
        closed_at=None if row.closed_at is None else _as_utc(row.closed_at),
    )


def event_to_domain(row: RunEventModel) -> RunEvent:
    """通过 Pydantic discriminator 校验并恢复事件 JSON。"""
    payload_type = _EVENT_PAYLOAD_TYPES.get(row.type)
    if payload_type is None:
        raise ValueError("run_event_type_invalid")
    payload = cast(RunEventPayload, payload_type.model_validate(row.payload))
    if payload.type != row.type:
        raise ValueError("run_event_payload_type_mismatch")
    return RunEvent(
        id=row.id,
        run_id=row.run_id,
        sequence=row.sequence,
        type=row.type,
        created_at=_as_utc(row.created_at),
        payload=payload,
    )


def event_payload_to_json(payload: RunEventPayload) -> dict[str, Any]:
    """将事件 discriminated union 序列化到数据库 JSON 边界。"""
    return payload.model_dump(mode="json", exclude_none=True)


def tool_call_to_domain(row: RunToolCallModel) -> RunToolCallRecord:
    """Restore one call record with typed outcome JSON."""
    return RunToolCallRecord(
        id=row.id,
        run_id=row.run_id,
        session_id=row.session_id,
        tool_call_id=row.tool_call_id,
        ordinal=row.ordinal,
        tool_id=row.tool_id,
        tool_kind=None if row.tool_kind is None else ToolKind(row.tool_kind),
        model_name=row.model_name,
        execution_epoch=row.execution_epoch,
        status=ToolCallStatus(row.status),
        call_message_id=row.call_message_id,
        result_message_id=row.result_message_id,
        started_at=None if row.started_at is None else _as_utc(row.started_at),
        finished_at=None if row.finished_at is None else _as_utc(row.finished_at),
        safe_error_code=row.safe_error_code,
        outcome=None if row.outcome is None else ToolOutcome.model_validate(row.outcome),
        requires_approval=row.requires_approval,
        requested_at=None if row.requested_at is None else _as_utc(row.requested_at),
        approval_decided_at=(
            None if row.approval_decided_at is None else _as_utc(row.approval_decided_at)
        ),
        approval_decision=(
            None
            if row.approval_decision is None
            else ToolApprovalDecision(row.approval_decision)
        ),
    )


__all__ = [
    "event_payload_to_json",
    "event_to_domain",
    "input_request_to_domain",
    "run_to_domain",
    "tool_call_to_domain",
]
