"""Run 仓储共享的审批投影组装与未完成工具调用收敛辅助函数。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.file.model import SessionFileModel
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.message.converter import execution_to_domain
from adapter.message.model import ExecutionMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunModel, RunToolCallModel
from adapter.run.repository._tx_helpers import append_event
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel
from common.ids import new_id
from domain.message import (
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import RunToolCallFinishedPayload
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
    SafeSkillApprovalProjection,
    SkillExecutionStatus,
)
from domain.tool import (
    FormattedToolArgument,
    PendingToolApproval,
    RuntimeToolSnapshot,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    format_tool_arguments,
    persisted_status_for_outcome,
    render_approval_summary,
)

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


def is_skill_tool_call(
    tool_id: str | None,
    model_name: str,
    tool_kind: str | None = None,
) -> bool:
    """按工具 ID、工具类别与归一化 model_name 识别 Skill 调用，确保审批展示绝不读取原始脚本参数。"""
    if tool_id in _SKILL_TOOL_IDS:
        return True
    # 显式标记为 MCP 工具时跳过名称后缀推断，避免与远端 MCP 工具命名发生语义混淆。
    if tool_kind == ToolKind.MCP.value:
        return False
    normalized = model_name.strip().lower()
    return normalized in _SKILL_MODEL_NAMES or any(
        normalized.endswith(f"_{name}") for name in _SKILL_MODEL_NAMES
    )


async def enrich_pending_approvals_in_session(
    session: AsyncSession,
    entries: Sequence[tuple[RunToolCallModel, RunModel, SafeSkillApprovalProjection | None]],
) -> list[tuple[str | None, str | None, tuple[FormattedToolArgument, ...]]]:
    """批量组装待审批调用的业务展示名、安全摘要与可读键值参数表，避免 N+1 查询。

    安全边界：
    - Skill 脚本调用严禁读取原始 ExecutionMessageModel 参数，仅从已脱敏的
      safe_skill_projection.parameter_summary 映射，防止隐藏脚本参数泄漏；
    - MCP 调用优先读取实时未软删 McpToolModel 展示元信息，若工具或 Server 已变动
      则优雅回退至 Run 创建时冻结的 tool_snapshot 元信息。
    """
    if not entries:
        return []

    non_skill_indices: set[int] = set()
    call_message_ids: set[str] = set()
    mcp_tool_ids: set[str] = set()
    for idx, (call_row, _run_row, skill_proj) in enumerate(entries):
        if skill_proj is not None or is_skill_tool_call(
            call_row.tool_id, call_row.model_name, call_row.tool_kind
        ):
            continue
        non_skill_indices.add(idx)
        call_message_ids.add(call_row.call_message_id)
        if call_row.tool_id:
            mcp_tool_ids.add(call_row.tool_id)

    # 1. 一次性批量加载非 Skill 待审批调用对应的执行消息参数。
    arguments_by_call: dict[tuple[str, str], dict[str, Any]] = {}
    if call_message_ids:
        exec_rows = (
            await session.scalars(
                select(ExecutionMessageModel).where(
                    ExecutionMessageModel.id.in_(tuple(call_message_ids))
                )
            )
        ).all()
        for exec_row in exec_rows:
            domain_msg = execution_to_domain(exec_row)
            for part in domain_msg.parts:
                if isinstance(part, ToolCallPart):
                    arguments_by_call[(exec_row.id, part.tool_call_id)] = dict(part.arguments)

    # 2. 一次性批量加载未软删 Server 下的实时 MCP 工具审批展示元信息。
    live_tools_by_id: dict[str, McpToolModel] = {}
    if mcp_tool_ids:
        live_rows = (
            await session.scalars(
                select(McpToolModel)
                .join(McpServerModel, McpServerModel.id == McpToolModel.server_id)
                .where(
                    McpToolModel.id.in_(tuple(mcp_tool_ids)),
                    McpServerModel.deleted_at.is_(None),
                )
            )
        ).all()
        live_tools_by_id = {row.id: row for row in live_rows}

    # 3. 缓存每个 Run 的冻结 tool_snapshot，作为 MCP 工具或 Server 变更后的优雅回退源。
    snapshots_by_run_id: dict[str, RuntimeToolSnapshot | None] = {}
    results: list[tuple[str | None, str | None, tuple[FormattedToolArgument, ...]]] = []
    for idx, (call_row, run_row, skill_proj) in enumerate(entries):
        if idx not in non_skill_indices:
            # Skill 工具严格隔离原始参数，仅从冻结安全投影输出可见摘要项。
            if skill_proj is not None:
                skill_display = skill_proj.script_label or call_row.model_name
                skill_args = tuple(
                    FormattedToolArgument(
                        key=item.name,
                        label=item.label,
                        value=item.value,
                    )
                    for item in skill_proj.parameter_summary
                )
                results.append((skill_display, None, skill_args))
            else:
                results.append((call_row.model_name, None, ()))
            continue

        arguments = arguments_by_call.get((call_row.call_message_id, call_row.tool_call_id), {})
        meta_display_name: str | None = None
        meta_template: str | None = None
        meta_labels: dict[str, str] = {}

        live_tool = live_tools_by_id.get(call_row.tool_id) if call_row.tool_id is not None else None
        if live_tool is not None:
            meta_display_name = live_tool.display_name
            meta_template = live_tool.approval_summary_template
            meta_labels = dict(live_tool.parameter_labels_json or {})
        else:
            if run_row.id not in snapshots_by_run_id:
                snapshots_by_run_id[run_row.id] = (
                    None
                    if run_row.tool_snapshot is None
                    else RuntimeToolSnapshot.model_validate(run_row.tool_snapshot)
                )
            snapshot = snapshots_by_run_id[run_row.id]
            if snapshot is not None:
                snap_tool = next(
                    (
                        item
                        for item in snapshot.tools
                        if (call_row.tool_id is not None and item.tool_id == call_row.tool_id)
                        or item.model_name == call_row.model_name
                    ),
                    None,
                )
                if snap_tool is not None:
                    meta_display_name = snap_tool.display_name
                    meta_template = snap_tool.approval_summary_template
                    meta_labels = dict(snap_tool.parameter_labels)

        display_name = meta_display_name or call_row.model_name
        approval_summary = render_approval_summary(meta_template, arguments)
        formatted_arguments = format_tool_arguments(arguments, meta_labels)
        results.append((display_name, approval_summary, formatted_arguments))

    return results


async def refresh_safe_skill_projection(
    session: AsyncSession,
    row: RunToolCallModel,
) -> SafeSkillApprovalProjection | None:
    """按冻结摘要复核 Skill 审批输入文件存在性，不读取当前 Skill 管理配置。"""
    if row.safe_skill_projection is None:
        return None
    projection = SafeSkillApprovalProjection.model_validate(row.safe_skill_projection)
    if not projection.input_files:
        return projection
    file_ids = tuple(item.file_id for item in projection.input_files)
    visible_ids = set(
        (
            await session.scalars(
                select(SessionFileModel.id).where(
                    SessionFileModel.session_id == row.session_id,
                    SessionFileModel.id.in_(file_ids),
                )
            )
        ).all()
    )
    files = tuple(
        item.model_copy(update={"available": item.file_id in visible_ids})
        for item in projection.input_files
    )
    all_available = all(item.available for item in files)
    return projection.model_copy(
        update={
            "input_files": files,
            "can_approve": projection.can_approve and all_available,
            "unavailable_reason": (
                projection.unavailable_reason
                if all_available
                else "skill_approval_input_unavailable"
            ),
        }
    )


async def load_pending_approvals_in_session(
    session: AsyncSession,
    run_row: RunModel,
    session_id: str,
) -> tuple[PendingToolApproval, ...]:
    """在当前只读会话内加载主/子 Run 的待人工审批工具调用及安全展示摘要。"""
    rows = (
        await session.scalars(
            select(RunToolCallModel)
            .where(
                RunToolCallModel.run_id == run_row.id,
                RunToolCallModel.session_id == session_id,
                RunToolCallModel.status == ToolCallStatus.PENDING_APPROVAL.value,
            )
            .order_by(RunToolCallModel.ordinal)
        )
    ).all()
    if not rows:
        return ()
    skill_projections: list[SafeSkillApprovalProjection | None] = []
    for row in rows:
        if row.requested_at is None:
            raise RuntimeError("run_tool_call_requested_at_missing")
        skill_projections.append(await refresh_safe_skill_projection(session, row))
    enriched = await enrich_pending_approvals_in_session(
        session,
        [
            (row, run_row, skill_proj)
            for row, skill_proj in zip(rows, skill_projections, strict=True)
        ],
    )
    pending: list[PendingToolApproval] = []
    for row, skill_proj, (display_name, approval_summary, formatted_arguments) in zip(
        rows, skill_projections, enriched, strict=True
    ):
        assert row.requested_at is not None
        pending.append(
            PendingToolApproval(
                call_id=row.id,
                batch_message_id=row.call_message_id,
                ordinal=row.ordinal,
                model_name=row.model_name,
                tool_kind=(None if row.tool_kind is None else ToolKind(row.tool_kind)),
                requested_at=row.requested_at,
                safe_skill_projection=skill_proj,
                display_name=display_name,
                approval_summary=approval_summary,
                formatted_arguments=formatted_arguments,
            )
        )
    return tuple(pending)


async def has_unsettled_tool_calls(session: AsyncSession, run_id: str) -> bool:
    """检查 Run 是否仍存在未进入终态的工具调用记录。"""
    return bool(
        await session.scalar(
            select(RunToolCallModel.id)
            .where(
                RunToolCallModel.run_id == run_id,
                RunToolCallModel.status.in_(
                    (
                        ToolCallStatus.PENDING_APPROVAL.value,
                        ToolCallStatus.APPROVED.value,
                        ToolCallStatus.EXECUTING.value,
                    )
                ),
            )
            .limit(1)
        )
    )


async def settle_running_skill_execution(
    session: AsyncSession,
    tool_call_record_id: str,
    settled_at: datetime,
) -> None:
    """按工具调用身份收敛遗留 running 审计，不使用回查失败时的新 ID。"""
    skill_execution = await session.scalar(
        select(SkillExecutionModel)
        .where(
            SkillExecutionModel.tool_call_record_id == tool_call_record_id,
            SkillExecutionModel.status == SkillExecutionStatus.RUNNING.value,
        )
        .with_for_update()
    )
    if skill_execution is not None:
        skill_execution.status = SkillExecutionStatus.UNKNOWN.value
        skill_execution.error_code = "tool_outcome_unknown"
        skill_execution.finished_at = settled_at


async def settle_unfinished_tool_calls(
    session: AsyncSession,
    message_repo: SqlAlchemyMessageRepository,
    locked_session: SessionModel,
    run: RunModel,
    settled_at: datetime,
) -> None:
    """Cancel undispatched and mark already-started calls unknown."""
    # 由调用方持有事务与 Session→Run 锁；这里按调用序号加行锁收敛全部未完成调用，不提交事务。
    records = list(
        (
            await session.scalars(
                select(RunToolCallModel)
                .where(
                    RunToolCallModel.run_id == run.id,
                    RunToolCallModel.status.in_(
                        (
                            ToolCallStatus.PENDING_APPROVAL.value,
                            ToolCallStatus.APPROVED.value,
                            ToolCallStatus.EXECUTING.value,
                        )
                    ),
                )
                .order_by(RunToolCallModel.ordinal)
                .with_for_update()
            )
        ).all()
    )
    for record in records:
        # 幂等防线：等待行锁期间调用可能已被并发流程收敛为终态，直接跳过。
        if record.status not in {
            ToolCallStatus.PENDING_APPROVAL.value,
            ToolCallStatus.APPROVED.value,
            ToolCallStatus.EXECUTING.value,
        }:
            continue
        # 结果消息需沿用调用消息的 thread 并保持调用/结果协议配对；
        # 找不到调用消息说明持久化数据不一致，不能静默继续。
        call_message = await session.get(ExecutionMessageModel, record.call_message_id)
        if call_message is None:
            raise RuntimeError("run_tool_call_message_missing")
        if record.status == ToolCallStatus.EXECUTING.value:
            # 已进入 EXECUTING 的调用可能已产生外部副作用，但结果无法确认：
            # 优先使用已持久化的 outcome，否则标记 UNKNOWN，绝不重新派发。
            outcome = (
                ToolOutcome.model_validate(record.outcome)
                if record.outcome is not None
                else ToolOutcome(
                    status=ToolOutcomeStatus.UNKNOWN,
                    code="tool_outcome_unknown",
                    message="工具执行结果无法确认。",
                )
            )
            persisted_status = persisted_status_for_outcome(outcome)
        else:
            # 仍待审批或已批准但未派发的调用没有外部副作用，可安全标记为未执行。
            outcome = ToolOutcome(
                status=ToolOutcomeStatus.CANCELLED,
                code="tool_not_executed",
                message="该工具调用未执行。",
            )
            persisted_status = ToolCallStatus.CANCELLED
        if record.result_message_id is None:
            # 为没有结果的调用补写 TOOL 结果消息，保持每条调用都有配对的协议结果。
            result_message_id = new_id("exec")
            result_draft = ExecutionMessageDraft(
                id=result_message_id,
                session_id=run.session_id,
                owner_id=run.owner_id,
                run_id=run.id,
                thread_id=call_message.thread_id,
                role=ExecutionRole.TOOL,
                parts=(
                    ToolResultPart(
                        tool_call_id=record.tool_call_id,
                        status=ToolResultStatus.ERROR,
                        output=outcome.canonical_output(),
                    ),
                ),
                tool_call_id=record.tool_call_id,
                source_message_id=None,
                created_at=settled_at,
                metadata=MessageMetadata(),
            )
            await message_repo.append_execution_in_session(
                session,
                result_draft,
                locked_session=locked_session,
            )
            record.result_message_id = result_message_id
            record.outcome = outcome.model_dump(mode="json")
        # 落库终态与安全错误码；成功结果不写 safe_error_code。
        record.status = persisted_status.value
        record.finished_at = settled_at
        record.safe_error_code = (
            outcome.code if outcome.status is not ToolOutcomeStatus.OK else None
        )
        if record.status == ToolCallStatus.UNKNOWN.value:
            # 脚本附加记录不能比工具调用更早变为可见终态；二者正常完成时
            # 同事务提交，这里只收敛此前事务失败或进程中断遗留的 running。
            await settle_running_skill_execution(
                session,
                record.id,
                settled_at,
            )
        # 每个调用都追加终态事件，保证 SSE 与读取模型看到一致的收敛结果。
        await append_event(
            session,
            run,
            RunToolCallFinishedPayload(
                call_id=record.id,
                ordinal=record.ordinal,
                status=persisted_status,
                round=record.round,
                safe_error_code=record.safe_error_code,
            ),
            settled_at,
        )
