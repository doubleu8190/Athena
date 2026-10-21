"""Native target PostgreSQL repository for Plan/Task orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.orchestration import (
    Plan,
    PlanDAG,
    PlanEdge,
    PlanRepository,
    PlanStatus,
    Task,
    TaskExecution,
    TaskResult,
    TaskStatus,
)

from ..models import AgentPlanModel, AgentTaskModel, TaskExecutionModel
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


class PostgresOrchestrationRepository(PlanRepository):
    """Persist target Plan/Task entities and claim work atomically."""

    def __init__(self, session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> None:
        self._session_factory = session_factory

    async def create(self, plan: Plan, tasks: list[Task]) -> Plan:
        now = _now()
        payload = {"nodes": [{"task_id": item} for item in plan.task_ids], "edges": [{"from": edge.from_task_id, "to": edge.to_task_id} for edge in plan.edges]}
        async with self._session_factory() as session:
            async with session.begin():
                session.add(AgentPlanModel(plan_id=plan.plan_id, run_id=plan.run_id or "", session_id=plan.session_id or "", status=plan.status.value, user_goal=plan.goal, max_parallelism=plan.max_parallelism, plan_json=_dump(payload), created_at=now, updated_at=now))
                session.add_all([AgentTaskModel(task_id=task.task_id, plan_id=plan.plan_id, run_id=plan.run_id or "", title=task.title, objective=task.objective, input_json=_dump(dict(task.input)), expected_output_json=_dump(dict(task.expected_output)), allowed_tools_json=_dump(list(task.allowed_tools)), worker_type=task.worker_type, status=TaskStatus.PENDING.value, execution_generation=0, created_at=now, updated_at=now) for task in tasks])
        return plan

    async def get(self, plan_id: str) -> tuple[Plan, list[Task]] | None:
        async with self._session_factory() as session:
            plan_row = await session.get(AgentPlanModel, plan_id)
            if plan_row is None:
                return None
            rows = (await session.execute(select(AgentTaskModel).where(AgentTaskModel.plan_id == plan_id).order_by(AgentTaskModel.task_id))).scalars().all()
        return self._plan(plan_row), [self._task(row) for row in rows]

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

    async def finish_task(self, task_id: str, execution_generation: int, *, status: str, output: dict[str, object] | None = None, error: dict[str, object] | None = None) -> dict[str, object]:
        async with self._session_factory() as session:
            async with session.begin():
                row = (await session.execute(select(AgentTaskModel).where(AgentTaskModel.task_id == task_id, AgentTaskModel.execution_generation == execution_generation).with_for_update())).scalar_one_or_none()
                if row is None or row.status != TaskStatus.RUNNING.value:
                    return {"accepted": False, "reason": "stale_execution"}
                row.status = status
                row.output_json = _dump(output) if output is not None else None
                row.error_json = _dump(error) if error is not None else None
                row.updated_at = _now()
                plan_row = await session.get(AgentPlanModel, row.plan_id, with_for_update=True)
                plan_status = await self._terminal_plan_status(session, row.plan_id)
                plan_completed = plan_status is not None
                if plan_row is not None:
                    plan_row.status = plan_status.value if plan_status else PlanStatus.RUNNING.value
                    plan_row.updated_at = _now()
                return {"accepted": True, "plan_completed": plan_completed}

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
        return Task(row.task_id, row.title, row.objective, _load(row.input_json, {}), _load(row.expected_output_json, {}), tuple(_load(row.allowed_tools_json, [])), row.worker_type)

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
        completed = plan.status in {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
        }
        return {"completed": completed, "results": results}


__all__ = ["PostgresOrchestrationRepository", "PostgresPlanLoader"]
