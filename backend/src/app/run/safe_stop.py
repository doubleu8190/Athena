"""Run 上下文安全停止的确定性报告构造与原子终态提交协作者。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol, cast

from app.run.context import RunInputFileContext
from domain.message import (
    ArtifactFileRef,
    ConversationMessage,
    ConversationMessageDraft,
    ConversationRole,
    ExecutionMessage,
    FileRefPart,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolResultPart,
)
from domain.run.model import (
    ContextStopReason,
    FailRunCommand,
    Run,
    RunContextSafetyCommitError,
    RunUsage,
)
from domain.run.model.errors import RunError, RunNotFoundError
from domain.run.ports import RunRepository
from domain.session.model.errors import SessionNotFoundError
from domain.skill import RunSkillPromptContext
from domain.tool import AuthorizedRunContext, ToolRetryContext

CONTEXT_SAFETY_STOP_ERROR_CODE = "run_context_safety_stop"
CONTEXT_SAFETY_COMMIT_MAX_ATTEMPTS = 3

CONTEXT_STOP_REASON_TEXT = {
    ContextStopReason.NO_COMPRESSIBLE_HISTORY: "没有新的可压缩旧历史",
    ContextStopReason.COMPRESSION_NOT_CONFIGURED: "未启用新格式压缩策略",
    ContextStopReason.COMPRESSION_MODEL_UNAVAILABLE: "压缩模型不可用",
    ContextStopReason.COMPRESSION_BUDGET_EXHAUSTED: "没有可用压缩调用额度",
    ContextStopReason.COMPRESSION_INPUT_TOO_LARGE: "压缩输入无法一次装入模型窗口",
    ContextStopReason.COMPRESSION_FAILED: "唯一一次压缩调用失败",
    ContextStopReason.COMPRESSION_INSUFFICIENT: "压缩后完整输入仍超过安全线",
    ContextStopReason.HARD_WINDOW_EXCEEDED: "完整输入与输出预留超过模型硬窗",
}

_UNFINISHED_TOOL_STATUSES = frozenset(
    {
        "pending_approval",
        "approved",
        "executing",
        "unknown",
        "failed",
        "cancelled",
    }
)


class SafeStopState(Protocol):
    """安全收尾所需的最小执行态视图，解耦于 RunExecutor 内部状态类。"""

    run: Run
    input_files: tuple[RunInputFileContext, ...]
    retry_context: tuple[ToolRetryContext, ...]


def system_context_report(
    reason: ContextStopReason,
    persisted_state: dict[str, object],
) -> str:
    """基于权威持久化元数据生成确定性四段安全停止报告，绝不宣称任务完成。"""
    reason_text = CONTEXT_STOP_REASON_TEXT[reason]
    tool_calls = cast(list[dict[str, object]], persisted_state.get("tool_calls", []))
    completed = [
        f"- 工具 {item.get('model_name')} 已完成（调用状态 {item.get('status')}）"
        for item in tool_calls
        if item.get("status") == "completed"
    ]
    loaded_skills = cast(dict[str, object], persisted_state.get("loaded_skills", {}))
    active_skills = cast(list[dict[str, object]], loaded_skills.get("active", []))
    completed.extend(
        f"- Skill {item.get('name')} 已加载完整指南"
        f"（skill_id: {item.get('skill_id')}，version_id: {item.get('version_id')}，"
        f"version_label: {item.get('version_label')}）"
        for item in active_skills
    )
    conversation = cast(list[dict[str, object]], persisted_state.get("conversation", []))
    execution = cast(list[dict[str, object]], persisted_state.get("execution", []))
    completed.append(
        f"- 当前 Run 已持久化 {len(conversation)} 条会话消息、"
        f"{len(execution)} 条执行消息、{len(tool_calls)} 条工具调用记录。"
    )

    unfinished = [
        f"- 工具 {item.get('model_name')} 未完成（调用状态 {item.get('status')}，"
        f"安全码 {item.get('safe_error_code') or '无'}）。"
        for item in tool_calls
        if item.get("status") in _UNFINISHED_TOOL_STATUSES
    ]
    retry_context = cast(list[dict[str, object]], persisted_state.get("retry_context", []))
    unfinished.extend(
        f"- 重试来源 Run {item.get('run_id')} 的工具 {item.get('tool_name')} 曾失败"
        f"（安全码 {item.get('safe_error_code')}）。"
        for item in retry_context
    )
    unfinished.append("- 业务循环已安全停止，尚未形成可安全提交的最终回答。")

    artifacts = cast(list[dict[str, object]], persisted_state.get("artifacts", []))
    input_files = cast(list[dict[str, object]], persisted_state.get("input_files", []))
    file_references = cast(
        list[dict[str, object]],
        persisted_state.get("message_file_references", []),
    )
    produced = [
        *(
            f"- 交付文件 {item.get('filename')}（file_id: {item.get('file_id')}，"
            f"mime_type: {item.get('mime_type')}，size_bytes: {item.get('size_bytes')}）"
            for item in artifacts
        ),
        *(
            f"- 输入文件 {item.get('filename')}（file_id: {item.get('file_id')}，"
            f"mime_type: {item.get('mime_type')}，size_bytes: {item.get('size_bytes')}；"
            "该引用不是交付产物）"
            for item in input_files
        ),
        *(
            f"- 消息文件引用 {item.get('filename')}（file_id: {item.get('file_id')}，"
            f"mime_type: {item.get('mime_type')}，size_bytes: {item.get('size_bytes')}；"
            "该引用不是交付产物）"
            for item in file_references
        ),
    ]
    compression_reason = cast(
        dict[str, object] | None,
        persisted_state.get("compression_stop_reason"),
    )
    compression_stop_line = (
        ()
        if compression_reason is None
        else (
            "- 压缩尝试结论："
            f"{compression_reason.get('text')}"
            f"（code: {compression_reason.get('code')}）。",
        )
    )
    return "\n".join(
        (
            "上下文安全停止。任务未标记为完成。",
            "",
            "已完成：",
            *completed,
            "",
            "产出：",
            *(produced or ["- 没有已持久化的交付文件或当前 Run 文件引用。"]),
            "",
            "未完成：",
            *unfinished,
            "",
            "停止原因：",
            f"- {reason_text}；完整业务输入达到 90% 安全线或无法满足 I+O<=W。",
            *compression_stop_line,
        )
    )


class SafeStopReporter:
    """上下文安全停止时的确定性报告构造与原子终态提交协作者。"""

    def __init__(
        self,
        repository: RunRepository,
        provider: object | None = None,
        *,
        id_factory: Callable[[str], str],
        now: Callable[[], datetime],
    ) -> None:
        # 保留可选 provider 位置参数仅用于向后兼容直接实例化测试；安全停止不再调用 LLM。
        del provider
        self._repository = repository
        self._id_factory = id_factory
        self._now = now

    async def stop_for_context_safety(
        self,
        state: SafeStopState,
        runtime: object | None,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        reason: ContextStopReason,
        *,
        remaining_seconds: Callable[[], float],
        authorize: Callable[[Run], Awaitable[AuthorizedRunContext | None]] | None,
        publish_events: Callable[[], Awaitable[None]],
        skill_context: RunSkillPromptContext | None = None,
        compression_stop_reason: ContextStopReason | None = None,
    ) -> None:
        """无模型调用生成确定性系统报告，并把报告与 FAILED 终态原子提交。"""
        del runtime
        run = state.run
        worker_id = run.worker_id
        if worker_id is None:
            raise RunContextSafetyCommitError(None, "worker_owner_missing")
        # 取消、失租和鉴权高于报告生成；任一失败都不能越权写入报告。
        try:
            is_live = await self._repository.is_live_execution(run.id, run.execution_epoch)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise RunContextSafetyCommitError(None, "liveness_check_failed") from error
        if not is_live:
            raise RunContextSafetyCommitError(None, "lease_lost")
        if authorize is not None:
            try:
                authorized = await authorize(run)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise RunContextSafetyCommitError(None, "authorization_check_failed") from error
            if authorized is None:
                await self._commit_context_authorization_failure(
                    state,
                    remaining_seconds=remaining_seconds,
                    authorize=authorize,
                )
                await publish_events()
                return

        persisted_state = await self.build_context_report_state(
            state,
            messages,
            skill_context=skill_context,
            compression_stop_reason=compression_stop_reason,
        )
        report_text = system_context_report(reason, persisted_state)
        finished_at = self._now()
        report_message = ConversationMessageDraft(
            id=self._id_factory("msg"),
            session_id=run.session_id,
            owner_id=run.owner_id,
            role=ConversationRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            parts=(TextPart(report_text),),
            reply_to_message_id=run.trigger_message_id,
            run_id=run.id,
            client_message_id=None,
            created_at=finished_at,
            completed_at=finished_at,
            metadata=MessageMetadata(provider="system_context_safety", model=None),
            context_turn_id=run.trigger_message_id,
        )
        report_command = FailRunCommand(
            run_id=run.id,
            worker_id=worker_id,
            execution_epoch=run.execution_epoch,
            safe_error_code=CONTEXT_SAFETY_STOP_ERROR_CODE,
            failed_at=finished_at,
            report_message=report_message,
            usage=RunUsage(),
            context_stop_reason=reason,
        )
        await self._commit_context_safety_failure(
            state,
            report_command,
            require_authorization=True,
            remaining_seconds=remaining_seconds,
            authorize=authorize,
        )
        await publish_events()

    async def _commit_context_authorization_failure(
        self,
        state: SafeStopState,
        *,
        remaining_seconds: Callable[[], float],
        authorize: Callable[[Run], Awaitable[AuthorizedRunContext | None]] | None,
    ) -> Run:
        """用稳定命令记录安全收尾阶段的鉴权失效，绝不附带报告消息。"""
        run = state.run
        if run.worker_id is None:
            raise RunContextSafetyCommitError(None, "worker_owner_missing")
        command = FailRunCommand(
            run_id=run.id,
            worker_id=run.worker_id,
            execution_epoch=run.execution_epoch,
            safe_error_code="run_auth_session_invalid",
            failed_at=self._now(),
            usage=RunUsage(),
        )
        # 鉴权失效本身就是终态原因；只复核生命周期，不再要求已被撤销的鉴权恢复。
        return await self._commit_context_safety_failure(
            state,
            command,
            require_authorization=False,
            remaining_seconds=remaining_seconds,
            authorize=authorize,
        )

    async def _commit_context_safety_failure(
        self,
        state: SafeStopState,
        command: FailRunCommand,
        *,
        require_authorization: bool,
        remaining_seconds: Callable[[], float],
        authorize: Callable[[Run], Awaitable[AuthorizedRunContext | None]] | None,
    ) -> Run:
        """在有效执行权内最多 3 次幂等重放同一安全收尾命令。"""
        run = state.run
        last_error: Exception | None = None
        failure_reason = "commit_failed"
        for _ in range(CONTEXT_SAFETY_COMMIT_MAX_ATTEMPTS):
            if remaining_seconds() <= 0:
                failure_reason = "deadline_expired"
                break
            try:
                if not await self._repository.is_live_execution(run.id, run.execution_epoch):
                    failure_reason = "lease_lost"
                    break
                if require_authorization and authorize is not None and await authorize(run) is None:
                    failure_reason = "authorization_lost"
                    break
                failed = await self._repository.fail(command)
            except asyncio.CancelledError:
                raise
            except (RunError, RunNotFoundError, SessionNotFoundError) as error:
                # 取消、失租、删除或状态冲突不是瞬态写失败，不能靠重放越过。
                last_error = error
                failure_reason = "command_rejected"
                break
            except Exception as error:
                # 事务结果可能不确定；同命令幂等重放可从仓储得到已提交终态。
                last_error = error
                continue
            if failed is not None:
                return failed
            # None 表示取消、失租、deadline 或状态竞争已拒绝命令，不能继续补写。
            failure_reason = "command_rejected"
            break

        report_message_id = None if command.report_message is None else command.report_message.id
        raise RunContextSafetyCommitError(report_message_id, failure_reason) from last_error

    async def build_context_report_state(
        self,
        state: SafeStopState,
        messages: tuple[ConversationMessage | ExecutionMessage, ...],
        *,
        skill_context: RunSkillPromptContext | None = None,
        compression_stop_reason: ContextStopReason | None = None,
    ) -> dict[str, object]:
        """汇总当前 Run 的隐私脱敏状态元数据，绝不包含图片 base64、Skill 正文或工具原始输出。"""
        run = state.run
        conversation: list[dict[str, object]] = []
        execution: list[dict[str, object]] = []
        artifacts: list[dict[str, object]] = []
        message_file_references: list[dict[str, object]] = []

        def collect_artifact(artifact: ArtifactFileRef) -> None:
            artifacts.append(
                {
                    "file_id": artifact.file_id,
                    "filename": artifact.filename,
                    "label": artifact.label,
                    "mime_type": artifact.mime_type,
                    "size_bytes": artifact.size_bytes,
                }
            )

        # 仅扫描消息部件提取文件引用与产物元数据，绝不保留文本正文、调用参数或工具原始输出。
        for message in messages:
            if isinstance(message, ConversationMessage):
                if message.id != run.trigger_message_id and message.run_id != run.id:
                    continue
                conversation.append(
                    {
                        "sequence": message.sequence,
                        "role": message.role.value,
                        "status": message.status.value,
                    }
                )
            else:
                execution.append(
                    {
                        "sequence": message.sequence,
                        "role": message.role.value,
                    }
                )
            for part in message.parts:
                if isinstance(part, FileRefPart):
                    message_file_references.append(
                        {
                            "type": "file_ref",
                            "file_id": part.file_id,
                            "filename": part.filename,
                            "mime_type": part.mime_type,
                            "size_bytes": part.size_bytes,
                        }
                    )
                elif isinstance(part, ToolResultPart):
                    for artifact in part.artifacts:
                        collect_artifact(artifact)

        # 分页读取工具调用状态，仅投影安全状态与错误码，隔离原始输出载荷。
        tool_calls: list[dict[str, object]] = []
        cursor = 0
        while True:
            page = await self._repository.list_tool_calls(
                run.session_id,
                run.owner_id,
                run.id,
                after_ordinal=cursor,
                limit=100,
            )
            if page is None:
                break
            for item in page.items:
                tool_calls.append(
                    {
                        "ordinal": item.ordinal,
                        "model_name": item.model_name,
                        "status": item.status.value,
                        "safe_error_code": item.safe_error_code,
                        "outcome_status": (
                            None if item.outcome is None else item.outcome.status.value
                        ),
                    }
                )
            if page.next_after_ordinal is None:
                break
            cursor = page.next_after_ordinal

        return {
            "run_id": run.id,
            "trigger_message_id": run.trigger_message_id,
            "conversation": conversation,
            "execution": execution,
            "message_file_references": message_file_references,
            # 安全止损隔离：仅保留最小文件元数据（含可选 vision_status），
            # 严禁将执行期内存态视觉图片或任何 base64 数据写入报告或数据库。
            "input_files": [
                {
                    "file_id": item.file_id,
                    "filename": item.filename,
                    "label": item.label,
                    "mime_type": item.mime_type,
                    "size_bytes": item.size_bytes,
                    "source": item.source,
                    "extraction_status": item.extraction_status,
                    "extracted_text_bytes": item.extracted_text_bytes,
                    "extraction_error_code": item.extraction_error_code,
                    **(
                        {"vision_status": item.vision_status}
                        if item.vision_status is not None
                        else {}
                    ),
                }
                for item in state.input_files
            ],
            # 仅记录 Skill 标识元数据，严禁落入 Skill 完整 guide 正文。
            "loaded_skills": {
                "catalog": (
                    []
                    if skill_context is None
                    else [
                        {
                            "skill_id": item.skill_id,
                            "name": item.name,
                            "description": item.description,
                            "version_id": item.version_id,
                            "version_label": item.version_label,
                        }
                        for item in skill_context.catalog
                    ]
                ),
                "active": (
                    []
                    if skill_context is None
                    else [
                        {
                            "skill_id": item.skill_id,
                            "name": item.name,
                            "version_id": item.version_id,
                            "version_label": item.version_label,
                        }
                        for item in skill_context.active
                    ]
                ),
            },
            "retry_context": [
                {
                    "run_id": item.run_id,
                    "tool_name": item.tool_name,
                    "safe_error_code": item.safe_error_code,
                }
                for item in state.retry_context
            ],
            "tool_calls": tool_calls,
            "artifacts": artifacts,
            "compression_stop_reason": (
                None
                if compression_stop_reason is None
                else {
                    "code": compression_stop_reason.value,
                    "text": CONTEXT_STOP_REASON_TEXT[compression_stop_reason],
                }
            ),
        }


__all__ = [
    "CONTEXT_SAFETY_COMMIT_MAX_ATTEMPTS",
    "CONTEXT_SAFETY_STOP_ERROR_CODE",
    "CONTEXT_STOP_REASON_TEXT",
    "SafeStopReporter",
    "SafeStopState",
    "system_context_report",
]
