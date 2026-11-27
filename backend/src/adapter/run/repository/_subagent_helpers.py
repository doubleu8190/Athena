"""Run 仓储共享的子智能体状态汇总、产物读取与父 Run 唤醒辅助函数。"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adapter.message.converter import conversation_to_domain, execution_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunInputRequestModel, RunModel, RunToolCallModel
from adapter.run.repository._approval_helpers import enrich_pending_approvals_in_session
from adapter.run.repository._tx_helpers import append_event, clear_lease
from adapter.session.model import SessionModel
from common.ids import new_id
from domain.message import (
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    TextPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    InputRequestStatus,
    PendingSubagentApproval,
    PendingSubagentInput,
    RunStatus,
    RunSubagentsResumedPayload,
    RunSubagentStatusChangedPayload,
    SubagentTaskSummary,
)
from domain.skill import SafeSkillApprovalProjection
from domain.tool import ToolCallStatus, ToolKind, ToolOutcome, ToolOutcomeStatus


async def load_subagent_final_output(session: AsyncSession, sub_run_id: str) -> str:
    """读取子 Run 最终助手执行消息中的文本输出。"""
    row = await session.scalar(
        select(ExecutionMessageModel)
        .where(
            ExecutionMessageModel.run_id == sub_run_id,
            ExecutionMessageModel.role == ExecutionRole.ASSISTANT.value,
        )
        .order_by(ExecutionMessageModel.sequence.desc())
        .limit(1)
    )
    if row is None:
        return ""
    msg = execution_to_domain(row)
    return "".join(part.text for part in msg.parts if isinstance(part, TextPart))


async def load_subagent_artifacts(
    session: AsyncSession, sub_run_id: str
) -> list[dict[str, object]]:
    """汇总子 Run 工具调用产生的交付文件元数据。"""
    rows = (
        await session.scalars(
            select(ExecutionMessageModel)
            .where(
                ExecutionMessageModel.run_id == sub_run_id,
                ExecutionMessageModel.role == ExecutionRole.TOOL.value,
            )
            .order_by(ExecutionMessageModel.sequence)
        )
    ).all()
    artifacts: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for row in rows:
        msg = execution_to_domain(row)
        for part in msg.parts:
            if isinstance(part, ToolResultPart):
                for artifact in part.artifacts:
                    if artifact.file_id in seen_ids:
                        continue
                    seen_ids.add(artifact.file_id)
                    artifacts.append(
                        {
                            "file_id": artifact.file_id,
                            "filename": artifact.filename,
                            "label": artifact.label,
                            "mime_type": artifact.mime_type,
                            "size_bytes": artifact.size_bytes,
                        }
                    )
    return artifacts


async def try_wake_parent_run(
    session: AsyncSession,
    message_repo: SqlAlchemyMessageRepository,
    locked_session: SessionModel,
    sub_run: RunModel,
    settled_at: datetime,
) -> RunModel | None:
    """子 Run 结算终态后锁定父 Run 行检查收敛条件，全部终态时原子唤醒父 Run。"""
    if sub_run.parent_run_id is None or sub_run.parent_tool_call_id is None:
        return None
    # 在 Session 锁保护下对父 Run 行执行 SELECT ... FOR UPDATE，防止并发子 Run 重复唤醒。
    parent = await session.scalar(
        select(RunModel)
        .where(
            RunModel.id == sub_run.parent_run_id,
            RunModel.session_id == sub_run.session_id,
        )
        .with_for_update()
    )
    if parent is None:
        return None
    await append_event(
        session,
        parent,
        RunSubagentStatusChangedPayload(
            sub_run_id=sub_run.id,
            subagent_slug=sub_run.subagent_slug or "",
            subagent_name=sub_run.subagent_name or "",
            status=RunStatus(sub_run.status),
            safe_error_code=sub_run.safe_error_code,
        ),
        settled_at,
    )
    if parent.status != RunStatus.WAITING_FOR_SUBAGENTS.value:
        return parent
    remaining = await session.scalar(
        select(func.count(RunModel.id)).where(
            RunModel.parent_run_id == parent.id,
            RunModel.parent_tool_call_id == sub_run.parent_tool_call_id,
            RunModel.status.notin_(
                (
                    RunStatus.COMPLETED.value,
                    RunStatus.FAILED.value,
                    RunStatus.CANCELLED.value,
                )
            ),
        )
    )
    if remaining and remaining > 0:
        return parent

    # 同批全部子 Run 均已进入终态，组装结构化汇总 JSON 并写入主 Run 的 ToolResultPart。
    batch_runs = (
        await session.scalars(
            select(RunModel)
            .where(
                RunModel.parent_run_id == parent.id,
                RunModel.parent_tool_call_id == sub_run.parent_tool_call_id,
            )
            .order_by(RunModel.created_at, RunModel.id)
        )
    ).all()
    results_payload: list[dict[str, object]] = []
    completed_count = 0
    failed_count = 0
    for item in batch_runs:
        if item.status == RunStatus.COMPLETED.value:
            completed_count += 1
            output_text = await load_subagent_final_output(session, item.id)
            artifacts = await load_subagent_artifacts(session, item.id)
            results_payload.append(
                {
                    "sub_run_id": item.id,
                    "subagent_slug": item.subagent_slug,
                    "subagent_name": item.subagent_name,
                    "status": "completed",
                    "output": output_text,
                    "artifacts": artifacts,
                }
            )
        else:
            failed_count += 1
            fallback_code = (
                "cancelled" if item.status == RunStatus.CANCELLED.value else "execution_failed"
            )
            results_payload.append(
                {
                    "sub_run_id": item.id,
                    "subagent_slug": item.subagent_slug,
                    "subagent_name": item.subagent_name,
                    "status": item.status,
                    "safe_error_code": item.safe_error_code or fallback_code,
                }
            )

    summary_json: dict[str, object] = {
        "results": results_payload,
        "completed_count": completed_count,
        "failed_count": failed_count,
    }
    tool_call_row = await session.scalar(
        select(RunToolCallModel)
        .where(
            RunToolCallModel.run_id == parent.id,
            RunToolCallModel.tool_call_id == sub_run.parent_tool_call_id,
        )
        .with_for_update()
    )
    thread_id = parent.id
    if tool_call_row is not None:
        call_message = await session.get(ExecutionMessageModel, tool_call_row.call_message_id)
        if call_message is not None:
            thread_id = call_message.thread_id
    result_message_id = new_id("exec")
    result_draft = ExecutionMessageDraft(
        id=result_message_id,
        session_id=parent.session_id,
        owner_id=parent.owner_id,
        run_id=parent.id,
        thread_id=thread_id,
        role=ExecutionRole.TOOL,
        parts=(
            ToolResultPart(
                tool_call_id=sub_run.parent_tool_call_id,
                status=ToolResultStatus.SUCCESS,
                output=json.dumps(summary_json, ensure_ascii=False, separators=(",", ":")),
            ),
        ),
        tool_call_id=sub_run.parent_tool_call_id,
        source_message_id=None,
        created_at=settled_at,
        metadata=MessageMetadata(),
    )
    await message_repo.append_execution_in_session(
        session,
        result_draft,
        locked_session=locked_session,
    )
    if tool_call_row is not None and tool_call_row.status == ToolCallStatus.EXECUTING.value:
        outcome = ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="subagents_completed",
            message=f"子智能体批次已完成（成功 {completed_count}，失败 {failed_count}）。",
            data=summary_json,
        )
        tool_call_row.result_message_id = result_message_id
        tool_call_row.status = ToolCallStatus.COMPLETED.value
        tool_call_row.finished_at = settled_at
        tool_call_row.outcome = outcome.model_dump(mode="json")

    parent.status = RunStatus.QUEUED.value
    parent.queued_at = settled_at
    parent.updated_at = settled_at
    clear_lease(parent)
    await append_event(
        session,
        parent,
        RunSubagentsResumedPayload(
            tool_call_id=sub_run.parent_tool_call_id,
            completed_count=completed_count,
            failed_count=failed_count,
        ),
        settled_at,
    )
    return parent


def extract_subagent_task_summary(raw_text: str) -> str:
    """从子 Run 初始任务文本提取精简摘要，去除模板前缀并截断至 120 字符内。"""
    text = raw_text.strip()
    if not text:
        return ""
    if text.startswith("任务：\n"):
        text = text[len("任务：\n") :]
    elif text.startswith("任务："):
        text = text[len("任务：") :]
    marker = "\n\n补充上下文："
    if marker in text:
        head, _ = text.split(marker, 1)
        if head.strip():
            text = head
    normalized = " ".join(text.split())
    if len(normalized) <= 120:
        return normalized
    return normalized[:119] + "…"


async def load_subagent_details_in_session(
    session: AsyncSession,
    session_id: str,
    parent_run_id: str,
    *,
    include_tasks: bool = True,
    include_inputs: bool = True,
    include_approvals: bool = True,
) -> tuple[
    tuple[SubagentTaskSummary, ...],
    tuple[PendingSubagentInput, ...],
    tuple[PendingSubagentApproval, ...],
]:
    """在当前只读会话内统一加载主 Run 的子任务摘要、待回答澄清与待审批调用。

    无子任务或无需进一步查询时直接短路，所有需要的视图仅加载一次初始任务摘要元数据。
    """
    sub_rows = (
        await session.scalars(
            select(RunModel)
            .where(
                RunModel.session_id == session_id,
                RunModel.parent_run_id == parent_run_id,
            )
            .order_by(RunModel.created_at, RunModel.id)
        )
    ).all()
    if not sub_rows:
        return (), (), ()

    sub_by_id = {sub.id: sub for sub in sub_rows}
    task_index_by_id = {sub.id: idx for idx, sub in enumerate(sub_rows, start=1)}
    waiting_input_ids = (
        tuple(sub.id for sub in sub_rows if sub.status == RunStatus.WAITING_FOR_INPUT.value)
        if include_inputs
        else ()
    )
    waiting_approval_ids = (
        tuple(sub.id for sub in sub_rows if sub.status == RunStatus.WAITING_FOR_APPROVAL.value)
        if include_approvals
        else ()
    )
    # 仅当至少一个目标视图非空时才拉取子 Run 首条 user 执行消息，避免多余查库。
    summary_target_ids = (
        tuple(sub.id for sub in sub_rows)
        if include_tasks
        else tuple(dict.fromkeys((*waiting_input_ids, *waiting_approval_ids)))
    )
    task_summary_by_id: dict[str, str] = {}
    if summary_target_ids:
        exec_rows = (
            await session.scalars(
                select(ExecutionMessageModel)
                .where(
                    ExecutionMessageModel.session_id == session_id,
                    ExecutionMessageModel.run_id.in_(summary_target_ids),
                    ExecutionMessageModel.role == ExecutionRole.USER.value,
                )
                .order_by(ExecutionMessageModel.sequence, ExecutionMessageModel.id)
            )
        ).all()
        for exec_row in exec_rows:
            if exec_row.run_id in task_summary_by_id:
                continue
            domain_exec = execution_to_domain(exec_row)
            raw_text = "".join(
                part.text for part in domain_exec.parts if isinstance(part, TextPart)
            )
            task_summary_by_id[exec_row.run_id] = extract_subagent_task_summary(raw_text)

    subagent_tasks = (
        tuple(
            SubagentTaskSummary(
                sub_run_id=sub.id,
                parent_tool_call_id=sub.parent_tool_call_id or "",
                subagent_profile_id=sub.subagent_profile_id,
                subagent_slug=sub.subagent_slug or "",
                subagent_name=sub.subagent_name or "",
                task_index=task_index_by_id[sub.id],
                task_summary=task_summary_by_id.get(sub.id, ""),
                status=RunStatus(sub.status),
                safe_error_code=sub.safe_error_code,
                created_at=sub.created_at,
                first_started_at=sub.first_started_at,
                finished_at=sub.finished_at,
            )
            for sub in sub_rows
        )
        if include_tasks
        else ()
    )

    pending_subagent_inputs: tuple[PendingSubagentInput, ...] = ()
    if waiting_input_ids:
        input_rows = (
            await session.execute(
                select(RunInputRequestModel, ConversationMessageModel)
                .join(
                    ConversationMessageModel,
                    ConversationMessageModel.id == RunInputRequestModel.question_message_id,
                )
                .where(
                    RunInputRequestModel.run_id.in_(waiting_input_ids),
                    RunInputRequestModel.status == InputRequestStatus.PENDING.value,
                )
                .order_by(RunInputRequestModel.created_at, RunInputRequestModel.id)
            )
        ).all()
        pending_subagent_inputs = tuple(
            PendingSubagentInput(
                sub_run_id=sub_by_id[req.run_id].id,
                subagent_slug=sub_by_id[req.run_id].subagent_slug or "",
                subagent_name=sub_by_id[req.run_id].subagent_name or "",
                task_index=task_index_by_id.get(req.run_id, 1),
                task_summary=task_summary_by_id.get(req.run_id, ""),
                input_request_id=req.id,
                question_message_id=req.question_message_id,
                question_text="".join(
                    part.text
                    for part in conversation_to_domain(msg_row).parts
                    if isinstance(part, TextPart)
                ),
                created_at=req.created_at,
            )
            for req, msg_row in input_rows
        )

    pending_subagent_approvals: tuple[PendingSubagentApproval, ...] = ()
    if waiting_approval_ids:
        approval_rows = (
            await session.execute(
                select(RunToolCallModel, RunModel)
                .join(RunModel, RunModel.id == RunToolCallModel.run_id)
                .where(
                    RunToolCallModel.run_id.in_(waiting_approval_ids),
                    RunToolCallModel.status == ToolCallStatus.PENDING_APPROVAL.value,
                )
                .order_by(RunModel.created_at, RunToolCallModel.ordinal, RunToolCallModel.id)
            )
        ).all()
        if approval_rows:
            skill_projections = [
                None
                if call_row.safe_skill_projection is None
                else SafeSkillApprovalProjection.model_validate(call_row.safe_skill_projection)
                for call_row, _ in approval_rows
            ]
            enriched = await enrich_pending_approvals_in_session(
                session,
                [
                    (call_row, sub_run, skill_proj)
                    for (call_row, sub_run), skill_proj in zip(
                        approval_rows, skill_projections, strict=True
                    )
                ],
            )
            pending_subagent_approvals = tuple(
                PendingSubagentApproval(
                    sub_run_id=sub_run.id,
                    subagent_slug=sub_run.subagent_slug or "",
                    subagent_name=sub_run.subagent_name or "",
                    task_index=task_index_by_id.get(sub_run.id, 1),
                    task_summary=task_summary_by_id.get(sub_run.id, ""),
                    call_id=call_row.id,
                    batch_message_id=call_row.call_message_id,
                    ordinal=call_row.ordinal,
                    model_name=call_row.model_name,
                    tool_kind=(
                        None if call_row.tool_kind is None else ToolKind(call_row.tool_kind)
                    ),
                    requested_at=call_row.requested_at or sub_run.updated_at,
                    safe_skill_projection=skill_proj,
                    display_name=display_name,
                    approval_summary=approval_summary,
                    formatted_arguments=formatted_arguments,
                )
                for (call_row, sub_run), skill_proj, (
                    display_name,
                    approval_summary,
                    formatted_arguments,
                ) in zip(approval_rows, skill_projections, enriched, strict=True)
            )

    return subagent_tasks, pending_subagent_inputs, pending_subagent_approvals
