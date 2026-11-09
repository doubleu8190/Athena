"""将 Run 应用结果投影为安全 HTTP DTO。"""

from __future__ import annotations

from api.dto.run import (
    RunAttemptUsageResponse,
    RunEventListResponse,
    RunEventResponse,
    RunInputRequestResponse,
    RunListResponse,
    RunResponse,
    RunToolCallDetailResponse,
    RunToolCallListResponse,
    RunToolCallSummaryResponse,
    RunToolOutcomeResponse,
    RunUsageResponse,
)
from app.run.model.result import RunDetailsResult, RunListResult
from domain.run.model import Run, RunEvent, RunInputRequest, RunStatus, RunUsage
from domain.tool import RunToolCallDetails, RunToolCallPage, RunToolCallRecord

RETRY_WARNING = "整个任务将从头执行，可能重复此前已成功的操作；原操作结果未知时也可能已生效。"


def _usage_to_response(usage: RunUsage) -> RunUsageResponse:
    return RunUsageResponse(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        complete=usage.complete,
        attempts=[
            RunAttemptUsageResponse(
                execution_epoch=item.execution_epoch,
                attempt=item.attempt,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                total_tokens=item.total_tokens,
                complete=item.complete,
                purpose=item.purpose,
                operation_id=item.operation_id,
                batch_index=item.batch_index,
                model_profile_id=item.model_profile_id,
                duration_ms=item.duration_ms,
            )
            for item in usage.attempts
        ],
        answer_tokens=usage.answer_tokens,
        compression_tokens=usage.compression_tokens,
    )


def _input_to_response(request: RunInputRequest) -> RunInputRequestResponse:
    return RunInputRequestResponse(
        id=request.id,
        question_message_id=request.question_message_id,
        status=request.status.value,
        created_at=request.created_at,
    )


def run_to_response(
    run: Run,
    *,
    pending_input: RunInputRequest | None = None,
    replayed: bool = False,
) -> RunResponse:
    """只公开 Run 业务状态，不包含供应商地址或凭据引用。"""
    return RunResponse(
        id=run.id,
        session_id=run.session_id,
        status=run.status.value,
        trigger_message_id=run.trigger_message_id,
        context_cutoff_sequence=run.context_cutoff_sequence,
        retry_of_run_id=run.retry_of_run_id,
        model_config_version_id=run.model_config_version_id,
        prompt_template_version=run.prompt_template_version,
        event_sequence=run.event_sequence,
        execution_epoch=run.execution_epoch,
        queued_at=run.queued_at,
        first_started_at=run.first_started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
        cancel_requested_at=run.cancel_requested_at,
        safe_error_code=run.safe_error_code,
        usage=_usage_to_response(run.usage),
        pending_input=None if pending_input is None else _input_to_response(pending_input),
        replayed=replayed,
        retry_warning=(
            RETRY_WARNING
            if run.retry_of_run_id is not None or run.status is RunStatus.FAILED
            else None
        ),
    )


def run_details_to_response(details: RunDetailsResult) -> RunResponse:
    return run_to_response(details.run, pending_input=details.pending_input)


def run_list_to_response(result: RunListResult) -> RunListResponse:
    return RunListResponse(
        items=[run_to_response(run) for run in result.items],
        next_cursor=result.next_cursor,
    )


def event_to_response(event: RunEvent) -> RunEventResponse:
    return RunEventResponse(
        id=event.id,
        run_id=event.run_id,
        sequence=event.sequence,
        type=event.type,
        created_at=event.created_at,
        payload=event.payload.model_dump(mode="json", exclude={"type"}),
    )


def events_to_response(events: tuple[RunEvent, ...], limit: int) -> RunEventListResponse:
    return RunEventListResponse(
        items=[event_to_response(event) for event in events],
        next_after_sequence=(events[-1].sequence if len(events) == limit and events else None),
    )


def tool_call_summary_to_response(record: RunToolCallRecord) -> RunToolCallSummaryResponse:
    """Project a call without arguments, output, or internal routing data."""
    duration_ms = (
        None
        if record.finished_at is None
        else max(0, int((record.finished_at - record.started_at).total_seconds() * 1000))
    )
    return RunToolCallSummaryResponse(
        id=record.id,
        tool_call_id=record.tool_call_id,
        ordinal=record.ordinal,
        tool_id=record.tool_id,
        tool_kind=None if record.tool_kind is None else record.tool_kind.value,
        model_name=record.model_name,
        status=record.status.value,
        started_at=record.started_at,
        finished_at=record.finished_at,
        duration_ms=duration_ms,
        safe_error_code=record.safe_error_code,
        truncated=bool(record.outcome is not None and record.outcome.truncated),
    )


def tool_calls_to_response(page: RunToolCallPage) -> RunToolCallListResponse:
    return RunToolCallListResponse(
        items=[tool_call_summary_to_response(item) for item in page.items],
        next_after_ordinal=page.next_after_ordinal,
    )


def tool_call_to_response(details: RunToolCallDetails) -> RunToolCallDetailResponse:
    summary = tool_call_summary_to_response(details.record)
    outcome = details.record.outcome
    return RunToolCallDetailResponse(
        **summary.model_dump(),
        arguments=details.arguments,
        outcome=(
            None
            if outcome is None
            else RunToolOutcomeResponse(
                status=outcome.status.value,
                code=outcome.code,
                message=outcome.message,
                data=outcome.data,
                truncated=outcome.truncated,
                cleanup_error_code=outcome.cleanup_error_code,
            )
        ),
    )


__all__ = [
    "event_to_response",
    "events_to_response",
    "run_details_to_response",
    "run_list_to_response",
    "run_to_response",
    "tool_call_summary_to_response",
    "tool_call_to_response",
    "tool_calls_to_response",
]
