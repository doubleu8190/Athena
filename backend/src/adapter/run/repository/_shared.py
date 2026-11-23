"""Run 仓储共享的锁序、租约、事件、幂等请求与用量聚合辅助。"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.repository import filter_readable_targets_in_session
from adapter.file.model import SessionFileModel
from adapter.knowledge.model import KnowledgeBaseModel
from adapter.llm.model.profile import (
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.message.converter.message import conversation_to_domain, execution_to_domain
from adapter.message.model import ConversationMessageModel, ExecutionMessageModel
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.run.converter import (
    event_payload_to_json,
    run_to_domain,
)
from adapter.run.model import (
    RunEventModel,
    RunInputRequestModel,
    RunModel,
    RunRequestModel,
    RunToolCallModel,
)
from adapter.session.model import SessionModel
from adapter.skill.model import SkillExecutionModel
from common.ids import new_id
from domain.access import ResourceAccessTarget, ResourceType, VisibilityScope
from domain.message import (
    ConversationMessage,
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    SessionNotFoundError,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.run.model import (
    InputRequestStatus,
    Run,
    RunAttemptUsage,
    RunEventPayload,
    RunFailedPayload,
    RunRequestOperation,
    RunRequestResult,
    RunStatus,
    RunSubagentsResumedPayload,
    RunSubagentStatusChangedPayload,
    RunToolCallFinishedPayload,
    RunUsage,
)
from domain.run.model.errors import (
    RunConflictError,
    RunInputResourceError,
    RunKnowledgeBaseNotFoundError,
    RunNotFoundError,
    RunStateError,
)
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
    SafeSkillApprovalProjection,
    SkillExecutionStatus,
)
from domain.tool import (
    FormattedToolArgument,
    RuntimeToolSnapshot,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    format_tool_arguments,
    persisted_status_for_outcome,
    render_approval_summary,
)

logger = logging.getLogger(__name__)

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


def _is_skill_tool_call(
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


def _purpose_total(attempts: tuple[RunAttemptUsage, ...], purpose: str) -> int | None:
    """按调用用途汇总已知 Token 数。"""
    values = [
        item.total_tokens
        for item in attempts
        if item.purpose == purpose and item.total_tokens is not None
    ]
    return None if not values else sum(values)


class RunRepositorySupportMixin:
    """提供跨生命周期、工具批处理与上下文子模块共享的事务内辅助能力。"""

    _session_factory: async_sessionmaker[AsyncSession]
    _message_repo: SqlAlchemyMessageRepository

    @property
    def _messages(self) -> SqlAlchemyMessageRepository:
        """平滑兼容属性，指向内部消息仓储实例。"""
        return self._message_repo

    @staticmethod
    def _aggregate_usage(attempts: tuple[RunAttemptUsage, ...]) -> RunUsage:
        def total(field: str) -> int | None:
            values = [getattr(item, field) for item in attempts]
            return (
                None
                if not any(value is not None for value in values)
                else sum(value for value in values if value is not None)
            )

        return RunUsage(
            input_tokens=total("input_tokens"),
            output_tokens=total("output_tokens"),
            total_tokens=total("total_tokens"),
            complete=bool(attempts) and all(item.complete for item in attempts),
            attempts=attempts,
            answer_tokens=_purpose_total(attempts, "answer"),
            compression_tokens=_purpose_total(attempts, "compression"),
        )

    @classmethod
    def _merge_usage(cls, current_json: dict[str, object], incoming: RunUsage) -> RunUsage:
        current = RunUsage.model_validate(current_json)
        by_key = {
            (
                item.execution_epoch,
                item.purpose,
                item.operation_id,
                item.batch_index,
                item.attempt,
            ): item
            for item in current.attempts
        }
        for item in incoming.attempts:
            key = (
                item.execution_epoch,
                item.purpose,
                item.operation_id,
                item.batch_index,
                item.attempt,
            )
            existing = by_key.get(key)
            if existing is not None and existing != item:
                raise RunConflictError("run_usage_attempt_conflict")
            by_key[key] = item
        if not incoming.attempts:
            if any(
                value is not None
                for value in (incoming.input_tokens, incoming.output_tokens, incoming.total_tokens)
            ):
                raise RunConflictError("run_usage_attempts_required")
            return current
        if not current.attempts:
            legacy_input = current.input_tokens
            legacy_output = current.output_tokens
            legacy_total = current.total_tokens
            legacy_answer = current.total_tokens
            legacy_complete = current.complete
        else:
            legacy_input = current.legacy_input_tokens
            legacy_output = current.legacy_output_tokens
            legacy_total = current.legacy_total_tokens
            legacy_answer = current.legacy_answer_tokens
            legacy_complete = current.legacy_complete
        keys = sorted(
            by_key,
            key=lambda item: (item[0], item[1], item[2] or "", item[3], item[4]),
        )
        aggregate = cls._aggregate_usage(tuple(by_key[key] for key in keys))
        if any(value is not None for value in (legacy_input, legacy_output, legacy_total)):
            aggregate = aggregate.model_copy(
                update={
                    "input_tokens": None
                    if aggregate.input_tokens is None and legacy_input is None
                    else (aggregate.input_tokens or 0) + (legacy_input or 0),
                    "output_tokens": None
                    if aggregate.output_tokens is None and legacy_output is None
                    else (aggregate.output_tokens or 0) + (legacy_output or 0),
                    "total_tokens": None
                    if aggregate.total_tokens is None and legacy_total is None
                    else (aggregate.total_tokens or 0) + (legacy_total or 0),
                    "legacy_input_tokens": legacy_input,
                    "legacy_output_tokens": legacy_output,
                    "legacy_total_tokens": legacy_total,
                    "legacy_answer_tokens": legacy_answer,
                    "legacy_complete": legacy_complete,
                    "answer_tokens": None
                    if aggregate.answer_tokens is None and legacy_answer is None
                    else (aggregate.answer_tokens or 0) + (legacy_answer or 0),
                    "complete": aggregate.complete and legacy_complete,
                }
            )
        return aggregate

    @staticmethod
    async def _has_unsettled_tool_calls(session: AsyncSession, run_id: str) -> bool:
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

    async def _settle_unfinished_tool_calls(
        self,
        session: AsyncSession,
        locked_session: SessionModel,
        run: RunModel,
        settled_at: datetime,
        *,
        records: list[RunToolCallModel] | None = None,
    ) -> None:
        """Cancel undispatched and mark already-started calls unknown."""
        # 由调用方持有事务与 Session→Run 锁；这里只收敛未完成调用，不提交事务。
        if records is None:
            # 未由调用方预先锁定时，在事务内按调用序号加行锁读取全部未完成调用，
            # 避免与并发审批或工具执行产生状态竞争。
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
                await self._message_repo.append_execution_in_session(
                    session,
                    result_draft,
                    locked_session=locked_session,
                    managed_run_id=run.id,
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
                await self._settle_running_skill_execution(
                    session,
                    record.id,
                    settled_at,
                )
            # 每个调用都追加终态事件，保证 SSE 与读取模型看到一致的收敛结果。
            await self._append_event(
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

    @staticmethod
    async def _settle_running_skill_execution(
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

    async def _fail_run_in_transaction(
        self,
        session: AsyncSession,
        locked_session: SessionModel,
        run: RunModel,
        safe_error_code: str,
        failed_at: datetime,
    ) -> None:
        """Converge unfinished calls before committing the failed terminal state."""
        await self._settle_unfinished_tool_calls(
            session,
            locked_session,
            run,
            failed_at,
        )
        run.status = RunStatus.FAILED.value
        run.safe_error_code = safe_error_code
        run.finished_at = failed_at
        run.updated_at = failed_at
        self._clear_lease(run)
        await self._close_pending_input(session, run, failed_at)
        await self._append_event(
            session,
            run,
            RunFailedPayload(safe_error_code=safe_error_code),
            failed_at,
        )
        await self._try_wake_parent_run(session, locked_session, run, failed_at)

    async def _try_wake_parent_run(
        self,
        session: AsyncSession,
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
        await self._append_event(
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
                output_text = await self._load_subagent_final_output(session, item.id)
                artifacts = await self._load_subagent_artifacts(session, item.id)
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
        await self._message_repo.append_execution_in_session(
            session,
            result_draft,
            locked_session=locked_session,
            managed_run_id=parent.id,
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
        self._clear_lease(parent)
        await self._append_event(
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

    @staticmethod
    async def _load_subagent_final_output(session: AsyncSession, sub_run_id: str) -> str:
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

    @staticmethod
    async def _load_subagent_artifacts(
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

    @staticmethod
    def _build_model_snapshot_json(profile: ModelProfileVersionModel) -> dict[str, object]:
        """构造创建 Run 时的非密钥模型元数据快照，严禁写入任何明文或密文 api_key。"""
        return {
            "id": profile.id,
            "name": profile.name,
            "protocol": profile.protocol,
            "base_url": profile.base_url,
            "model": profile.model,
            "context_window_tokens": profile.context_window_tokens,
            "tokenizer_encoding": profile.tokenizer_encoding,
            "supports_stream": bool(profile.supports_stream),
            "supports_tools": bool(profile.supports_tools),
            "supports_parallel_tool_calls": bool(profile.supports_parallel_tool_calls),
            "max_output_tokens": profile.max_output_tokens,
            "request_timeout_seconds": profile.request_timeout_seconds,
            "execution_timeout_seconds": profile.execution_timeout_seconds,
            "max_concurrency": profile.max_concurrency,
            "temperature": profile.temperature,
            "scenario_label": profile.scenario_label,
            "supports_vision": bool(profile.supports_vision),
            "version": 1 if profile.version is None else int(profile.version),
        }

    @staticmethod
    async def _enrich_pending_approvals_in_session(
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
            if skill_proj is not None or _is_skill_tool_call(
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

            live_tool = (
                live_tools_by_id.get(call_row.tool_id) if call_row.tool_id is not None else None
            )
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

    async def _lock_default_profile_row(self, session: AsyncSession) -> ModelProfileVersionModel:
        profile_id = await session.scalar(
            select(DefaultModelProfileModel.profile_id).where(
                DefaultModelProfileModel.singleton_id == 1
            )
        )
        if profile_id is None:
            raise RunStateError("llm_default_profile_unavailable")
        # 使用 FOR SHARE 共享锁：允许多个 Run 并发提交，同时与管理员禁用/修改互斥。
        profile = await session.scalar(
            select(ModelProfileVersionModel)
            .where(
                ModelProfileVersionModel.id == profile_id,
                ModelProfileVersionModel.deleted_at.is_(None),
            )
            .with_for_update(read=True)
        )
        # 共享锁定默认配置指针，允许并发读取默认版本，仍与管理员切换默认配置互斥。
        default = await session.scalar(
            select(DefaultModelProfileModel)
            .where(DefaultModelProfileModel.singleton_id == 1)
            .with_for_update(read=True)
        )
        if default is None or default.profile_id != profile_id:
            raise RunConflictError("llm_default_profile_changed")
        if profile is None:
            raise RunStateError("llm_default_profile_unavailable")
        if not profile.enabled or not profile.supports_stream or not profile.supports_tools:
            raise RunStateError("llm_default_profile_unavailable")
        if (
            profile.api_key_ciphertext is None
            or profile.api_key_nonce is None
            or profile.api_key_key_version is None
        ):
            raise RunStateError("llm_default_profile_unavailable")
        return profile

    async def _lock_default_profile_id(self, session: AsyncSession) -> str:
        return (await self._lock_default_profile_row(session)).id

    async def _lock_selected_profile_row(
        self, session: AsyncSession, profile_id: str
    ) -> ModelProfileVersionModel:
        """在提交事务内共享锁定用户显式选择的模型版本，校验可见、启用、未删且能力完整。"""
        profile = await session.scalar(
            select(ModelProfileVersionModel)
            .where(
                ModelProfileVersionModel.id == profile_id,
                ModelProfileVersionModel.deleted_at.is_(None),
            )
            .with_for_update(read=True)
        )
        if (
            profile is None
            or not profile.enabled
            or not profile.visible_to_users
            or not profile.supports_stream
            or not profile.supports_tools
            or profile.api_key_ciphertext is None
            or profile.api_key_nonce is None
            or profile.api_key_key_version is None
        ):
            raise RunInputResourceError("model_profile_unavailable")
        return profile

    async def _lock_selected_profile_id(self, session: AsyncSession, profile_id: str) -> str:
        return (await self._lock_selected_profile_row(session, profile_id)).id

    async def _lock_retry_profile_row(
        self, session: AsyncSession, profile_id: str
    ) -> ModelProfileVersionModel:
        """在重试事务内共享锁定原 Run 的模型版本，复核启用、未删、能力完整且仍对用户可用。

        遵循与 _lock_default_profile_id 一致的锁顺序（先模型行、再默认指针行）：
        除常规能力与凭据校验外，要求目标模型仍满足 visible_to_users=True 或恰为当前默认模型。
        """
        profile = await session.scalar(
            select(ModelProfileVersionModel)
            .where(
                ModelProfileVersionModel.id == profile_id,
                ModelProfileVersionModel.deleted_at.is_(None),
            )
            .with_for_update(read=True)
        )
        if (
            profile is None
            or not profile.enabled
            or not profile.supports_stream
            or not profile.supports_tools
            or profile.api_key_ciphertext is None
            or profile.api_key_nonce is None
            or profile.api_key_key_version is None
        ):
            raise RunInputResourceError("model_profile_unavailable")
        if not profile.visible_to_users:
            default = await session.scalar(
                select(DefaultModelProfileModel)
                .where(DefaultModelProfileModel.singleton_id == 1)
                .with_for_update(read=True)
            )
            if default is None or default.profile_id != profile.id:
                raise RunInputResourceError("model_profile_unavailable")
        return profile

    async def _lock_retry_profile_id(self, session: AsyncSession, profile_id: str) -> str:
        return (await self._lock_retry_profile_row(session, profile_id)).id

    async def _validate_explicit_knowledge_base_ids(
        self,
        session: AsyncSession,
        owner_id: str,
        knowledge_base_ids: tuple[str, ...] | None,
    ) -> list[str] | None:
        """在同一提交事务内实时校验显式知识库存在、未删除且当前用户可读，防止跨部门 ID 探测。"""
        if knowledge_base_ids is None:
            return None
        ordered_ids = tuple(dict.fromkeys(knowledge_base_ids))
        if len(ordered_ids) > 20:
            raise RunInputResourceError("run_knowledge_base_ids_invalid")
        if not ordered_ids:
            return []
        kb_rows = (
            await session.execute(
                select(
                    KnowledgeBaseModel.id,
                    KnowledgeBaseModel.visibility_scope,
                    KnowledgeBaseModel.owner_id,
                    KnowledgeBaseModel.owner_department_id,
                ).where(
                    KnowledgeBaseModel.id.in_(ordered_ids),
                    KnowledgeBaseModel.deleted_time.is_(None),
                )
            )
        ).all()
        if len(kb_rows) != len(ordered_ids):
            raise RunKnowledgeBaseNotFoundError("knowledge_base_not_found")
        targets = tuple(
            ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id=row[0],
                visibility_scope=VisibilityScope(row[1]),
                owner_id=row[2],
                owner_department_id=row[3],
            )
            for row in kb_rows
        )
        readable_ids = await filter_readable_targets_in_session(session, owner_id, targets)
        if readable_ids != set(ordered_ids):
            raise RunKnowledgeBaseNotFoundError("knowledge_base_not_found")
        return list(ordered_ids)

    @staticmethod
    async def _current_compression_policy_id(session: AsyncSession) -> str | None:
        """读取当前策略版本，供新 Run 固定配置快照。"""
        return await session.scalar(
            select(CurrentCompressionPolicyModel.policy_version_id).where(
                CurrentCompressionPolicyModel.singleton_id == 1
            )
        )

    @staticmethod
    async def _next_run_created_at(session: AsyncSession, session_id: str) -> datetime:
        """在 Session 锁内生成严格晚于已有 Run 的创建时间。"""
        database_now = cast(datetime | None, await session.scalar(select(func.clock_timestamp())))
        latest_created_at = cast(
            datetime | None,
            await session.scalar(
                select(func.max(RunModel.created_at)).where(RunModel.session_id == session_id)
            ),
        )
        if database_now is None:
            raise RuntimeError("run_database_clock_unavailable")
        if database_now.tzinfo is None:
            database_now = database_now.replace(tzinfo=UTC)
        if latest_created_at is not None and latest_created_at.tzinfo is None:
            latest_created_at = latest_created_at.replace(tzinfo=UTC)
        if latest_created_at is None or database_now > latest_created_at:
            return database_now
        return latest_created_at + timedelta(microseconds=1)

    async def _validate_and_merge_input_file_ids(
        self,
        session: AsyncSession,
        session_id: str,
        owner_id: str,
        *,
        existing: tuple[str, ...],
        incoming: tuple[str, ...],
    ) -> tuple[str, ...]:
        """在同一 Run 事务中验证文件归属，并按首次提供顺序合并。"""
        if len(incoming) != len(set(incoming)):
            raise RunInputResourceError("run_input_file_ids_duplicate")
        ordered = tuple(dict.fromkeys((*existing, *incoming)))
        if len(ordered) != len(existing) + len(incoming):
            raise RunInputResourceError("run_input_file_ids_duplicate")
        if not incoming:
            return ordered
        visible = set(
            (
                await session.scalars(
                    select(SessionFileModel.id).where(
                        SessionFileModel.id.in_(incoming),
                        SessionFileModel.session_id == session_id,
                        SessionFileModel.owner_id == owner_id,
                    )
                )
            ).all()
        )
        if visible != set(incoming):
            raise RunInputResourceError("run_input_file_unavailable")
        return ordered

    @staticmethod
    async def _lock_session(session: AsyncSession, session_id: str, owner_id: str) -> SessionModel:
        row = await session.scalar(
            select(SessionModel)
            .where(
                SessionModel.id == session_id,
                SessionModel.owner_id == owner_id,
                SessionModel.deleted_time.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            raise SessionNotFoundError("session_not_found")
        return row

    @staticmethod
    async def _lock_session_by_id(session: AsyncSession, session_id: str) -> SessionModel:
        row = await session.scalar(
            select(SessionModel)
            .where(SessionModel.id == session_id, SessionModel.deleted_time.is_(None))
            .with_for_update()
        )
        if row is None:
            raise SessionNotFoundError("session_not_found")
        return row

    @staticmethod
    async def _lock_run(session: AsyncSession, session_id: str, run_id: str) -> RunModel:
        row = await session.scalar(
            select(RunModel)
            .where(RunModel.id == run_id, RunModel.session_id == session_id)
            .with_for_update()
        )
        if row is None:
            raise RunNotFoundError("run_not_found")
        return row

    async def _lock_worker_run(
        self, session: AsyncSession, run_id: str, worker_id: str, execution_epoch: int
    ) -> RunModel:
        session_id = await session.scalar(select(RunModel.session_id).where(RunModel.id == run_id))
        if session_id is None:
            raise RunNotFoundError("run_not_found")
        await self._lock_session_by_id(session, session_id)
        row = await self._lock_run(session, session_id, run_id)
        if row.worker_id != worker_id or row.execution_epoch != execution_epoch:
            raise RunStateError("run_execution_lease_lost")
        return row

    @staticmethod
    def _lease_is_valid(
        row: RunModel, worker_id: str, execution_epoch: int, db_now: datetime | None
    ) -> bool:
        return bool(
            db_now is not None
            and row.worker_id == worker_id
            and row.execution_epoch == execution_epoch
            and row.lease_expires_at is not None
            and row.lease_expires_at > db_now
        )

    @staticmethod
    def _work_deadline_is_valid(row: RunModel, db_now: datetime | None) -> bool:
        """判断当前执行是否仍可继续产生业务结果。"""
        return bool(
            db_now is not None
            and row.execution_deadline_at is not None
            and row.execution_deadline_at > db_now
        )

    @staticmethod
    def _clear_lease(row: RunModel) -> None:
        row.worker_id = None
        row.lease_expires_at = None
        row.execution_started_at = None
        row.execution_deadline_at = None

    @staticmethod
    async def _append_event(
        session: AsyncSession,
        run: RunModel,
        payload: RunEventPayload,
        created_at: datetime,
    ) -> None:
        run.event_sequence += 1
        event = RunEventModel(
            id=new_id("runevt"),
            run_id=run.id,
            session_id=run.session_id,
            sequence=run.event_sequence,
            type=payload.type,
            created_at=created_at,
            payload=event_payload_to_json(payload),
        )
        session.add(event)
        await session.flush()

    @staticmethod
    async def _close_pending_input(
        session: AsyncSession, run: RunModel, closed_at: datetime
    ) -> None:
        request = await session.scalar(
            select(RunInputRequestModel)
            .where(
                RunInputRequestModel.run_id == run.id,
                RunInputRequestModel.status == InputRequestStatus.PENDING.value,
            )
            .with_for_update()
        )
        if request is not None:
            request.status = InputRequestStatus.CLOSED.value
            request.closed_at = closed_at

    @staticmethod
    async def _find_request(
        session: AsyncSession,
        owner_id: str,
        session_id: str,
        operation: RunRequestOperation,
        request_id: str,
    ) -> RunRequestModel | None:
        return await session.scalar(
            select(RunRequestModel).where(
                RunRequestModel.owner_id == owner_id,
                RunRequestModel.session_id == session_id,
                RunRequestModel.operation == operation.value,
                RunRequestModel.request_id == request_id,
            )
        )

    @staticmethod
    def _check_fingerprint(row: RunRequestModel, fingerprint: str) -> None:
        if row.fingerprint != fingerprint:
            raise RunConflictError("run_request_id_conflict")

    @staticmethod
    async def _store_request(
        session: AsyncSession,
        owner_id: str,
        session_id: str,
        operation: RunRequestOperation,
        request_id: str,
        fingerprint: str,
        result: RunRequestResult,
        created_at: datetime,
    ) -> None:
        session.add(
            RunRequestModel(
                id=new_id("runreq"),
                owner_id=owner_id,
                session_id=session_id,
                operation=operation.value,
                request_id=request_id,
                fingerprint=fingerprint,
                result=result.model_dump(mode="json", exclude_none=True),
                created_at=created_at,
            )
        )
        await session.flush()

    @staticmethod
    async def _load_submit_result(
        session: AsyncSession, request: RunRequestModel
    ) -> tuple[Run, ConversationMessage]:
        result = RunRequestResult.model_validate(request.result)
        run = await session.get(RunModel, result.run_id)
        message = await session.get(ConversationMessageModel, result.message_id)
        if run is None or message is None:
            raise RunNotFoundError("run_request_result_missing")
        return run_to_domain(run), conversation_to_domain(message)
