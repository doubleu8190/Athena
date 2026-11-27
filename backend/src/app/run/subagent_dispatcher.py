"""主 Run 派发子智能体批次的参数、权限与快照校验协作者。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from app.access import AccessPolicyService
from app.tool.ports import RunToolRuntime
from domain.access import ResourceType
from domain.llm.model import LlmError, LlmResponse, LlmToolCall
from domain.message import (
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    TextPart,
    ToolCallPart,
)
from domain.run.model import (
    DISPATCH_SUBAGENTS_TOOL_ID,
    BeginSubagentBatchCommand,
    BeginSubagentBatchItem,
    Run,
)
from domain.run.model.errors import RunConflictError, RunInputResourceError, RunStateError
from domain.run.ports import RunRepository
from domain.tool import AuthorizedRunContext, RuntimeToolSnapshot

if TYPE_CHECKING:
    from adapter.subagent.repository import SqlAlchemySubagentRepository


class SubagentDispatcher:
    """校验 dispatch_subagents 参数与实时权限并原子派发子 Run 批次。"""

    def __init__(
        self,
        *,
        repository: RunRepository,
        subagents: SqlAlchemySubagentRepository | None,
        tool_runtime: RunToolRuntime | None,
        access_policy: AccessPolicyService | None,
        id_factory: Callable[[str], str],
        now: Callable[[], datetime],
    ) -> None:
        """保存子智能体派发所需的仓储、工具运行时、权限策略与时钟。"""
        self._repository = repository
        self._subagents = subagents
        self._tool_runtime = tool_runtime
        self._access_policy = access_policy
        self._id_factory = id_factory
        self._now = now

    async def dispatch(
        self,
        *,
        run: Run,
        authorized: AuthorizedRunContext | None,
        snapshot: RuntimeToolSnapshot,
        round_number: int,
        llm_request_count: int | Callable[[], int],
        call: LlmToolCall,
        explanation: str,
        response: LlmResponse,
        authorize: Callable[[Run], Awaitable[AuthorizedRunContext | None]],
        publish_events: Callable[[], Awaitable[None]],
    ) -> None:
        """校验 dispatch_subagents 参数并原子派发子 Run 批次，随后释放主 Run 租约。"""
        if run.depth != 0:
            raise LlmError("subagent_nested_dispatch_forbidden")
        if self._tool_runtime is not None and await authorize(run) is None:
            raise LlmError("run_auth_session_invalid")
        if self._subagents is None:
            raise LlmError("subagent_runtime_unavailable")
        definition = snapshot.by_model_name(call.name)
        if definition is None or definition.tool_id != DISPATCH_SUBAGENTS_TOOL_ID:
            raise LlmError("tool_unknown")
        if self._tool_runtime is not None:
            validation = self._tool_runtime.validate_call(
                definition, call.name, dict(call.arguments)
            )
            if validation is not None:
                raise LlmError(validation.code)

        raw_tasks = call.arguments.get("tasks")
        if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 5:
            raise LlmError("subagent_batch_size_invalid")

        created_at = self._now()
        batch_items: list[BeginSubagentBatchItem] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                raise LlmError("tool_arguments_invalid")
            slug = raw_task.get("subagent_slug")
            task_text = raw_task.get("task")
            context_text = raw_task.get("context")
            raw_files = raw_task.get("input_file_ids", [])
            if (
                not isinstance(slug, str)
                or not slug.strip()
                or not isinstance(task_text, str)
                or not task_text.strip()
            ):
                raise LlmError("tool_arguments_invalid")
            if context_text is not None and not isinstance(context_text, str):
                raise LlmError("tool_arguments_invalid")
            if (
                not isinstance(raw_files, list)
                or any(not isinstance(item, str) or not item for item in raw_files)
                or len(raw_files) != len(set(raw_files))
            ):
                raise LlmError("run_input_file_ids_invalid")

            profile = await self._subagents.get_by_slug(slug.strip())
            if profile is None or not profile.enabled:
                raise LlmError("subagent_profile_unavailable")

            # 调度执行前实时复核当前员工是否对目标子智能体拥有读权限，
            # 防止模型强行拼出他部门 subagent slug 或员工调岗后越权触发子 Run。
            if self._access_policy is not None:
                if not await self._access_policy.can_read_resource(
                    run.owner_id,
                    ResourceType.SUBAGENT,
                    profile.id,
                ):
                    raise LlmError("subagent_profile_unavailable")
            elif self._tool_runtime is not None:
                if not await self._tool_runtime.can_read_subagent(
                    run.owner_id,
                    profile.id,
                ):
                    raise LlmError("subagent_profile_unavailable")

            if self._tool_runtime is not None:
                if authorized is None:
                    raise LlmError("run_auth_session_invalid")
                sub_snapshot = await self._tool_runtime.build_snapshot(
                    run.prompt_template_version,
                    authorized,
                    depth=1,
                    allowed_tool_ids=profile.allowed_tool_ids,
                    knowledge_base_ids=run.knowledge_base_ids,
                )
            else:
                sub_snapshot = RuntimeToolSnapshot(tools=())

            sub_run_id = self._id_factory("run")
            formatted_task = (
                f"任务：\n{task_text.strip()}\n\n补充上下文：\n{context_text.strip()}"
                if isinstance(context_text, str) and context_text.strip()
                else task_text.strip()
            )
            task_message = ExecutionMessageDraft(
                id=self._id_factory("exec"),
                session_id=run.session_id,
                owner_id=run.owner_id,
                run_id=sub_run_id,
                thread_id=sub_run_id,
                role=ExecutionRole.USER,
                parts=(TextPart(formatted_task),),
                tool_call_id=None,
                source_message_id=None,
                created_at=created_at,
                metadata=MessageMetadata(),
            )
            batch_items.append(
                BeginSubagentBatchItem(
                    sub_run_id=sub_run_id,
                    subagent_profile_id=profile.id,
                    subagent_slug=profile.slug,
                    subagent_name=profile.name,
                    system_prompt=profile.system_prompt,
                    model_profile_id=profile.model_profile_id or run.model_config_version_id,
                    tool_snapshot=sub_snapshot,
                    skill_snapshot=run.skill_snapshot,
                    input_file_ids=tuple(raw_files),
                    task_message=task_message,
                    max_llm_requests=profile.max_llm_requests,
                    timeout_seconds=profile.timeout_seconds,
                )
            )

        call_message = ExecutionMessageDraft(
            id=self._id_factory("exec"),
            session_id=run.session_id,
            owner_id=run.owner_id,
            run_id=run.id,
            thread_id=run.id,
            role=ExecutionRole.ASSISTANT,
            parts=(
                *((TextPart(explanation),) if explanation.strip() else ()),
                ToolCallPart(call.call_id, call.name, dict(call.arguments)),
            ),
            tool_call_id=None,
            source_message_id=None,
            created_at=created_at,
            metadata=MessageMetadata(provider="chat_completions", model=response.model),
        )
        try:
            await self._repository.begin_subagent_batch(
                BeginSubagentBatchCommand(
                    run_id=run.id,
                    worker_id=run.worker_id or "",
                    execution_epoch=run.execution_epoch,
                    tool_call_id=call.call_id,
                    call_message=call_message,
                    record_id=self._id_factory("toolcall"),
                    items=tuple(batch_items),
                    requested_at=created_at,
                    round=round_number,
                    llm_request_count=(
                        llm_request_count() if callable(llm_request_count) else llm_request_count
                    ),
                )
            )
        except (RunStateError, RunInputResourceError, RunConflictError) as error:
            raise LlmError(str(error)) from error
        await publish_events()
