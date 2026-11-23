"""将 Run 应用结果投影为安全 HTTP DTO。"""

from __future__ import annotations

from api.dto.message import ArtifactFileRefResponse
from api.dto.run import (
    FormattedToolArgumentResponse,
    PendingSubagentApprovalResponse,
    PendingSubagentInputResponse,
    RunAttemptUsageResponse,
    RunEventListResponse,
    RunEventResponse,
    RunInputInteractionResponse,
    RunInputRequestResponse,
    RunListResponse,
    RunModelSnapshotResponse,
    RunPendingToolApprovalResponse,
    RunResponse,
    RunSkillEntryResponse,
    RunSkillSnapshotResponse,
    RunToolCallDetailResponse,
    RunToolCallListResponse,
    RunToolCallSummaryResponse,
    RunToolOutcomeResponse,
    RunUsageResponse,
    SafeSkillApprovalProjectionResponse,
    SafeSkillInputFileResponse,
    SkillParameterSummaryItemResponse,
    SubagentTaskResponse,
)
from app.run.model.result import RunDetailsResult, RunListResult
from domain.run.model import (
    PendingSubagentApproval,
    PendingSubagentInput,
    Run,
    RunEvent,
    RunInputRequest,
    RunStatus,
    RunUsage,
    SubagentTaskSummary,
)
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
    SafeSkillApprovalProjection,
)
from domain.tool import (
    PendingToolApproval,
    RunToolCallDetails,
    RunToolCallPage,
    RunToolCallRecord,
)

RETRY_WARNING = "整个任务将从头执行，可能重复此前已成功的操作；原操作结果未知时也可能已生效。"

_SKILL_TOOL_IDS = frozenset(
    {
        SKILL_EXECUTE_TOOL_ID,
        SKILL_LOAD_TOOL_ID,
        SKILL_READ_RESOURCE_TOOL_ID,
    }
)
_SKILL_MODEL_NAMES = frozenset(
    {
        "skill_execute",
        "skill_load",
        "skill_read_resource",
    }
)


def _is_skill_call(tool_id: str | None, model_name: str) -> bool:
    """工具身份缺失或冲突时，仍按归一化 modelName 识别 Skill 调用。"""
    if tool_id in _SKILL_TOOL_IDS:
        return True
    normalized = model_name.strip().lower()
    return normalized in _SKILL_MODEL_NAMES or any(
        normalized.endswith(f"_{name}") for name in _SKILL_MODEL_NAMES
    )


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


def safe_skill_projection_to_response(
    projection: SafeSkillApprovalProjection,
) -> SafeSkillApprovalProjectionResponse:
    """只序列化已冻结审批摘要，不查询当前 Skill 或原始参数。"""
    return SafeSkillApprovalProjectionResponse(
        call_id=projection.call_id,
        skill_id=projection.skill_id,
        skill_name=projection.skill_name,
        skill_version_id=projection.skill_version_id,
        skill_version_label=projection.skill_version_label,
        script_label=projection.script_label,
        purpose=projection.purpose,
        input_files=[
            SafeSkillInputFileResponse(
                file_id=item.file_id,
                filename=item.filename,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                available=item.available,
            )
            for item in projection.input_files
        ],
        parameter_summary=[
            SkillParameterSummaryItemResponse(
                name=item.name,
                label=item.label,
                value=item.value,
            )
            for item in projection.parameter_summary
        ],
        hidden_parameter_count=projection.hidden_parameter_count,
        can_approve=projection.can_approve,
        unavailable_reason=projection.unavailable_reason,
    )


