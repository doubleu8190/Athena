"""Native target PostgreSQL repository for Plan/Task orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
    PlanStatus,
    ReviewContext,
    ReviewContextRepository,
    TaskResult,
    TaskStatus,
)

from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
)
from domain.leases import LeaseRecord
from .lease_center import PostgresLeaseCenter
from .task_execution_results import load_task_execution_outputs


def _review_task_result(
    round_task: RoundTaskModel,
    task_row: AgentTaskModel,
    execution: TaskExecutionModel | None,
    output: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """把执行记录转换成有界、带引用的 Root Review 结果摘要。"""
    result = dict(output) if isinstance(output, Mapping) else {}
    summary_value = result.get("output", result)
    summary_json = json.dumps(
        summary_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if len(summary_json) > 6000:
        summary_json = summary_json[:6000] + "…[truncated]"
    uncertainties = result.get("uncertainties", ())
    if isinstance(uncertainties, str):
        uncertainties = (uncertainties,) if uncertainties else ()
    elif not isinstance(uncertainties, (list, tuple)):
        uncertainties = ()
    error = (
        execution.error_json
        if execution is not None and isinstance(execution.error_json, Mapping)
        else task_row.error_json
    )
    return {
        "task_id": round_task.task_id,
        "status": round_task.status,
        "summary": summary_json,
        "result_ref": execution.result_ref if execution is not None else None,
        "evidence": result.get("evidence", []),
        "uncertainties": [str(item) for item in uncertainties if str(item)],
        "error": dict(error) if isinstance(error, Mapping) else None,
    }


def _bounded_text(value: object | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _execution_snapshot(
    execution: TaskExecutionModel, lease: LeaseRecord | None
) -> dict[str, object]:
    error: dict[str, object] | None = None
    if isinstance(execution.error_json, Mapping):
        error = {
            key: _bounded_text(value, 240) if key == "message" else value
            for key, value in execution.error_json.items()
            if key in {"code", "message", "retryable", "stage"}
        }
    return {
        "execution_id": execution.execution_id,
        "attempt": execution.attempt,
        "worker_thread_id": execution.worker_thread_id,
        "status": execution.status,
        "fencing_token": lease.fencing_token if lease is not None else None,
        "cancellation_epoch": execution.cancellation_epoch,
        "lease_until": lease.lease_until if lease is not None else None,
        "result_ref": execution.result_ref,
        "error": error,
        "created_at": execution.created_at,
        "updated_at": execution.updated_at,
    }


class PostgresOrchestrationRepository(ReviewContextRepository):
    """Read persisted plan and round data for Root review and result projection."""

    def __init__(self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._session_factory = session_factory
        self._leases = PostgresLeaseCenter(session_factory)

    async def get_plan_status(self, plan_id: str) -> PlanStatus | None:
        async with self._session_factory() as session:
            plan_row = await session.get(AgentPlanModel, plan_id)
        if plan_row is None:
            return None
        return PlanStatus(plan_row.status)

    async def get_run_orchestration_snapshot(
        self, run_id: str
    ) -> Mapping[str, object] | None:
        """从同一 PostgreSQL 快照读取可刷新恢复的 Run 编排事实。"""
        async with self._session_factory() as session:
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            run_row = await session.get(AgentRunModel, run_id)
            if run_row is None:
                return None

            plan_row = (
                await session.execute(
                    select(AgentPlanModel).where(AgentPlanModel.run_id == run_id)
                )
            ).scalar_one_or_none()
            approvals = (
                await session.execute(
                    select(ApprovalRecordModel)
                    .where(ApprovalRecordModel.run_id == run_id)
                    .order_by(ApprovalRecordModel.created_at, ApprovalRecordModel.approval_id)
                )
            ).scalars().all()

            plan_document: dict[str, object] | None = None
            if plan_row is not None:
                round_rows = (
                    await session.execute(
                        select(PlanRoundModel)
                        .where(PlanRoundModel.plan_id == plan_row.plan_id)
                        .order_by(PlanRoundModel.round_number)
                    )
                ).scalars().all()
                version_rows = (
                    await session.execute(
                        select(PlanVersionModel)
                        .where(PlanVersionModel.plan_id == plan_row.plan_id)
                        .order_by(PlanVersionModel.version)
                    )
                ).scalars().all()
                round_ids = [row.round_id for row in round_rows]
                members = (
                    await session.execute(
                        select(RoundTaskModel, AgentTaskModel)
                        .join(
                            AgentTaskModel,
                            AgentTaskModel.task_id == RoundTaskModel.task_id,
                        )
                        .where(RoundTaskModel.round_id.in_(round_ids))
                        .order_by(RoundTaskModel.round_id, RoundTaskModel.task_id)
                    )
                ).all() if round_ids else []
                executions = (
                    await session.execute(
                        select(TaskExecutionModel)
                        .where(TaskExecutionModel.round_id.in_(round_ids))
                        .order_by(
                            TaskExecutionModel.round_id,
                            TaskExecutionModel.task_id,
                            TaskExecutionModel.attempt,
                        )
                    )
                ).scalars().all() if round_ids else []

                leases_by_execution = await self._leases.get_many_in_transaction(
                    session, "worker_execution",
                    [execution.execution_id for execution in executions],
                )

                executions_by_member: dict[tuple[str, str], list[TaskExecutionModel]] = {}
                for execution in executions:
                    executions_by_member.setdefault(
                        (execution.round_id, execution.task_id), []
                    ).append(execution)
                tasks_by_round: dict[str, list[dict[str, object]]] = {}
                for member, task_row in members:
                    tasks_by_round.setdefault(member.round_id, []).append(
                        {
                            "task_id": member.task_id,
                            "title": _bounded_text(task_row.title, 240),
                            "objective": _bounded_text(task_row.objective, 2000),
                            "status": member.status,
                            "active_execution_id": member.active_execution_id,
                            "terminal_at": member.terminal_at,
                            "executions": [
                                _execution_snapshot(
                                    execution,
                                    leases_by_execution.get(execution.execution_id),
                                )
                                for execution in executions_by_member.get(
                                    (member.round_id, member.task_id), []
                                )
                            ],
                        }
                    )

                plan_document = {
                    "plan_id": plan_row.plan_id,
                    "status": plan_row.status,
                    "user_goal": _bounded_text(plan_row.user_goal, 4000),
                    "max_parallelism": plan_row.max_parallelism,
                    "current_version": plan_row.current_version,
                    "active_round_id": plan_row.active_round_id,
                    "cancellation_epoch": plan_row.cancellation_epoch,
                    "versions": [
                        {
                            "version": version.version,
                            "change_reason": _bounded_text(version.change_reason, 500),
                            "based_on_round_id": version.based_on_round_id,
                            "created_at": version.created_at,
                        }
                        for version in version_rows
                    ],
                    "rounds": [
                        {
                            "round_id": round_row.round_id,
                            "plan_version": round_row.plan_version,
                            "round_number": round_row.round_number,
                            "status": round_row.status,
                            "expected_task_count": round_row.expected_task_count,
                            "terminal_task_count": round_row.terminal_task_count,
                            "created_at": round_row.created_at,
                            "completed_at": round_row.completed_at,
                            "tasks": tasks_by_round.get(round_row.round_id, []),
                        }
                        for round_row in round_rows
                    ],
                }

        return {
            "run": {
                "run_id": run_row.run_id,
                "session_id": run_row.session_id,
                "status": run_row.status,
                "root_thread_id": run_row.root_thread_id,
                "cancellation_epoch": run_row.cancellation_epoch,
                "cancellation_external_effect_status": run_row.cancellation_external_effect_status,
                "created_at": run_row.created_at,
                "updated_at": run_row.updated_at,
            },
            "plan": plan_document,
            "approvals": [
                {
                    "approval_id": approval.approval_id,
                    "plan_id": approval.plan_id,
                    "task_id": approval.task_id,
                    "execution_id": approval.execution_id,
                    "interrupt_id": approval.interrupt_id,
                    "tool_name": approval.tool_name,
                    "risk_level": approval.risk_level,
                    "status": approval.status,
                    "decision": approval.decision,
                    "created_at": approval.created_at,
                    "decided_at": approval.decided_at,
                }
                for approval in approvals
            ],
        }

    async def get_plan_version(
        self, plan_id: str, version: int
    ) -> Mapping[str, object] | None:
        """按复合身份读取不可变 DAG 版本和修订说明。"""
        async with self._session_factory() as session:
            row = await session.get(PlanVersionModel, (plan_id, version))
        if row is None:
            return None
        return {
            "plan_id": row.plan_id,
            "version": row.version,
            "dag_json": row.dag_json,
            "change_reason": row.change_reason,
            "based_on_round_id": row.based_on_round_id,
            "created_at": row.created_at,
        }

    async def load_review_context(self, round_id: str) -> ReviewContext:
        """按指定 Round 和不可变版本读取 Root Review 所需业务事实。

        Args:
            round_id: 必须是当前等待审查的已完成 Round。

        Returns:
            使用该 Round 对应 Plan Version、历史结果和未完成任务构成的上下文。

        Raises:
            LookupError: Round、Plan 或其版本不存在。
            RuntimeError: Round 尚未完成、已过期或持久化身份不一致。
        """
        async with self._session_factory() as session:
            round_row = await session.get(PlanRoundModel, round_id)
            if round_row is None:
                raise LookupError("review_round_not_found")
            if round_row.status != "completed":
                raise RuntimeError("review_round_not_completed")

            plan_row = await session.get(AgentPlanModel, round_row.plan_id)
            if plan_row is None:
                raise LookupError("review_plan_not_found")
            if (
                plan_row.status != PlanStatus.WAITING_REVIEW.value
                or plan_row.active_round_id != round_id
                or plan_row.current_version != round_row.plan_version
                or round_row.terminal_task_count != round_row.expected_task_count
            ):
                raise RuntimeError("review_round_identity_stale")

            version_row = await session.get(
                PlanVersionModel, (round_row.plan_id, round_row.plan_version)
            )
            if version_row is None:
                raise LookupError("review_plan_version_not_found")
            dag_document = version_row.dag_json
            if not isinstance(dag_document, Mapping):
                raise RuntimeError("review_plan_version_document_invalid")

            task_rows = (
                await session.execute(
                    select(AgentTaskModel).where(
                        AgentTaskModel.plan_id == plan_row.plan_id
                    )
                )
            ).scalars().all()
            task_by_id = {row.task_id: row for row in task_rows}
            round_execution_rows = (
                await session.execute(
                    select(
                        PlanRoundModel.round_number,
                        RoundTaskModel,
                        TaskExecutionModel,
                    )
                    .join(
                        RoundTaskModel,
                        RoundTaskModel.round_id == PlanRoundModel.round_id,
                    )
                    .outerjoin(
                        TaskExecutionModel,
                        TaskExecutionModel.execution_id
                        == RoundTaskModel.active_execution_id,
                    )
                    .where(
                        PlanRoundModel.plan_id == plan_row.plan_id,
                        PlanRoundModel.round_number <= round_row.round_number,
                        PlanRoundModel.status == "completed",
                    )
                    .order_by(
                        PlanRoundModel.round_number,
                        RoundTaskModel.task_id,
                    )
                )
            ).all()
            executions = [
                execution
                for _, _, execution in round_execution_rows
                if execution is not None
            ]
            outputs_by_execution = await load_task_execution_outputs(
                session, executions
            )

        current_task_ids: set[str] = set()
        previous_results: list[Mapping[str, Any]] = []
        round_results: list[Mapping[str, Any]] = []
        for result_round_number, round_task, execution in round_execution_rows:
            task_id = round_task.task_id
            if execution is not None and (
                execution.round_id != round_task.round_id
                or execution.task_id != task_id
            ):
                raise RuntimeError("review_round_execution_identity_mismatch")
            round_is_incomplete = (
                result_round_number == round_row.round_number
                and round_task.status
                not in {
                    TaskStatus.COMPLETED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                }
            )
            if round_is_incomplete:
                raise RuntimeError("review_round_task_not_terminal")
            task_row = task_by_id.get(task_id)
            if task_row is None:
                raise RuntimeError("review_round_task_definition_missing")
            result = _review_task_result(
                round_task,
                task_row,
                execution,
                outputs_by_execution.get(execution.execution_id)
                if execution is not None
                else None,
            )
            if result_round_number == round_row.round_number:
                current_task_ids.add(task_id)
                round_results.append(result)
            else:
                previous_results.append(result)

        nodes = dag_document.get("nodes", [])
        edges = dag_document.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise RuntimeError("review_plan_dag_invalid")
        definitions_by_id: dict[str, Mapping[str, Any]] = {}
        for definition in nodes:
            if not isinstance(definition, Mapping):
                raise RuntimeError("review_plan_dag_invalid")
            task_id = definition.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError("review_plan_dag_invalid")
            if task_id in definitions_by_id:
                raise RuntimeError("review_plan_task_id_duplicate")
            if task_id not in task_by_id:
                raise RuntimeError("review_plan_task_definition_missing")
            definitions_by_id[task_id] = definition
        previous_results = [
            item for item in previous_results if item["task_id"] in definitions_by_id
        ]

        dependencies_by_task: dict[str, list[str]] = {}
        for edge in edges:
            if not isinstance(edge, Mapping):
                raise RuntimeError("review_plan_dag_invalid")
            source = edge.get("from")
            target = edge.get("to")
            if (
                not isinstance(source, str)
                or not isinstance(target, str)
                or source not in definitions_by_id
                or target not in definitions_by_id
            ):
                raise RuntimeError("review_plan_dag_invalid")
            dependencies_by_task.setdefault(target, []).append(source)

        remaining_tasks: list[Mapping[str, Any]] = []
        for task_id, definition in definitions_by_id.items():
            if task_id in current_task_ids:
                continue
            task_row = task_by_id[task_id]
            blocked_by = sorted(
                parent_id
                for parent_id in dependencies_by_task.get(task_id, [])
                if task_by_id[parent_id].status
                in {
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                }
            )
            task_status = task_row.status
            if task_status == TaskStatus.PENDING.value or (
                task_status == TaskStatus.CANCELLED.value and blocked_by
            ):
                remaining_tasks.append(
                    {
                        "task_id": task_id,
                        "title": str(definition.get("title", "")),
                        "instruction": str(
                            definition.get("objective", definition.get("title", ""))
                        ),
                        "depends_on": dependencies_by_task.get(task_id, []),
                        "status": task_status,
                        "blocked_by": blocked_by,
                    }
                )

        if len(current_task_ids) != round_row.expected_task_count:
            raise RuntimeError("review_round_membership_incomplete")

        raw_constraints = dag_document.get("user_constraints", ())
        user_constraints = (
            tuple(item for item in raw_constraints if isinstance(item, str))
            if isinstance(raw_constraints, (list, tuple))
            else ()
        )
        raw_budget = dag_document.get("remaining_budget", {})
        remaining_budget = dict(raw_budget) if isinstance(raw_budget, Mapping) else {}
        return ReviewContext(
            original_goal=plan_row.user_goal,
            plan_id=plan_row.plan_id,
            plan_version=round_row.plan_version,
            round_id=round_row.round_id,
            full_dag_summary=dict(dag_document),
            previous_results_summary=tuple(previous_results),
            round_results=tuple(round_results),
            remaining_tasks=tuple(remaining_tasks),
            user_constraints=user_constraints,
            remaining_budget=remaining_budget,
        )

    async def get_wait_context(self, plan_id: str) -> dict[str, object] | None:
        """Return the exact active Round identity used by Root interrupts."""
        async with self._session_factory() as session:
            row = await session.get(AgentPlanModel, plan_id)
        if row is None or row.active_round_id is None:
            return None
        return {
            "type": "round_wait",
            "run_id": row.run_id,
            "plan_id": row.plan_id,
            "round_id": row.active_round_id,
            "plan_version": row.current_version,
        }

    async def get_results(self, plan_id: str) -> Mapping[str, TaskResult]:
        async with self._session_factory() as session:
            rows = (await session.execute(select(AgentTaskModel).where(AgentTaskModel.plan_id == plan_id))).scalars().all()
            results = await self._task_results(session, rows)
        return results

    async def _task_results(
        self,
        session: AsyncSession,
        rows: list,
    ) -> dict[str, TaskResult]:
        """将 Task 投影与对应执行结果组合为完整领域结果。

        Args:
            session: 当前事务使用的 PostgreSQL 会话。
            rows: 需要恢复结果的 Task 投影。

        Returns:
            以 Task ID 为键的结果映射。

        Raises:
            RuntimeError: 投影包含无法解析的大结果引用时抛出。
        """
        execution_ids = [
            row.active_execution_id for row in rows if row.active_execution_id
        ]
        executions = (
            (
                await session.execute(
                    select(TaskExecutionModel).where(
                        TaskExecutionModel.execution_id.in_(execution_ids)
                    )
                )
            ).scalars().all()
            if execution_ids
            else []
        )
        outputs_by_execution = await load_task_execution_outputs(
            session, executions
        )
        results: dict[str, TaskResult] = {}
        for row in rows:
            resolved_output = outputs_by_execution.get(row.active_execution_id)
            raw_output = (
                resolved_output
                if resolved_output is not None
                else (row.output_json if row.output_json is not None else {})
            )
            if (
                resolved_output is None
                and isinstance(raw_output, Mapping)
                and raw_output.get("result_ref")
                and {"result_bytes", "result_sha256"}.issubset(raw_output)
            ):
                raise RuntimeError("task_execution_result_reference_unavailable")
            if not isinstance(raw_output, Mapping):
                raw_output = {}
            try:
                status = TaskStatus(row.status)
            except ValueError:
                status = TaskStatus.FAILED
            results[row.task_id] = TaskResult(
                row.task_id,
                status,
                dict(raw_output),
                error=row.error_json,
            )
        return results


class PostgresPlanLoader:
    """Expose persisted plan completion to the Root graph adapter."""

    def __init__(self, repository: PostgresOrchestrationRepository) -> None:
        self._repository = repository

    async def __call__(self, *, plan_id: str | None, **_kwargs: object) -> dict[str, object]:
        if not plan_id:
            return {"completed": False, "results": {}}
        status = await self._repository.get_plan_status(plan_id)
        if status is None:
            return {"completed": False, "results": {}}
        results = await self._repository.get_results(plan_id)
        wait_context = await self._repository.get_wait_context(plan_id)
        completed = status in {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
        }
        return {
            "completed": completed,
            "results": results,
            "root_wait": wait_context or {},
        }


__all__ = ["PostgresOrchestrationRepository", "PostgresPlanLoader"]
