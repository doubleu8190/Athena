"""主 Run 派发子智能体批次的 PostgreSQL 组合式 Store。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.llm.model import ModelProfileVersionModel
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.converter import run_to_domain
from adapter.run.model import RunModel, RunToolCallModel
from adapter.run.repository._approval_helpers import has_unsettled_tool_calls
from adapter.run.repository._tx_helpers import (
    append_event,
    build_model_snapshot_json,
    clear_lease,
    database_now,
    lease_is_valid,
    lock_worker_run,
    sync_llm_request_count,
    validate_and_merge_input_file_ids,
    validate_usable_profile,
    work_deadline_is_valid,
)
from adapter.session.model import SessionModel
from adapter.subagent.model import SubagentModel
from domain.message import ExecutionRole, SessionNotFoundError
from domain.run.model import (
    BeginSubagentBatchCommand,
    BeginSubagentBatchResult,
    RunQueuedPayload,
    RunStatus,
    RunUsage,
    RunWaitingForSubagentsPayload,
)
from domain.run.model.errors import RunStateError
from domain.skill import RunSkillSnapshot
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID
from domain.tool import ToolCallStatus, ToolKind


class RunSubagentStore:
    """负责主 Run 派发子 Run 批次计数与原子落库。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        message_repository: SqlAlchemyMessageRepository,
    ) -> None:
        """保存数据库会话工厂与同事务消息追加端口。"""
        self._session_factory = session_factory
        self._message_repo = message_repository

    async def count_subagent_dispatches(self, root_run_id: str) -> int:
        """统计同一主 Run 累计已派发的子 Run 数量。"""
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(RunModel.id)).where(
                    RunModel.root_run_id == root_run_id,
                    RunModel.depth >= 1,
                )
            )
            return int(count or 0)

    async def begin_subagent_batch(
        self, command: BeginSubagentBatchCommand
    ) -> BeginSubagentBatchResult:
        """原子校验并创建子 Run 批次，同时将主 Run 置为 waiting_for_subagents 并释放租约。"""
        if not 1 <= len(command.items) <= 5:
            raise RunStateError("subagent_batch_size_invalid")
        async with self._session_factory() as session:
            async with session.begin():
                row = await lock_worker_run(
                    session, command.run_id, command.worker_id, command.execution_epoch
                )
                locked_session = await session.get(SessionModel, row.session_id)
                if locked_session is None or locked_session.deleted_time is not None:
                    raise SessionNotFoundError("session_not_found")
                db_now = await session.scalar(select(func.clock_timestamp()))
                if not lease_is_valid(
                    row, command.worker_id, command.execution_epoch, db_now
                ) or not work_deadline_is_valid(row, db_now):
                    raise RunStateError("run_execution_lease_lost")
                if row.status != RunStatus.RUNNING.value or row.cancel_requested_at is not None:
                    raise RunStateError("run_execution_not_active")
                if row.depth != 0:
                    raise RunStateError("subagent_nested_dispatch_forbidden")
                if await has_unsettled_tool_calls(session, row.id):
                    raise RunStateError("run_tool_call_unsettled")

                root_run_id = row.root_run_id or row.id
                existing_count = int(
                    await session.scalar(
                        select(func.count(RunModel.id)).where(
                            RunModel.root_run_id == root_run_id,
                            RunModel.depth >= 1,
                        )
                    )
                    or 0
                )
                if existing_count + len(command.items) > 15:
                    raise RunStateError("subagent_dispatch_limit_exceeded")

                if (
                    command.call_message.session_id != row.session_id
                    or command.call_message.owner_id != row.owner_id
                    or command.call_message.run_id != row.id
                    or command.call_message.role is not ExecutionRole.ASSISTANT
                ):
                    raise RunStateError("run_tool_call_message_invalid")

                parent_skill_snapshot = (
                    None
                    if row.skill_snapshot is None
                    else RunSkillSnapshot.model_validate(row.skill_snapshot)
                )
                # 校验子任务 Subagent、模型可用性与输入文件归属，并固化子 Run 模型快照。
                validated_items: list[
                    tuple[
                        str,
                        dict[str, object],
                        tuple[str, ...],
                        RunSkillSnapshot | None,
                    ]
                ] = []
                for item in command.items:
                    if (
                        item.task_message.session_id != row.session_id
                        or item.task_message.owner_id != row.owner_id
                        or item.task_message.run_id != item.sub_run_id
                        or item.task_message.thread_id != item.sub_run_id
                        or item.task_message.role is not ExecutionRole.USER
                    ):
                        # 校验子任务草稿归属对应子 Run 及独立 thread，防污染父或兄弟 Run 上下文。
                        raise RunStateError("subagent_task_message_invalid")
                    subagent_row = await session.scalar(
                        select(SubagentModel).where(
                            SubagentModel.id == item.subagent_profile_id,
                            SubagentModel.slug == item.subagent_slug,
                            SubagentModel.deleted_at.is_(None),
                            SubagentModel.enabled.is_(True),
                        )
                    )
                    if subagent_row is None:
                        raise RunStateError("subagent_profile_unavailable")
                    if DISPATCH_SUBAGENTS_TOOL_ID in {t.tool_id for t in item.tool_snapshot.tools}:
                        raise RunStateError("subagent_nested_dispatch_forbidden")
                    model_profile_id = subagent_row.model_profile_id or row.model_config_version_id
                    profile_row = validate_usable_profile(
                        await session.get(ModelProfileVersionModel, model_profile_id),
                        error=RunStateError("model_profile_unavailable"),
                    )
                    validated_files = await validate_and_merge_input_file_ids(
                        session,
                        row.session_id,
                        row.owner_id,
                        existing=(),
                        incoming=item.input_file_ids,
                    )
                    if item.skill_snapshot is None:
                        child_skill_snapshot = None
                    elif parent_skill_snapshot is None:
                        raise RunStateError("subagent_skill_snapshot_invalid")
                    else:
                        try:
                            # 子 Run 只继承父 Run 已冻结条目；请求中的名称、版本和审批
                            # 字段不可作为授权依据，必须由父快照重建。
                            child_skill_snapshot = parent_skill_snapshot.trusted_subset(
                                item.skill_snapshot
                            )
                        except ValueError as error:
                            raise RunStateError("subagent_skill_snapshot_invalid") from error
                    validated_items.append(
                        (
                            model_profile_id,
                            build_model_snapshot_json(profile_row),
                            validated_files,
                            child_skill_snapshot,
                        )
                    )

                # 父 Run 行锁已覆盖本查询，派发批次必须续接同一 Run 的全局工具序号。
                max_tool_ordinal = await session.scalar(
                    select(func.max(RunToolCallModel.ordinal)).where(
                        RunToolCallModel.run_id == row.id
                    )
                )
                dispatch_ordinal = int(max_tool_ordinal or 0) + 1
                persisted_call_msg = await self._message_repo.append_execution_in_session(
                    session,
                    command.call_message,
                    locked_session=locked_session,
                )
                session.add(
                    RunToolCallModel(
                        id=command.record_id,
                        run_id=row.id,
                        session_id=row.session_id,
                        tool_call_id=command.tool_call_id,
                        ordinal=dispatch_ordinal,
                        tool_id=DISPATCH_SUBAGENTS_TOOL_ID,
                        tool_kind=ToolKind.BUILTIN.value,
                        model_name=DISPATCH_SUBAGENTS_TOOL_ID,
                        execution_epoch=command.execution_epoch,
                        round=command.round,
                        status=ToolCallStatus.EXECUTING.value,
                        call_message_id=persisted_call_msg.id,
                        requires_approval=False,
                        started_at=command.requested_at,
                    )
                )

                child_rows: list[RunModel] = []
                for idx, item in enumerate(command.items):
                    (
                        model_profile_id,
                        child_model_snapshot,
                        validated_files,
                        child_skill_snapshot,
                    ) = validated_items[idx]
                    child_created_at = await database_now(session)
                    child = RunModel(
                        id=item.sub_run_id,
                        session_id=row.session_id,
                        owner_id=row.owner_id,
                        status=RunStatus.QUEUED.value,
                        trigger_message_id=row.trigger_message_id,
                        context_cutoff_sequence=row.context_cutoff_sequence,
                        retry_of_run_id=None,
                        model_config_version_id=model_profile_id,
                        model_snapshot_json=child_model_snapshot,
                        main_prompt_version_id=row.main_prompt_version_id,
                        compression_prompt_version_id=row.compression_prompt_version_id,
                        # 子 Run 上下文隔离且无正式会话历史，不继承主 Run 压缩策略。
                        compression_policy_version_id=None,
                        skill_snapshot=(
                            None
                            if child_skill_snapshot is None
                            else child_skill_snapshot.model_dump(mode="json")
                        ),
                        input_file_ids=list(validated_files),
                        # 子 Run 继承主 Run 显式或默认的知识库限定范围，确保执行期不越界。
                        knowledge_base_ids=(
                            None if row.knowledge_base_ids is None else list(row.knowledge_base_ids)
                        ),
                        tool_snapshot=item.tool_snapshot.model_dump(mode="json"),
                        auth_session_id=row.auth_session_id,
                        llm_request_count=0,
                        prompt_template_version=row.prompt_template_version,
                        event_sequence=0,
                        execution_epoch=0,
                        queued_at=command.requested_at,
                        created_at=child_created_at,
                        updated_at=max(command.requested_at, child_created_at),
                        usage=RunUsage().model_dump(mode="json"),
                        parent_run_id=row.id,
                        root_run_id=root_run_id,
                        parent_tool_call_id=command.tool_call_id,
                        depth=1,
                        subagent_profile_id=item.subagent_profile_id,
                        subagent_slug=item.subagent_slug,
                        subagent_name=item.subagent_name,
                        subagent_system_prompt=item.system_prompt,
                        max_llm_requests=item.max_llm_requests,
                        timeout_seconds=item.timeout_seconds,
                    )
                    session.add(child)
                    await session.flush()
                    await self._message_repo.append_execution_in_session(
                        session,
                        item.task_message,
                        locked_session=locked_session,
                    )
                    await append_event(
                        session,
                        child,
                        RunQueuedPayload(trigger_message_id=row.trigger_message_id),
                        command.requested_at,
                    )
                    child_rows.append(child)

                # 主 Run 转入 waiting_for_subagents 并清空 worker_id 与租约，退出本轮 Executor。
                row.status = RunStatus.WAITING_FOR_SUBAGENTS.value
                sync_llm_request_count(row, command.llm_request_count)
                clear_lease(row)
                row.updated_at = command.requested_at
                await append_event(
                    session,
                    row,
                    RunWaitingForSubagentsPayload(
                        tool_call_id=command.tool_call_id,
                        subagent_run_ids=tuple(item.sub_run_id for item in command.items),
                    ),
                    command.requested_at,
                )
                return BeginSubagentBatchResult(
                    run=run_to_domain(row),
                    sub_runs=tuple(run_to_domain(child) for child in child_rows),
                )
