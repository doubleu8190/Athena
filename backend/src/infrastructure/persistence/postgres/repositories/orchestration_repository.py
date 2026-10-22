"""Native target PostgreSQL repository for Plan/Task orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
    Plan,
    PlanDAG,
    PlanEdge,
    PlanRepository,
    PlanStatus,
    ReviewContext,
    ReviewContextRepository,
    Task,
    TaskExecution,
    TaskResult,
    TaskStatus,
)

from ..models import (
    AgentPlanModel,
    AgentRunModel,
    AgentTaskModel,
    ApprovalRecordModel,
    OrchestrationOutboxModel,
    PlanRoundModel,
    PlanVersionModel,
    RoundTaskModel,
    TaskExecutionModel,
)
from .task_execution_results import load_task_execution_outputs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


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
        else _load(task_row.error_json, None)
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


def _execution_snapshot(execution: TaskExecutionModel) -> dict[str, object]:
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
        "fencing_token": execution.fencing_token,
        "cancellation_epoch": execution.cancellation_epoch,
        "lease_until": execution.lease_until,
        "result_ref": execution.result_ref,
        "error": error,
        "created_at": execution.created_at,
        "updated_at": execution.updated_at,
    }


class PostgresOrchestrationRepository(PlanRepository, ReviewContextRepository):
    """Persist target Plan/Task entities and claim work atomically."""

    def __init__(self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._session_factory = session_factory

    async def create(self, plan: Plan, tasks: list[Task]) -> Plan:
        now = _now()
        payload = {"nodes": [{"task_id": item} for item in plan.task_ids], "edges": [{"from": edge.from_task_id, "to": edge.to_task_id} for edge in plan.edges]}
        async with self._session_factory() as session:
            async with session.begin():
                session.add(AgentPlanModel(plan_id=plan.plan_id, run_id=plan.run_id or "", session_id=plan.session_id or "", status=plan.status.value, user_goal=plan.goal, max_parallelism=plan.max_parallelism, plan_json=_dump(payload), created_at=now, updated_at=now))
                session.add_all([AgentTaskModel(task_id=task.task_id, plan_id=plan.plan_id, run_id=plan.run_id or "", title=task.title, objective=task.objective, input_json=_dump(dict(task.input)), expected_output_json=_dump(dict(task.expected_output)), allowed_tools_json=_dump(list(task.allowed_tools)), worker_type=task.worker_type, timeout_seconds=task.timeout_seconds, status=TaskStatus.PENDING.value, execution_generation=0, created_at=now, updated_at=now) for task in tasks])
        return plan

    async def get(self, plan_id: str) -> tuple[Plan, list[Task]] | None:
        async with self._session_factory() as session:
            plan_row = await session.get(AgentPlanModel, plan_id)
            if plan_row is None:
                return None
            rows = (await session.execute(select(AgentTaskModel).where(AgentTaskModel.plan_id == plan_id).order_by(AgentTaskModel.task_id))).scalars().all()
        return self._plan(plan_row), [self._task(row) for row in rows]

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
                                _execution_snapshot(execution)
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
                    TaskStatus.DONE.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.TIMED_OUT.value,
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
                    TaskStatus.TIMED_OUT.value,
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

    async def claim_ready_tasks(self, plan_id: str, max_parallelism: int) -> list[TaskExecution]:
        async with self._session_factory() as session:
            async with session.begin():
                plan_row = await session.get(AgentPlanModel, plan_id, with_for_update=True)
                if plan_row is None or plan_row.status not in {PlanStatus.CREATED.value, PlanStatus.RUNNING.value}:
                    return []
                rows = (await session.execute(select(AgentTaskModel).where(AgentTaskModel.plan_id == plan_id).with_for_update())).scalars().all()
                plan = self._plan(plan_row)
                states = {row.task_id: row.status for row in rows}
                dag = PlanDAG(plan)
                dag.propagate_dependency_failures(states)
                for row in rows:
                    if states.get(row.task_id) == TaskStatus.CANCELLED.value and row.status == TaskStatus.PENDING.value:
                        row.status = TaskStatus.CANCELLED.value
                        row.error_json = _dump({"reason": "dependency_failed"})
                        row.updated_at = _now()
                running = sum(row.status == TaskStatus.RUNNING.value for row in rows)
                claimed: list[TaskExecution] = []
                task_by_id = {row.task_id: self._task(row) for row in rows}
                for task_id in dag.ready_tasks({row.task_id: row.status for row in rows}):
                    if running + len(claimed) >= min(max_parallelism, plan.max_parallelism):
                        break
                    row = next(row for row in rows if row.task_id == task_id)
                    row.status = TaskStatus.RUNNING.value
                    row.execution_generation += 1
                    row.worker_thread_id = f"task:{task_id}:exec:{row.execution_generation}"
                    row.updated_at = _now()
                    upstream = await self._upstream(
                        session, plan, rows, task_id
                    )
                    claimed.append(TaskExecution(plan.plan_id, plan.run_id or row.run_id, plan.session_id or "", task_by_id[task_id], row.execution_generation, upstream))
                if claimed and plan_row.status == PlanStatus.CREATED.value:
                    plan_row.status = PlanStatus.RUNNING.value
                    plan_row.updated_at = _now()
                return claimed

    async def finish_task(
        self,
        task_id: str,
        execution_generation: int,
        *,
        status: str,
        output: dict[str, object] | None = None,
        error: dict[str, object] | None = None,
        finalize_plan: bool = True,
    ) -> dict[str, object]:
        async with self._session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(AgentTaskModel)
                        .where(
                            AgentTaskModel.task_id == task_id,
                            AgentTaskModel.execution_generation
                            == execution_generation,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None or row.status != TaskStatus.RUNNING.value:
                    return {"accepted": False, "reason": "stale_execution"}
                now = datetime.now(timezone.utc)
                row.status = status
                row.output_json = _dump(output) if output is not None else None
                row.error_json = _dump(error) if error is not None else None
                row.updated_at = now.isoformat()
                plan_row = await session.get(AgentPlanModel, row.plan_id, with_for_update=True)
                plan_status = await self._terminal_plan_status(session, row.plan_id)
                plan_completed = (
                    plan_status is not None
                    and plan_row is not None
                    and plan_row.status
                    in {PlanStatus.CREATED.value, PlanStatus.RUNNING.value}
                )
                if finalize_plan and plan_completed:
                    self._record_plan_terminal(
                        session,
                        plan_row,
                        plan_status,
                        now,
                    )
                return {"accepted": True, "plan_completed": plan_completed}

    async def finalize_plan(self, plan_id: str) -> PlanStatus | None:
        """将已全部终结的活跃 Plan 与终态事件原子提交。"""
        async with self._session_factory() as session:
            async with session.begin():
                plan_row = await session.get(
                    AgentPlanModel, plan_id, with_for_update=True
                )
                if (
                    plan_row is None
                    or plan_row.status
                    not in {PlanStatus.CREATED.value, PlanStatus.RUNNING.value}
                ):
                    return None
                plan_status = await self._terminal_plan_status(session, plan_id)
                if plan_status is None:
                    return None
                self._record_plan_terminal(
                    session,
                    plan_row,
                    plan_status,
                    datetime.now(timezone.utc),
                )
                return plan_status

    @staticmethod
    def _record_plan_terminal(
        session: AsyncSession,
        plan_row: AgentPlanModel,
        plan_status: PlanStatus,
        now: datetime,
    ) -> None:
        """原子记录 Plan 终态和 SSE Outbox 事实。"""
        plan_row.status = plan_status.value
        plan_row.updated_at = now.isoformat()
        event_type = (
            "PlanCompleted"
            if plan_status == PlanStatus.COMPLETED
            else "PlanFailed"
        )
        session.add(
            OrchestrationOutboxModel(
                message_id=uuid4().hex,
                dedupe_key=f"plan:{plan_row.plan_id}:{plan_status.value}",
                event_type=event_type,
                correlation_id=plan_row.run_id,
                plan_id=plan_row.plan_id,
                plan_version=plan_row.current_version,
                round_id=plan_row.active_round_id,
                payload_json={
                    "goal": plan_row.user_goal,
                    "parent_activity_id": f"run:{plan_row.run_id}",
                },
                schema_version=1,
                status="pending",
                attempt=0,
                available_at=now,
                occurred_at=now,
            )
        )

    async def set_task_waiting_approval(self, task_id: str, execution_generation: int, approval_batch: dict[str, object] | None = None) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                values: dict[str, object] = {
                    "status": TaskStatus.WAITING_APPROVAL.value,
                    "updated_at": _now(),
                }
                if approval_batch is not None:
                    values["error_json"] = _dump({"approval_batch": approval_batch})
                result = await session.execute(
                    update(AgentTaskModel)
                    .where(
                        AgentTaskModel.task_id == task_id,
                        AgentTaskModel.execution_generation == execution_generation,
                        AgentTaskModel.status == TaskStatus.RUNNING.value,
                    )
                    .values(**values)
                )
        return bool(result.rowcount)

    async def set_task_running(self, task_id: str, execution_generation: int) -> bool:
        return await self._set_status(task_id, execution_generation, TaskStatus.RUNNING.value, expected=TaskStatus.WAITING_APPROVAL.value)

    async def retry_task(self, task_id: str) -> dict[str, object] | None:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(update(AgentTaskModel).where(AgentTaskModel.task_id == task_id, AgentTaskModel.status.in_([TaskStatus.FAILED.value, TaskStatus.TIMED_OUT.value])).values(status=TaskStatus.PENDING.value, updated_at=_now()))
                if result.rowcount:
                    row = await session.get(AgentTaskModel, task_id, with_for_update=True)
                    if row is not None:
                        plan_row = await session.get(AgentPlanModel, row.plan_id, with_for_update=True)
                        if plan_row is not None:
                            plan_row.status = PlanStatus.RUNNING.value
                            plan_row.updated_at = _now()
                return {"task_id": task_id} if result.rowcount else None

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

    async def get_task_execution(self, task_id: str) -> TaskExecution | None:
        async with self._session_factory() as session:
            row = await session.get(AgentTaskModel, task_id)
            if row is None:
                return None
            plan_row = await session.get(AgentPlanModel, row.plan_id)
            if plan_row is None:
                return None
            rows = (await session.execute(select(AgentTaskModel).where(AgentTaskModel.plan_id == row.plan_id))).scalars().all()
            upstream = await self._upstream(session, self._plan(plan_row), rows, task_id)
        plan = self._plan(plan_row)
        return TaskExecution(plan.plan_id, plan.run_id or row.run_id, plan.session_id or "", self._task(row), row.execution_generation, upstream)

    async def _set_status(self, task_id: str, generation: int, status: str, *, expected: str = TaskStatus.RUNNING.value) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                result = await session.execute(update(AgentTaskModel).where(AgentTaskModel.task_id == task_id, AgentTaskModel.execution_generation == generation, AgentTaskModel.status == expected).values(status=status, updated_at=_now()))
        return bool(result.rowcount)

    async def _terminal_plan_status(self, session: AsyncSession, plan_id: str) -> PlanStatus | None:
        rows = (await session.execute(select(AgentTaskModel.status).where(AgentTaskModel.plan_id == plan_id))).all()
        if not rows:
            return None
        terminal = {
            item.value
            for item in (
                TaskStatus.COMPLETED,
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.TIMED_OUT,
                TaskStatus.CANCELLED,
            )
        }
        if not all(row[0] in terminal for row in rows):
            return None
        successful = {TaskStatus.DONE.value, TaskStatus.COMPLETED.value}
        return PlanStatus.COMPLETED if all(row[0] in successful for row in rows) else PlanStatus.FAILED

    @staticmethod
    def _plan(row) -> Plan:
        payload = _load(row.plan_json, {})
        return Plan(row.plan_id, row.user_goal, tuple(item["task_id"] for item in payload.get("nodes", [])), tuple(PlanEdge(item["from"], item["to"]) for item in payload.get("edges", [])), row.max_parallelism, PlanStatus(row.status), row.session_id, row.run_id)

    @staticmethod
    def _task(row) -> Task:
        return Task(
            row.task_id,
            row.title,
            row.objective,
            _load(row.input_json, {}),
            _load(row.expected_output_json, {}),
            tuple(_load(row.allowed_tools_json, [])),
            row.worker_type,
            getattr(row, "timeout_seconds", 3600.0) or 3600.0,
        )

    async def _upstream(
        self,
        session: AsyncSession,
        plan: Plan,
        rows: list,
        task_id: str,
    ) -> dict[str, TaskResult]:
        """读取 DAG 中指定 Task 的直接上游结果，并解析持久化引用。

        Args:
            session: 当前事务使用的 PostgreSQL 会话。
            plan: 用于确定直接依赖边的 Plan。
            rows: Plan 中已加载的 Task 投影。
            task_id: 需要读取上游结果的 Task ID。

        Returns:
            以直接上游 Task ID 为键的完整结果映射。

        Raises:
            RuntimeError: 上游大结果的持久化引用无法解析时抛出。
        """
        by_id = {row.task_id: row for row in rows}
        parent_rows = [
            by_id[edge.from_task_id]
            for edge in plan.edges
            if edge.to_task_id == task_id and edge.from_task_id in by_id
        ]
        return await self._task_results(session, parent_rows)

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
                else _load(row.output_json, {})
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
                error=_load(row.error_json, None),
            )
        return results


class PostgresPlanLoader:
    """Expose persisted plan completion to the Root graph adapter."""

    def __init__(self, repository: PostgresOrchestrationRepository) -> None:
        self._repository = repository

    async def __call__(self, *, plan_id: str | None, **_kwargs: object) -> dict[str, object]:
        if not plan_id:
            return {"completed": False, "results": {}}
        value = await self._repository.get(plan_id)
        if value is None:
            return {"completed": False, "results": {}}
        plan, _ = value
        results = await self._repository.get_results(plan_id)
        wait_context = await self._repository.get_wait_context(plan_id)
        completed = plan.status in {
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
