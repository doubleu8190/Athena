"""将 Run 应用结果投影为安全 HTTP DTO。"""

from __future__ import annotations

from api.dto.message import ArtifactFileRefResponse
from api.dto.run import (
    PendingSubagentApprovalResponse,
    PendingSubagentInputResponse,
    RunEventListResponse,
    RunEventResponse,
    RunInputInteractionResponse,
    RunInputRequestResponse,
    RunListResponse,
    RunModelSnapshotResponse,
    RunPendingToolApprovalResponse,
    RunResponse,
    RunSkillSnapshotResponse,
    RunToolCallDetailResponse,
    RunToolCallListResponse,
    RunToolCallSummaryResponse,
    RunToolOutcomeResponse,
    RunUsageResponse,
    SafeSkillApprovalProjectionResponse,
    SubagentTaskResponse,
)
from app.run.results import RunListResult
from domain.run.model import (
    PendingSubagentApproval,
    PendingSubagentInput,
    Run,
    RunDetails,
    RunEvent,
    RunInputRequest,
    RunStatus,
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


def safe_skill_projection_to_response(
    projection: SafeSkillApprovalProjection,
) -> SafeSkillApprovalProjectionResponse:
    """只序列化已冻结审批摘要，不查询当前 Skill 或原始参数。"""
    return SafeSkillApprovalProjectionResponse.model_validate(projection)


def run_to_response(
    run: Run,
    *,
    pending_input: RunInputRequest | None = None,
    pending_approvals: tuple[PendingToolApproval, ...] = (),
    subagent_tasks: tuple[SubagentTaskSummary, ...] = (),
    pending_subagent_inputs: tuple[PendingSubagentInput, ...] = (),
    pending_subagent_approvals: tuple[PendingSubagentApproval, ...] = (),
    assistant_message_id: str | None = None,
    replayed: bool = False,
) -> RunResponse:
    """只公开 Run 业务状态，不包含供应商地址或凭据引用。"""
    return RunResponse(
        id=run.id,
        session_id=run.session_id,
        status=run.status,
        trigger_message_id=run.trigger_message_id,
        context_cutoff_sequence=run.context_cutoff_sequence,
        retry_of_run_id=run.retry_of_run_id,
        model_config_version_id=run.model_config_version_id,
        # RunModelSnapshotResponse 仅声明安全元数据字段，自动过滤 base_url 等内部字段。
        model_snapshot=RunModelSnapshotResponse.model_validate(run.model_snapshot),
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
        assistant_message_id=assistant_message_id,
        usage=RunUsageResponse.model_validate(run.usage),
        skill_snapshot=(
            None
            if run.skill_snapshot is None
            else RunSkillSnapshotResponse.model_validate(run.skill_snapshot)
        ),
        input_file_ids=run.input_files.ids,
        knowledge_base_ids=(
            None if run.knowledge_base_ids is None else list(run.knowledge_base_ids)
        ),
        pending_input=(
            None if pending_input is None else RunInputRequestResponse.model_validate(pending_input)
        ),
        pending_approvals=[
            RunPendingToolApprovalResponse.model_validate(item) for item in pending_approvals
        ],
        parent_run_id=run.parent_run_id,
        root_run_id=run.root_run_id,
        parent_tool_call_id=run.parent_tool_call_id,
        depth=run.depth,
        subagent_profile_id=run.subagent_profile_id,
        subagent_slug=run.subagent_slug,
        subagent_name=run.subagent_name,
        subagent_tasks=[SubagentTaskResponse.model_validate(item) for item in subagent_tasks],
        pending_subagent_inputs=[
            PendingSubagentInputResponse.model_validate(item) for item in pending_subagent_inputs
        ],
        pending_subagent_approvals=[
            PendingSubagentApprovalResponse.model_validate(item)
            for item in pending_subagent_approvals
        ],
        input_interactions=[
            RunInputInteractionResponse.model_validate(item) for item in run.input_interactions
        ],
        replayed=replayed,
        retry_warning=(
            RETRY_WARNING
            if run.retry_of_run_id is not None or run.status is RunStatus.FAILED
            else None
        ),
    )


def run_details_to_response(details: RunDetails) -> RunResponse:
    return run_to_response(
        details.run,
        pending_input=details.pending_input,
        pending_approvals=details.pending_approvals,
        subagent_tasks=details.subagent_tasks,
        pending_subagent_inputs=details.pending_subagent_inputs,
        pending_subagent_approvals=details.pending_subagent_approvals,
        assistant_message_id=details.assistant_message_id,
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
        tool_kind=record.tool_kind,
        model_name=record.model_name,
        status=record.status,
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
                status=outcome.status,
                code=outcome.code,
                message=outcome.message,
                data={} if is_skill_call else outcome.data,
                truncated=outcome.truncated,
                cleanup_error_code=outcome.cleanup_error_code,
            )
        ),
        artifacts=[
            ArtifactFileRefResponse.model_validate(artifact) for artifact in details.artifacts
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