def run_to_response(
    run: Run,
    *,
    pending_input: RunInputRequest | None = None,
    pending_approvals: tuple[PendingToolApproval, ...] = (),
    subagent_tasks: tuple[SubagentTaskSummary, ...] = (),
    pending_subagent_inputs: tuple[PendingSubagentInput, ...] = (),
    pending_subagent_approvals: tuple[PendingSubagentApproval, ...] = (),
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
        model_snapshot=(
            None
            if run.model_snapshot is None
            else RunModelSnapshotResponse(
                id=run.model_snapshot.id,
                name=run.model_snapshot.name,
                model=run.model_snapshot.model,
                scenario_label=run.model_snapshot.scenario_label,
                supports_vision=run.model_snapshot.supports_vision,
                version=run.model_snapshot.version,
            )
        ),
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
        report_message_id=run.report_message_id,
        usage=_usage_to_response(run.usage),
        skill_snapshot=(
            None
            if run.skill_snapshot is None
            else RunSkillSnapshotResponse(
                version=run.skill_snapshot.version,
                skills=[
                    RunSkillEntryResponse(
                        skill_id=item.skill_id,
                        slug=item.slug,
                        scope=item.scope.value,
                        name=item.name,
                        description=item.description,
                        version_id=item.version_id,
                        version_label=item.version_label,
                        requires_approval=item.requires_approval,
                    )
                    for item in run.skill_snapshot.skills
                ],
                selected_skill_ids=list(run.skill_snapshot.selected_skill_ids),
            )
        ),
        input_file_ids=run.input_files.ids,
        knowledge_base_ids=(
            None if run.knowledge_base_ids is None else list(run.knowledge_base_ids)
        ),
        pending_input=None if pending_input is None else _input_to_response(pending_input),
        pending_approvals=[
            RunPendingToolApprovalResponse(
                call_id=item.call_id,
                batch_message_id=item.batch_message_id,
                ordinal=item.ordinal,
                model_name=item.model_name,
                tool_kind=None if item.tool_kind is None else item.tool_kind.value,
                requested_at=item.requested_at,
                safe_skill_projection=(
                    None
                    if item.safe_skill_projection is None
                    else safe_skill_projection_to_response(item.safe_skill_projection)
                ),
                display_name=item.display_name,
                approval_summary=item.approval_summary,
                formatted_arguments=[
                    FormattedToolArgumentResponse(
                        key=arg.key,
                        label=arg.label,
                        value=arg.value,
                    )
                    for arg in item.formatted_arguments
                ],
            )
            for item in pending_approvals
        ],
        parent_run_id=run.parent_run_id,
        root_run_id=run.root_run_id,
        parent_tool_call_id=run.parent_tool_call_id,
        depth=run.depth,
        subagent_profile_id=run.subagent_profile_id,
        subagent_slug=run.subagent_slug,
        subagent_name=run.subagent_name,
        subagent_tasks=[
            SubagentTaskResponse(
                sub_run_id=item.sub_run_id,
                parent_tool_call_id=item.parent_tool_call_id,
                subagent_profile_id=item.subagent_profile_id,
                subagent_slug=item.subagent_slug,
                subagent_name=item.subagent_name,
                task_index=item.task_index,
                task_summary=item.task_summary,
                status=item.status.value,
                safe_error_code=item.safe_error_code,
                created_at=item.created_at,
                first_started_at=item.first_started_at,
                finished_at=item.finished_at,
            )
            for item in subagent_tasks
        ],
        pending_subagent_inputs=[
            PendingSubagentInputResponse(
                sub_run_id=item.sub_run_id,
                subagent_slug=item.subagent_slug,
                subagent_name=item.subagent_name,
                task_index=item.task_index,
                task_summary=item.task_summary,
                input_request_id=item.input_request_id,
                question_message_id=item.question_message_id,
                question_text=item.question_text,
                created_at=item.created_at,
            )
            for item in pending_subagent_inputs
        ],
        pending_subagent_approvals=[
            PendingSubagentApprovalResponse(
                sub_run_id=item.sub_run_id,
                subagent_slug=item.subagent_slug,
                subagent_name=item.subagent_name,
                task_index=item.task_index,
                task_summary=item.task_summary,
                call_id=item.call_id,
                batch_message_id=item.batch_message_id,
                ordinal=item.ordinal,
                model_name=item.model_name,
                tool_kind=item.tool_kind,
                requested_at=item.requested_at,
                safe_skill_projection=(
                    None
                    if item.safe_skill_projection is None
                    else safe_skill_projection_to_response(item.safe_skill_projection)
                ),
                display_name=item.display_name,
                approval_summary=item.approval_summary,
                formatted_arguments=[
                    FormattedToolArgumentResponse(
                        key=arg.key,
                        label=arg.label,
                        value=arg.value,
                    )
                    for arg in item.formatted_arguments
                ],
            )
            for item in pending_subagent_approvals
        ],
        input_interactions=[
            RunInputInteractionResponse(
                input_request_id=item.input_request_id,
                status=item.status.value,
                question_text=item.question_text,
                answer_text=item.answer_text,
                created_at=item.created_at,
                answered_at=item.answered_at,
            )
            for item in run.input_interactions
        ],
        replayed=replayed,
        retry_warning=(
            RETRY_WARNING
            if run.retry_of_run_id is not None or run.status is RunStatus.FAILED
            else None
        ),
    )


def run_details_to_response(details: RunDetailsResult) -> RunResponse:
    return run_to_response(
        details.run,
        pending_input=details.pending_input,
        pending_approvals=details.pending_approvals,
        subagent_tasks=details.subagent_tasks,
        pending_subagent_inputs=details.pending_subagent_inputs,
        pending_subagent_approvals=details.pending_subagent_approvals,
    )


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
        if record.finished_at is None or record.started_at is None
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
    safe_projection = details.safe_skill_projection
    # Skill 调用即使投影损坏、tool_id 缺失或与 modelName 分叉也必须统一脱敏，
    # 不能因数据库旧记录缺少新字段而回退到裸参数和内部结果数据。
    is_skill_call = _is_skill_call(
        details.record.tool_id,
        details.record.model_name,
    )
    return RunToolCallDetailResponse(
        **summary.model_dump(),
        arguments=None if is_skill_call else details.arguments,
        outcome=(
            None
            if outcome is None
            else RunToolOutcomeResponse(
                status=outcome.status.value,
                code=outcome.code,
                message=outcome.message,
                data={} if is_skill_call else outcome.data,
                truncated=outcome.truncated,
                cleanup_error_code=outcome.cleanup_error_code,
            )
        ),
        artifacts=[
            ArtifactFileRefResponse(
                file_id=artifact.file_id,
                filename=artifact.filename,
                label=artifact.label,
                mime_type=artifact.mime_type,
                size_bytes=artifact.size_bytes,
            )
            for artifact in details.artifacts
        ],
        safe_skill_projection=(
            None if safe_projection is None else safe_skill_projection_to_response(safe_projection)
        ),
    )


__all__ = [
    "event_to_response",
    "events_to_response",
    "run_details_to_response",
    "run_list_to_response",
    "run_to_response",
    "safe_skill_projection_to_response",
    "tool_call_summary_to_response",
    "tool_call_to_response",
    "tool_calls_to_response",
]
