"""以一个 PostgreSQL 事务接受首版 Plan 并冻结首轮任务。"""

from __future__ import annotations

import hashlib
import json
from math import isfinite
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
    CommandReceipt,
    NoReadyTasksError,
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    PlanDAG,
    PlanEdge,
    PlanRound,
    PlanRevision,
    PlanStatus,
    PlanValidator,
    PlanVersion,
    ReviewAction,
    ReviewDecision,
    RoundStatus,
    Task,
    TaskAttempt,
    ExecutionStatus,
    TaskStatus,
    freeze_ready_round,
    OrchestrationCommandRepository,
    PlanCommandRejected,
)
from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    OrchestrationCommandModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
    WorkerJobModel,
)


class OrchestrationCommandConflict(ValueError):
    """相同命令幂等键被不同内容复用。"""


class PostgresOrchestrationCommandRepository(OrchestrationCommandRepository):
    """在事务中保存 Plan、版本、首轮、执行作业和可靠事件。"""

    def __init__(
        self,
        session_factory: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ],
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """保存数据库会话工厂和可替换的时间、ID 来源。

        Args:
            session_factory: 提供异步 SQLAlchemy 会话的工厂。
            id_factory: 生成消息、轮次、执行和作业 ID 的函数。
            clock: 返回带时区当前时间的函数。
        """
        self._session_factory = session_factory
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def submit_plan(
        self,
        command: OrchestrationCommand,
        plan: Plan,
        tasks: list[Task],
    ) -> CommandReceipt:
        """原子创建 Plan Version 1、首个 Round 和对应 Worker Jobs。

        Args:
            command: SubmitPlan 命令，correlation_id 指向 Run。
            plan: 经应用层验证且绑定 run_id/session_id 的 Plan。
            tasks: Plan 中全部 Task 定义。

        Returns:
            固定回执；相同幂等键和内容的重试会返回原回执。

        Raises:
            ValueError: 命令类型、Plan 版本或关联 ID 不合法。
            OrchestrationCommandConflict: 幂等键已用于其他请求内容。
            PlanCommandRejected: Run 不存在、已停止或已有 Plan。
        """
        if command.command_type != OrchestrationCommandType.SUBMIT_PLAN:
            raise ValueError("submit_plan requires a SubmitPlan command")
        if plan.run_id != command.correlation_id or plan.plan_id != command.plan_id:
            raise ValueError("command and plan identities do not match")
        if command.scope_key != command.correlation_id:
            raise ValueError("SubmitPlan scope_key must be the target run_id")
        if command.plan_version not in (None, 1):
            raise ValueError("SubmitPlan must create version 1")
        if not plan.session_id:
            raise ValueError("plan.session_id is required")

        now = self._clock()
        command_hash = _plan_hash(command, plan, tasks)
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(
                    AgentRunModel, command.correlation_id, with_for_update=True
                )
                if run is None:
                    raise PlanCommandRejected("run_not_found")

                prior = (
                    await session.execute(
                        select(OrchestrationCommandModel)
                        .where(
                            OrchestrationCommandModel.scope_key == command.scope_key,
                            OrchestrationCommandModel.idempotency_key
                            == command.idempotency_key,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    if prior.payload_hash != command_hash:
                        raise OrchestrationCommandConflict(
                            "idempotency_key_reused_with_different_payload"
                        )
                    return _receipt(prior.result_json, deduplicated=True)

                if run.status not in {"created", "running"}:
                    raise PlanCommandRejected("run_not_active")
                if run.session_id != plan.session_id:
                    raise PlanCommandRejected("run_session_mismatch")
                existing = (
                    await session.execute(
                        select(AgentPlanModel.plan_id).where(
                            AgentPlanModel.run_id == plan.run_id
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    raise PlanCommandRejected("plan_already_exists")

                initial_states = {task.task_id: TaskStatus.PENDING.value for task in tasks}
                plan_document = _plan_document(plan, tasks, version=1)
                plan_version = PlanVersion(
                    plan_id=plan.plan_id,
                    version=1,
                    dag=plan_document,
                    created_at=now,
                )
                round_id = self._id_factory()
                round_snapshot = freeze_ready_round(
                    plan,
                    plan_version=plan_version.version,
                    round_number=1,
                    round_id=round_id,
                    task_states=initial_states,
                    created_at=now,
                )
                receipt = CommandReceipt(
                    message_id=command.message_id,
                    plan_id=plan.plan_id,
                    plan_version=plan_version.version,
                    round_id=round_snapshot.round_id,
                    task_ids=round_snapshot.task_ids,
                )
                plan_row = AgentPlanModel(
                    plan_id=plan.plan_id,
                    run_id=plan.run_id,
                    session_id=plan.session_id,
                    status=PlanStatus.RUNNING.value,
                    user_goal=plan.goal,
                    max_parallelism=plan.max_parallelism,
                    current_version=1,
                    active_round_id=round_snapshot.round_id,
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
                session.add(plan_row)
                # Bare ORM instances are not linked by relationships, so make
                # the referenced Plan visible before inserting PlanVersion.
                await session.flush()
                session.add(
                    PlanVersionModel(
                        plan_id=plan.plan_id,
                        version=1,
                        dag_json=plan_document,
                        change_reason=None,
                        based_on_round_id=None,
                        created_at=now,
                    )
                )
                # Flush the composite FK target before the first Round row is
                # queued; bare ORM objects here have no relationship mapping
                # for the unit of work to order reliably.
                await session.flush()
                task_rows = {
                    task.task_id: AgentTaskModel(
                        task_id=task.task_id,
                        plan_id=plan.plan_id,
                        run_id=plan.run_id,
                        title=task.title,
                        objective=task.objective,
                        input_json=json.dumps(
                            dict(task.input),
                            ensure_ascii=False,
                            default=_json_default,
                        ),
                        expected_output_json=json.dumps(
                            dict(task.expected_output),
                            ensure_ascii=False,
                            default=_json_default,
                        ),
                        allowed_tools_json=json.dumps(
                            list(task.allowed_tools), ensure_ascii=False
                        ),
                        worker_type=task.worker_type,
                        timeout_seconds=task.timeout_seconds,
                        status="pending",
                        execution_generation=1,
                        worker_thread_id=f"task:{task.task_id}:exec:1",
                        llm_turn_count=0,
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                    )
                    for task in tasks
                }
                session.add_all(list(task_rows.values()))
                session.add(
                    PlanRoundModel(
                        round_id=round_snapshot.round_id,
                        plan_id=plan.plan_id,
                        plan_version=round_snapshot.plan_version,
                        round_number=round_snapshot.round_number,
                        status=RoundStatus.RUNNING.value,
                        expected_task_count=len(round_snapshot.task_ids),
                        terminal_task_count=0,
                        created_at=now,
                    )
                )
                await session.flush()

                execution_rows: list[TaskExecutionModel] = []
                job_rows: list[WorkerJobModel] = []
                for task_id in round_snapshot.task_ids:
                    execution_id = self._id_factory()
                    worker_thread_id = f"task:{task_id}:exec:1"
                    attempt = TaskAttempt(
                        execution_id=execution_id,
                        task_id=task_id,
                        round_id=round_snapshot.round_id,
                        attempt=1,
                        worker_thread_id=worker_thread_id,
                        fencing_token=1,
                        status=ExecutionStatus.QUEUED,
                    )
                    session.add(
                        RoundTaskModel(
                            round_id=round_snapshot.round_id,
                            task_id=task_id,
                            status="pending",
                            active_execution_id=attempt.execution_id,
                        )
                    )
                    task_row = task_rows[task_id]
                    task_row.active_execution_id = attempt.execution_id
                    execution_rows.append(
                        TaskExecutionModel(
                            execution_id=attempt.execution_id,
                            round_id=attempt.round_id,
                            task_id=attempt.task_id,
                            attempt=attempt.attempt,
                            worker_thread_id=attempt.worker_thread_id,
                            status=attempt.status.value,
                            fencing_token=attempt.fencing_token,
                            cancellation_epoch=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    job_rows.append(
                        WorkerJobModel(
                            job_id=self._id_factory(),
                            dedupe_key=f"execute:{attempt.execution_id}",
                            job_type="execute",
                            execution_id=attempt.execution_id,
                            payload_json={
                                "plan_id": plan.plan_id,
                                "round_id": round_snapshot.round_id,
                                "task_id": task_id,
                            },
                            status="pending",
                            attempt=0,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                session.add_all(execution_rows + job_rows)
                run.status = "running"
                run.updated_at = now.isoformat()
                session.add(
                    OrchestrationCommandModel(
                        message_id=command.message_id,
                        scope_key=command.scope_key,
                        idempotency_key=command.idempotency_key,
                        payload_hash=command_hash,
                        command_type=command.command_type.value,
                        plan_id=plan.plan_id,
                        plan_version=1,
                        round_id=round_snapshot.round_id,
                        status="accepted",
                        result_json=_receipt_document(receipt),
                        created_at=now,
                    )
                )
                self._write_outbox(
                    session,
                    command,
                    event_type="PlanAccepted",
                    dedupe_key=f"plan:{plan.plan_id}:accepted",
                    round_snapshot=round_snapshot,
                    payload={"task_ids": list(plan.task_ids)},
                    now=now,
                )
                self._write_outbox(
                    session,
                    command,
                    event_type="RoundStarted",
                    dedupe_key=f"round:{round_snapshot.round_id}:started",
                    round_snapshot=round_snapshot,
                    payload={"task_ids": list(round_snapshot.task_ids)},
                    now=now,
                )
                return receipt

    async def apply_review_decision(
        self,
        command: OrchestrationCommand,
        decision: ReviewDecision,
    ) -> CommandReceipt:
        """Atomically apply a round-scoped Root decision and schedule its next round."""
        expected_type = {
            ReviewAction.CONTINUE: OrchestrationCommandType.CONTINUE_PLAN,
            ReviewAction.REPLAN: OrchestrationCommandType.REVISE_PLAN,
            ReviewAction.FINISH: OrchestrationCommandType.FINISH_PLAN,
        }[decision.action]
        if command.command_type != expected_type:
            raise PlanCommandRejected("review_action_command_type_mismatch")
        if (
            command.scope_key != command.correlation_id
            or not command.round_id
            or command.plan_version is None
            or isinstance(command.plan_version, bool)
            or command.plan_version < 1
        ):
            raise PlanCommandRejected("review_command_identity_invalid")
        if (
            decision.action == ReviewAction.REPLAN
            and decision.revision is not None
            and decision.revision.base_version != command.plan_version
        ):
            raise PlanCommandRejected("review_revision_version_mismatch")

        command_hash = _review_command_hash(command, decision)
        now = self._clock()
        async with self._session_factory() as session:
            async with session.begin():
                run = await session.get(
                    AgentRunModel, command.correlation_id, with_for_update=True
                )
                if run is None:
                    raise PlanCommandRejected("run_not_found")

                prior = (
                    await session.execute(
                        select(OrchestrationCommandModel)
                        .where(
                            OrchestrationCommandModel.scope_key == command.scope_key,
                            OrchestrationCommandModel.idempotency_key
                            == command.idempotency_key,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if prior is not None:
                    if prior.payload_hash != command_hash:
                        raise OrchestrationCommandConflict(
                            "idempotency_key_reused_with_different_payload"
                        )
                    return _receipt(prior.result_json, deduplicated=True)
                if run.status not in {"created", "running"}:
                    raise PlanCommandRejected("run_not_active")

                plan_row = await session.get(
                    AgentPlanModel, command.plan_id, with_for_update=True
                )
                if plan_row is None:
                    raise PlanCommandRejected("plan_not_found")
                if plan_row.run_id != command.correlation_id:
                    raise PlanCommandRejected("review_run_mismatch")
                if (
                    plan_row.status != PlanStatus.WAITING_REVIEW.value
                    or plan_row.current_version != command.plan_version
                    or plan_row.active_round_id != command.round_id
                ):
                    raise PlanCommandRejected("review_round_identity_stale")

                round_row = await session.get(
                    PlanRoundModel, command.round_id, with_for_update=True
                )
                if (
                    round_row is None
                    or round_row.plan_id != plan_row.plan_id
                    or round_row.plan_version != command.plan_version
                    or round_row.status != RoundStatus.COMPLETED.value
                    or round_row.expected_task_count != round_row.terminal_task_count
                ):
                    raise PlanCommandRejected("review_round_not_complete")
                version_row = await session.get(
                    PlanVersionModel,
                    (plan_row.plan_id, command.plan_version),
                    with_for_update=True,
                )
                if version_row is None or not isinstance(
                    version_row.dag_json, Mapping
                ):
                    raise PlanCommandRejected("review_plan_version_invalid")
                task_rows = (
                    await session.execute(
                        select(AgentTaskModel)
                        .where(AgentTaskModel.plan_id == plan_row.plan_id)
                        .with_for_update()
                    )
                ).scalars().all()
                task_by_id = {row.task_id: row for row in task_rows}

                if decision.action == ReviewAction.FINISH:
                    assert decision.final_status is not None
                    plan_status = PlanStatus(decision.final_status)
                    for task_row in task_rows:
                        if task_row.status == TaskStatus.PENDING.value:
                            task_row.status = TaskStatus.CANCELLED.value
                            task_row.error_json = json.dumps(
                                {"reason": "plan_finished"}, ensure_ascii=False
                            )
                            task_row.updated_at = now.isoformat()
                    plan_row.status = plan_status.value
                    plan_row.updated_at = now.isoformat()
                    receipt = CommandReceipt(
                        message_id=command.message_id,
                        plan_id=plan_row.plan_id,
                        plan_version=command.plan_version,
                        round_id=round_row.round_id,
                        task_ids=(),
                    )
                    self._write_review_terminal_outbox(
                        session,
                        command,
                        decision,
                        plan_row,
                        round_row,
                        plan_status,
                        now,
                    )
                else:
                    current_document = dict(version_row.dag_json)
                    current_tasks = _tasks_from_document(
                        current_document, task_by_id
                    )
                    current_plan = _plan_from_document(
                        plan_row, current_document, current_tasks
                    )
                    current_legal_tools = {
                        tool for task in current_tasks for tool in task.allowed_tools
                    }
                    PlanValidator(legal_tools=current_legal_tools).validate(
                        current_plan, current_tasks
                    )
                    if decision.action == ReviewAction.REPLAN:
                        assert decision.revision is not None
                        next_document, next_tasks, newly_added = _revise_plan_document(
                            current_document,
                            current_tasks,
                            task_by_id,
                            decision.revision,
                            plan_row=plan_row,
                            run_id=run.run_id,
                        )
                        next_plan = _plan_from_document(
                            plan_row, next_document, next_tasks
                        )
                        legal_tools = {
                            tool for task in next_tasks for tool in task.allowed_tools
                        }
                        PlanValidator(legal_tools=legal_tools).validate(
                            next_plan, next_tasks
                        )
                        for task_id in decision.revision.cancel_task_ids:
                            row = task_by_id[task_id]
                            if row.status == TaskStatus.PENDING.value:
                                row.status = TaskStatus.CANCELLED.value
                                row.error_json = json.dumps(
                                    {"reason": "removed_by_root_revision"},
                                    ensure_ascii=False,
                                )
                                row.updated_at = now.isoformat()
                        for task in newly_added:
                            task_row = AgentTaskModel(
                                task_id=task.task_id,
                                plan_id=plan_row.plan_id,
                                run_id=run.run_id,
                                title=task.title,
                                objective=task.objective,
                                input_json=json.dumps(
                                    dict(task.input),
                                    ensure_ascii=False,
                                    default=_json_default,
                                ),
                                expected_output_json=json.dumps(
                                    dict(task.expected_output),
                                    ensure_ascii=False,
                                    default=_json_default,
                                ),
                                allowed_tools_json=json.dumps(
                                    list(task.allowed_tools), ensure_ascii=False
                                ),
                                worker_type=task.worker_type,
                                timeout_seconds=task.timeout_seconds,
                                status=TaskStatus.PENDING.value,
                                execution_generation=0,
                                created_at=now.isoformat(),
                                updated_at=now.isoformat(),
                            )
                            task_by_id[task.task_id] = task_row
                            session.add(task_row)
                        next_version = command.plan_version + 1
                        next_document["plan_id"] = plan_row.plan_id
                        next_document["version"] = next_version
                        version_row = PlanVersionModel(
                            plan_id=plan_row.plan_id,
                            version=next_version,
                            dag_json=next_document,
                            change_reason=decision.reason,
                            based_on_round_id=round_row.round_id,
                            created_at=now,
                        )
                        session.add(version_row)
                        plan_row.current_version = next_version
                        current_plan = next_plan
                        # The ORM mappings use scalar foreign-key columns
                        # without relationships, so insertion order is not
                        # inferred for the composite PlanVersion -> Round FK.
                        # Persist the new immutable version and tasks before
                        # the next Round schedules executions against them.
                        await session.flush()
                    elif decision.action != ReviewAction.CONTINUE:
                        raise PlanCommandRejected("review_action_unsupported")

                    task_ids = set(current_plan.task_ids)
                    states = {
                        task_id: task_by_id[task_id].status for task_id in task_ids
                    }
                    dag = PlanDAG(current_plan)
                    cancelled = dag.propagate_dependency_failures(states)
                    for task_id in cancelled:
                        row = task_by_id[task_id]
                        row.status = TaskStatus.CANCELLED.value
                        row.error_json = json.dumps(
                            {"reason": "dependency_failed"}, ensure_ascii=False
                        )
                        row.updated_at = now.isoformat()
                    next_round_id = self._id_factory()
                    try:
                        next_round = freeze_ready_round(
                            current_plan,
                            plan_version=plan_row.current_version,
                            round_number=round_row.round_number + 1,
                            round_id=next_round_id,
                            task_states=states,
                            created_at=now,
                        )
                    except NoReadyTasksError as exc:
                        error_code = (
                            "BLOCKED_DEPENDENCIES"
                            if cancelled
                            else "NO_REMAINING_TASKS"
                        )
                        raise PlanCommandRejected(error_code) from exc
                    self._schedule_review_round(
                        session,
                        command,
                        decision,
                        plan_row,
                        next_round,
                        current_plan,
                        task_by_id,
                        now,
                    )
                    receipt = CommandReceipt(
                        message_id=command.message_id,
                        plan_id=plan_row.plan_id,
                        plan_version=next_round.plan_version,
                        round_id=next_round.round_id,
                        task_ids=next_round.task_ids,
                    )

                session.add(
                    OrchestrationCommandModel(
                        message_id=command.message_id,
                        scope_key=command.scope_key,
                        idempotency_key=command.idempotency_key,
                        payload_hash=command_hash,
                        command_type=command.command_type.value,
                        plan_id=command.plan_id,
                        plan_version=command.plan_version,
                        round_id=command.round_id,
                        status="accepted",
                        result_json={
                            **_receipt_document(receipt),
                            "review_decision": _review_decision_document(decision),
                        },
                        created_at=now,
                    )
                )
                return receipt

    async def load_review_decision(
        self,
        *,
        scope_key: str,
        idempotency_key: str,
        plan_id: str,
        plan_version: int,
        round_id: str,
    ) -> ReviewDecision | None:
        """Load the stored decision so checkpoint retries never call Root twice."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(OrchestrationCommandModel).where(
                        OrchestrationCommandModel.scope_key == scope_key,
                        OrchestrationCommandModel.idempotency_key
                        == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        if (
            row.plan_id != plan_id
            or row.plan_version != plan_version
            or row.round_id != round_id
            or row.status != "accepted"
        ):
            raise OrchestrationCommandConflict("review_idempotency_identity_conflict")
        result = row.result_json
        if not isinstance(result, Mapping) or not isinstance(
            result.get("review_decision"), Mapping
        ):
            raise RuntimeError("stored_review_decision_missing")
        decision = _review_decision_from_document(result["review_decision"])
        expected_type = {
            ReviewAction.CONTINUE: OrchestrationCommandType.CONTINUE_PLAN.value,
            ReviewAction.REPLAN: OrchestrationCommandType.REVISE_PLAN.value,
            ReviewAction.FINISH: OrchestrationCommandType.FINISH_PLAN.value,
        }[decision.action]
        if row.command_type != expected_type:
            raise OrchestrationCommandConflict("stored_review_decision_type_conflict")
        return decision

    def _schedule_review_round(
        self,
        session: AsyncSession,
        command: OrchestrationCommand,
        decision: ReviewDecision,
        plan_row: AgentPlanModel,
        round_snapshot: PlanRound,
        plan: Plan,
        task_by_id: dict[str, AgentTaskModel],
        now: datetime,
    ) -> None:
        """Persist an immutable Round membership and its queued executions."""
        plan_row.status = PlanStatus.RUNNING.value
        plan_row.active_round_id = round_snapshot.round_id
        plan_row.updated_at = now.isoformat()
        session.add(
            PlanRoundModel(
                round_id=round_snapshot.round_id,
                plan_id=plan_row.plan_id,
                plan_version=round_snapshot.plan_version,
                round_number=round_snapshot.round_number,
                status=RoundStatus.RUNNING.value,
                expected_task_count=len(round_snapshot.task_ids),
                terminal_task_count=0,
                created_at=now,
            )
        )
        for task_id in round_snapshot.task_ids:
            task_row = task_by_id[task_id]
            attempt = (
                task_row.execution_generation + 1
                if task_row.active_execution_id
                else max(1, task_row.execution_generation)
            )
            execution_id = self._id_factory()
            worker_thread_id = f"task:{task_id}:exec:{attempt}"
            task_row.execution_generation = attempt
            task_row.worker_thread_id = worker_thread_id
            task_row.active_execution_id = execution_id
            task_row.status = TaskStatus.PENDING.value
            task_row.error_json = None
            task_row.updated_at = now.isoformat()
            session.add(
                RoundTaskModel(
                    round_id=round_snapshot.round_id,
                    task_id=task_id,
                    status=TaskStatus.PENDING.value,
                    active_execution_id=execution_id,
                )
            )
            session.add(
                TaskExecutionModel(
                    execution_id=execution_id,
                    round_id=round_snapshot.round_id,
                    task_id=task_id,
                    attempt=attempt,
                    worker_thread_id=worker_thread_id,
                    status=ExecutionStatus.QUEUED.value,
                    fencing_token=attempt,
                    cancellation_epoch=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                WorkerJobModel(
                    job_id=self._id_factory(),
                    dedupe_key=f"execute:{execution_id}",
                    job_type="execute",
                    execution_id=execution_id,
                    payload_json={
                        "plan_id": plan_row.plan_id,
                        "round_id": round_snapshot.round_id,
                        "task_id": task_id,
                    },
                    status="pending",
                    attempt=0,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        self._write_outbox(
            session,
            command,
            event_type="RoundStarted",
            dedupe_key=f"round:{round_snapshot.round_id}:started",
            round_snapshot=round_snapshot,
            payload={
                "task_ids": list(round_snapshot.task_ids),
                "review_action": decision.action.value,
                "reason": decision.reason,
            },
            now=now,
        )
        if decision.action == ReviewAction.REPLAN:
            self._write_outbox(
                session,
                command,
                event_type="PlanRevised",
                dedupe_key=(
                    f"plan:{plan_row.plan_id}:version:{round_snapshot.plan_version}:revised"
                ),
                round_snapshot=round_snapshot,
                payload={
                    "goal": plan.goal,
                    "max_parallelism": plan.max_parallelism,
                    "task_ids": list(plan.task_ids),
                    "edges": [
                        {
                            "from_task_id": edge.from_task_id,
                            "to_task_id": edge.to_task_id,
                        }
                        for edge in plan.edges
                    ],
                    "reason": decision.reason,
                },
                now=now,
            )

    def _write_review_terminal_outbox(
        self,
        session: AsyncSession,
        command: OrchestrationCommand,
        decision: ReviewDecision,
        plan_row: AgentPlanModel,
        round_row: PlanRoundModel,
        plan_status: PlanStatus,
        now: datetime,
    ) -> None:
        """Persist the Root's terminal decision as a durable Plan event."""
        event_type = (
            "PlanCompleted"
            if plan_status == PlanStatus.COMPLETED
            else "PlanFailed"
        )
        session.add(
            OrchestrationOutboxModel(
                message_id=self._id_factory(),
                dedupe_key=f"plan:{plan_row.plan_id}:{plan_status.value}",
                event_type=event_type,
                correlation_id=command.correlation_id,
                plan_id=plan_row.plan_id,
                plan_version=plan_row.current_version,
                round_id=round_row.round_id,
                payload_json={
                    "goal": plan_row.user_goal,
                    "reason": decision.reason,
                    "final_status": plan_status.value,
                    "parent_activity_id": f"run:{plan_row.run_id}",
                },
                schema_version=command.schema_version,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )

    def _write_outbox(
        self,
        session: AsyncSession,
        command: OrchestrationCommand,
        *,
        event_type: str,
        dedupe_key: str,
        round_snapshot: PlanRound,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        """将首轮业务事件写入和 Plan 状态放在同一事务。"""
        session.add(
            OrchestrationOutboxModel(
                message_id=self._id_factory(),
                dedupe_key=dedupe_key,
                event_type=event_type,
                correlation_id=command.correlation_id,
                plan_id=command.plan_id,
                plan_version=round_snapshot.plan_version,
                round_id=round_snapshot.round_id,
                payload_json=payload,
                schema_version=command.schema_version,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )


def _plan_document(plan: Plan, tasks: list[Task], *, version: int) -> dict[str, Any]:
    """生成供版本表保存的完整、可读 DAG 文档。"""
    return {
        "plan_id": plan.plan_id,
        "version": version,
        "goal": plan.goal,
        "max_parallelism": plan.max_parallelism,
        "nodes": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "objective": task.objective,
                "input": dict(task.input),
                "expected_output": dict(task.expected_output),
                "allowed_tools": list(task.allowed_tools),
                "worker_type": task.worker_type,
                "timeout_seconds": task.timeout_seconds,
            }
            for task in tasks
        ],
        "edges": [
            {"from": edge.from_task_id, "to": edge.to_task_id}
            for edge in plan.edges
        ],
    }


def _tasks_from_document(
    document: Mapping[str, Any], task_rows: dict[str, AgentTaskModel]
) -> list[Task]:
    """Rebuild task definitions from one immutable Plan Version."""
    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        raise PlanCommandRejected("review_plan_nodes_invalid")
    tasks: list[Task] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            raise PlanCommandRejected("review_plan_task_invalid")
        task_id = node.get("task_id")
        if not isinstance(task_id, str) or task_id not in task_rows:
            raise PlanCommandRejected("review_plan_task_definition_missing")
        title = node.get("title")
        objective = node.get("objective")
        raw_input = node.get("input")
        expected_output = node.get("expected_output")
        allowed_tools = node.get("allowed_tools")
        worker_type = node.get("worker_type")
        timeout_seconds = node.get("timeout_seconds")
        if (
            not isinstance(title, str)
            or not isinstance(objective, str)
            or not isinstance(raw_input, Mapping)
            or not isinstance(expected_output, Mapping)
            or not isinstance(allowed_tools, list)
            or any(not isinstance(item, str) for item in allowed_tools)
            or not isinstance(worker_type, str)
            or not _valid_timeout_seconds(timeout_seconds)
        ):
            raise PlanCommandRejected("review_plan_task_invalid")
        tasks.append(
            Task(
                task_id=task_id,
                title=title,
                objective=objective,
                input=dict(raw_input),
                expected_output=dict(expected_output),
                allowed_tools=tuple(allowed_tools),
                worker_type=worker_type,
                timeout_seconds=timeout_seconds,
            )
        )
    return tasks


def _plan_from_document(
    plan_row: AgentPlanModel,
    document: Mapping[str, Any],
    tasks: list[Task],
) -> Plan:
    """Rebuild a domain Plan while keeping the version document authoritative."""
    nodes = document.get("nodes")
    edges = document.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise PlanCommandRejected("review_plan_dag_invalid")
    task_ids: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("task_id"), str):
            raise PlanCommandRejected("review_plan_task_invalid")
        task_ids.append(node["task_id"])
    plan_edges: list[PlanEdge] = []
    for edge in edges:
        if (
            not isinstance(edge, Mapping)
            or not isinstance(edge.get("from"), str)
            or not isinstance(edge.get("to"), str)
        ):
            raise PlanCommandRejected("review_plan_edge_invalid")
        plan_edges.append(PlanEdge(edge["from"], edge["to"]))
    max_parallelism = document.get("max_parallelism", plan_row.max_parallelism)
    if not isinstance(max_parallelism, int) or isinstance(max_parallelism, bool):
        raise PlanCommandRejected("review_plan_parallelism_invalid")
    return Plan(
        plan_id=plan_row.plan_id,
        goal=plan_row.user_goal,
        task_ids=tuple(task_ids),
        edges=tuple(plan_edges),
        max_parallelism=max_parallelism,
        status=PlanStatus(plan_row.status),
        session_id=plan_row.session_id,
        run_id=plan_row.run_id,
    )


def _revise_plan_document(
    document: Mapping[str, Any],
    tasks: list[Task],
    task_rows: dict[str, AgentTaskModel],
    revision: PlanRevision,
    *,
    plan_row: AgentPlanModel,
    run_id: str,
) -> tuple[dict[str, Any], list[Task], list[Task]]:
    """Build the next immutable DAG and its retained/new task definitions."""
    old_nodes = document.get("nodes")
    old_edges = document.get("edges")
    if not isinstance(old_nodes, list) or not isinstance(old_edges, list):
        raise PlanCommandRejected("review_plan_dag_invalid")
    old_ids = {
        node.get("task_id")
        for node in old_nodes
        if isinstance(node, Mapping) and isinstance(node.get("task_id"), str)
    }
    keep_ids = set(revision.keep_task_ids)
    cancel_ids = set(revision.cancel_task_ids)
    if keep_ids | cancel_ids != old_ids or keep_ids & cancel_ids:
        raise PlanCommandRejected("revision_must_partition_existing_tasks")
    new_tasks = _revision_tasks(revision, run_id=run_id)
    if any(task.task_id in task_rows for task in new_tasks):
        raise PlanCommandRejected("revision_task_id_already_exists")

    retained_nodes = []
    for node in old_nodes:
        if not isinstance(node, Mapping) or node.get("task_id") not in keep_ids:
            continue
        task_id = str(node["task_id"])
        retained_nodes.append(
            {
                **node,
                "timeout_seconds": node.get(
                    "timeout_seconds",
                    getattr(task_rows[task_id], "timeout_seconds", 3600.0)
                    or 3600.0,
                ),
            }
        )
    new_ids = {task.task_id for task in new_tasks}
    scoped_ids_by_requested_id = {
        str(raw["task_id"]): task.task_id
        for raw, task in zip(revision.new_tasks, new_tasks, strict=True)
    }
    all_ids = keep_ids | new_ids
    revised_edges: list[dict[str, str]] = []
    for edge in old_edges:
        if not isinstance(edge, Mapping):
            raise PlanCommandRejected("review_plan_edge_invalid")
        source, target = edge.get("from"), edge.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            raise PlanCommandRejected("review_plan_edge_invalid")
        if source in keep_ids and target in keep_ids:
            revised_edges.append({"from": source, "to": target})
    new_nodes: list[dict[str, Any]] = []
    for raw_task in revision.new_tasks:
        requested_task_id = str(raw_task["task_id"])
        task_id = scoped_ids_by_requested_id[requested_task_id]
        title = raw_task.get("title")
        objective = raw_task.get("objective", raw_task.get("instruction"))
        dependencies = raw_task.get("depends_on", ())
        input_value = raw_task.get("input", {})
        expected_output = raw_task.get("expected_output", {})
        allowed_tools = raw_task.get("allowed_tools", ())
        worker_type = raw_task.get("worker_type", "general")
        timeout_seconds = raw_task.get("timeout_seconds", 3600.0)
        if (
            not isinstance(title, str)
            or not isinstance(objective, str)
            or not isinstance(dependencies, (list, tuple))
            or any(not isinstance(item, str) for item in dependencies)
            or not set(dependencies).issubset(all_ids)
            or not isinstance(input_value, Mapping)
            or not isinstance(expected_output, Mapping)
            or not isinstance(allowed_tools, (list, tuple))
            or any(not isinstance(item, str) for item in allowed_tools)
            or not isinstance(worker_type, str)
            or not _valid_timeout_seconds(timeout_seconds)
        ):
            raise PlanCommandRejected("revision_task_invalid")
        new_nodes.append(
            {
                "task_id": task_id,
                "title": title,
                "objective": objective,
                "input": dict(input_value),
                "expected_output": dict(expected_output),
                "allowed_tools": list(allowed_tools),
                "worker_type": worker_type,
                "timeout_seconds": timeout_seconds,
            }
        )
        revised_edges.extend(
            {
                "from": scoped_ids_by_requested_id.get(source, source),
                "to": task_id,
            }
            for source in dependencies
        )

    revised_document = {
        **document,
        "plan_id": plan_row.plan_id,
        "goal": plan_row.user_goal,
        "max_parallelism": plan_row.max_parallelism,
        "nodes": retained_nodes + new_nodes,
        "edges": revised_edges,
    }
    revised_tasks = [task for task in tasks if task.task_id in keep_ids]
    revised_tasks.extend(new_tasks)
    return revised_document, revised_tasks, new_tasks


def _revision_tasks(revision: PlanRevision, *, run_id: str) -> list[Task]:
    """Validate, scope and convert new task definitions from a Root revision."""
    tasks: list[Task] = []
    for raw in revision.new_tasks:
        task_id = raw.get("task_id")
        title = raw.get("title")
        objective = raw.get("objective", raw.get("instruction"))
        if (
            not isinstance(task_id, str)
            or not task_id.strip()
            or not isinstance(title, str)
            or not isinstance(objective, str)
        ):
            raise PlanCommandRejected("revision_task_invalid")
        raw_input = raw.get("input", {})
        expected_output = raw.get("expected_output", {})
        raw_tools = raw.get("allowed_tools", ())
        worker_type = raw.get("worker_type", "general")
        timeout_seconds = raw.get("timeout_seconds", 3600.0)
        if (
            not isinstance(raw_input, Mapping)
            or not isinstance(expected_output, Mapping)
            or not isinstance(raw_tools, (list, tuple))
            or any(not isinstance(item, str) for item in raw_tools)
            or not isinstance(worker_type, str)
            or not _valid_timeout_seconds(timeout_seconds)
        ):
            raise PlanCommandRejected("revision_task_invalid")
        tasks.append(
            Task(
                task_id=_scope_task_id(task_id, run_id),
                title=title,
                objective=objective,
                input=dict(raw_input),
                expected_output=dict(expected_output),
                allowed_tools=tuple(raw_tools),
                worker_type=worker_type,
                timeout_seconds=timeout_seconds,
            )
        )
    if len({task.task_id for task in tasks}) != len(tasks):
        raise PlanCommandRejected("revision_task_id_duplicate_after_scoping")
    return tasks


def _scope_task_id(task_id: str, run_id: str) -> str:
    """Bind a model-generated Task ID to this Run's database identity space."""
    suffix = f":{run_id}"
    return task_id if task_id.endswith(suffix) else f"{task_id}{suffix}"


def _valid_timeout_seconds(value: Any) -> bool:
    """判断持久任务定义中的超时是否为有限正数。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        timeout_seconds = float(value)
    except OverflowError:
        return False
    return isfinite(timeout_seconds) and timeout_seconds > 0


def _plan_hash(
    command: OrchestrationCommand, plan: Plan, tasks: list[Task]
) -> str:
    """对逻辑 Plan 请求生成稳定 SHA-256 指纹。"""
    canonical = json.dumps(
        {
            "plan": _plan_document(plan, tasks, version=1),
            "run_id": plan.run_id,
            "session_id": plan.session_id,
            "command_type": command.command_type.value,
            "plan_id": command.plan_id,
            "plan_version": command.plan_version,
            "payload": dict(command.payload),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _review_command_hash(
    command: OrchestrationCommand, decision: ReviewDecision
) -> str:
    """Hash the semantic decision without transient message/time fields."""
    canonical = json.dumps(
        {
            "command_type": command.command_type.value,
            "scope_key": command.scope_key,
            "idempotency_key": command.idempotency_key,
            "correlation_id": command.correlation_id,
            "plan_id": command.plan_id,
            "plan_version": command.plan_version,
            "round_id": command.round_id,
            "payload": dict(command.payload),
            "decision": decision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    """把已知领域值编码为 JSON 值，拒绝未知对象。"""
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported value for orchestration JSON: {type(value).__name__}")


def _receipt_document(receipt: CommandReceipt) -> dict[str, Any]:
    """生成回执持久化所需的 JSON 文档。"""
    return {
        "message_id": receipt.message_id,
        "plan_id": receipt.plan_id,
        "plan_version": receipt.plan_version,
        "round_id": receipt.round_id,
        "task_ids": list(receipt.task_ids),
    }


def _review_decision_document(decision: ReviewDecision) -> dict[str, Any]:
    """Serialize the exact accepted Root decision alongside its receipt."""
    revision = decision.revision
    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "revision": (
            {
                "base_version": revision.base_version,
                "keep_task_ids": list(revision.keep_task_ids),
                "cancel_task_ids": list(revision.cancel_task_ids),
                "new_tasks": [dict(task) for task in revision.new_tasks],
            }
            if revision is not None
            else None
        ),
        "final_status": decision.final_status,
    }


def _review_decision_from_document(value: Mapping[str, Any]) -> ReviewDecision:
    """Restore a stored ReviewDecision through its strict domain contract."""
    try:
        action = ReviewAction(value["action"])
        reason = value["reason"]
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("stored_review_decision_invalid") from exc
    if not isinstance(reason, str):
        raise RuntimeError("stored_review_decision_invalid")
    revision: PlanRevision | None = None
    if action == ReviewAction.REPLAN:
        raw_revision = value.get("revision")
        if not isinstance(raw_revision, Mapping):
            raise RuntimeError("stored_review_revision_invalid")
        base_version = raw_revision.get("base_version")
        keep_ids = raw_revision.get("keep_task_ids")
        cancel_ids = raw_revision.get("cancel_task_ids")
        new_tasks = raw_revision.get("new_tasks")
        if (
            not isinstance(base_version, int)
            or isinstance(base_version, bool)
            or not isinstance(keep_ids, list)
            or not isinstance(cancel_ids, list)
            or not isinstance(new_tasks, list)
            or any(not isinstance(item, str) for item in (*keep_ids, *cancel_ids))
            or any(not isinstance(item, Mapping) for item in new_tasks)
        ):
            raise RuntimeError("stored_review_revision_invalid")
        revision = PlanRevision(
            base_version=base_version,
            keep_task_ids=tuple(keep_ids),
            cancel_task_ids=tuple(cancel_ids),
            new_tasks=tuple(dict(task) for task in new_tasks),
        )
    final_status = value.get("final_status")
    if final_status is not None and not isinstance(final_status, str):
        raise RuntimeError("stored_review_decision_invalid")
    try:
        return ReviewDecision(
            action=action,
            reason=reason,
            revision=revision,
            final_status=final_status,
        )
    except ValueError as exc:
        raise RuntimeError("stored_review_decision_invalid") from exc


def _receipt(value: dict[str, Any] | None, *, deduplicated: bool) -> CommandReceipt:
    """从已存回执还原稳定响应。"""
    if value is None:
        raise RuntimeError("accepted command has no stored receipt")
    return CommandReceipt(
        message_id=str(value["message_id"]),
        plan_id=str(value["plan_id"]),
        plan_version=int(value["plan_version"]),
        round_id=str(value["round_id"]),
        task_ids=tuple(str(item) for item in value["task_ids"]),
        deduplicated=deduplicated,
    )


__all__ = [
    "OrchestrationCommandConflict",
    "PlanCommandRejected",
    "PostgresOrchestrationCommandRepository",
]
