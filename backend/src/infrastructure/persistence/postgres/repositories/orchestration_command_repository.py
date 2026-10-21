"""以一个 PostgreSQL 事务接受首版 Plan 并冻结首轮任务。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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
    OrchestrationCommand,
    OrchestrationCommandType,
    Plan,
    PlanRound,
    PlanStatus,
    PlanVersion,
    RoundStatus,
    Task,
    TaskAttempt,
    ExecutionStatus,
    freeze_ready_round,
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


class PlanCommandRejected(ValueError):
    """当前 Run 状态或 Plan 事实不允许接受命令。"""


class PostgresOrchestrationCommandRepository:
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

                initial_states = {task.task_id: "pending" for task in tasks}
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
                    plan_json=json.dumps(
                        _legacy_plan_document(plan), ensure_ascii=False
                    ),
                    current_version=1,
                    active_round_id=round_snapshot.round_id,
                    cancellation_epoch=0,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
                session.add(plan_row)
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
                        status="pending",
                        execution_generation=1,
                        worker_thread_id=f"task:{task_id}:exec:1",
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
            }
            for task in tasks
        ],
        "edges": [
            {"from": edge.from_task_id, "to": edge.to_task_id}
            for edge in plan.edges
        ],
    }


def _legacy_plan_document(plan: Plan) -> dict[str, Any]:
    """保留现有只读 Plan 投影使用的 nodes/edges 形状。"""
    return {
        "nodes": [{"task_id": task_id} for task_id in plan.task_ids],
        "edges": [
            {"from": edge.from_task_id, "to": edge.to_task_id}
            for edge in plan.edges
        ],
    }


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
