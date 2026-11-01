"""Worker 注册与逐轮 Plan/Execution 的 PostgreSQL 事务仓储。"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

import jsonschema
import rfc8785
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from application.orchestration.worker_protocol_models import (
    WorkerCancelReceiptModel,
    WorkerCommandReceiptModel,
    WorkerTaskCommandModel,
    WorkerRegistrationResponseModel,
    WorkerResultReceiptModel as WorkerResultReceiptProtocolModel,
    WorkerResumeReceiptModel,
    WorkerStopConfirmationModel as WorkerStopConfirmationProtocolModel,
    WorkerStopConfirmationReceiptModel,
)
from domain.orchestration.worker_entities import (
    WorkerDispatchLease,
    WorkerDispatchStart,
    WorkerPlanReceipt,
    WorkerPlanSubmission,
    WorkerIdempotencyConflict,
    WorkerRegistrationIntent,
    WorkerRegistrationReceipt,
    WorkerResultSubmission,
    WorkerExecutionError,
    WorkerResultReceipt,
    WorkerStopConfirmation,
    WorkerStopReceipt,
    WorkerApprovalIntent,
    WorkerTaskStatus,
    TASK_TERMINAL_STATUSES,
)
from domain.orchestration import (
    RootExecutionAttemptFact,
    RootPlanFacts,
    RootPlanTaskFact,
    WorkerCapabilityRevision,
)
from domain.leases import LeaseGrant, LeaseKey
from domain.orchestration.worker_registry import WorkerInstancePrincipalMismatch
from ..models import (
    AgentRunModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    WorkerContractModel,
    WorkerInstanceModel,
    WorkerRegistrationReceiptModel,
    WorkerResultReceiptModel,
    WorkerStopConfirmationModel,
    WorkerApprovalRequestModel,
    WorkerResourceModel,
    ApprovalRecordModel,
    OrchestrationOutboxModel,
    SessionModel,
)
from .lease_center import PostgresLeaseCenter


class WorkerPlanRejected(ValueError):
    """当前 Run 状态不允许提交新 Plan。"""


class PostgresWorkerOrchestrationRepository:
    """以单事务维护 Worker Plan、执行结果与 Root 唤醒事实。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_execution_attempts: int = 3,
        execution_retry_initial_seconds: float = 2.0,
        execution_retry_max_seconds: float = 60.0,
        execution_retry_jitter_ratio: float = 0.2,
        queue_max_pending_tasks: int = 10000,
        resume_dispatch_max_wait_seconds: float = 300.0,
    ) -> None:
        """初始化仓储依赖。

        参数：
        - `session_factory`（async_sessionmaker[AsyncSession]）：提供 PostgreSQL 事务会话的工厂。
        - `id_factory`（Callable[[], str] | None）：生成持久对象 ID 的函数；默认使用 UUID。
        - `clock`（Callable[[], datetime] | None）：测试时注入的带时区时钟；生产默认读取 PostgreSQL 时间。
        - `max_execution_attempts`（int）：一个逻辑 Task 可执行的总尝试数；范围为 1 到 10。
        - `execution_retry_initial_seconds`（float）：执行失败后首次重试的退避基数，必须为正数。
        - `execution_retry_max_seconds`（float）：执行级指数退避上限，必须不小于首次间隔。
        - `execution_retry_jitter_ratio`（float）：相对退避基数的抖动比例，范围为 0–1。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self._session_factory = session_factory
        self._leases = PostgresLeaseCenter(session_factory)
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._clock = clock
        if not 1 <= max_execution_attempts <= 10:
            raise ValueError("worker_max_attempts_out_of_range")
        if queue_max_pending_tasks < 1:
            raise ValueError("worker_queue_max_pending_tasks_invalid")
        if not math.isfinite(resume_dispatch_max_wait_seconds) or not 1 <= resume_dispatch_max_wait_seconds <= 86400:
            raise ValueError("worker_resume_dispatch_max_wait_out_of_range")
        if (
            not math.isfinite(execution_retry_initial_seconds)
            or not math.isfinite(execution_retry_max_seconds)
            or execution_retry_initial_seconds <= 0
            or execution_retry_max_seconds < execution_retry_initial_seconds
            or not 0 <= execution_retry_jitter_ratio <= 1
        ):
            raise ValueError("worker_execution_retry_configuration_invalid")
        self._max_execution_attempts = max_execution_attempts
        self._execution_retry_initial = execution_retry_initial_seconds
        self._execution_retry_max = execution_retry_max_seconds
        self._execution_retry_jitter_ratio = execution_retry_jitter_ratio
        self._queue_max_pending_tasks = queue_max_pending_tasks
        self._resume_dispatch_max_wait_seconds = resume_dispatch_max_wait_seconds

    async def list_authorized_capabilities(
        self, run_id: str
    ) -> tuple[WorkerCapabilityRevision, ...]:
        """读取当前活动 Run 可使用的部署级健康 Worker 能力目录。

        Worker 注册是部署级授权；本方法不代表用户级工具或数据权限。
        """
        async with self._session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            if run is None or run.status != "running":
                return ()
            owner_session = await session.get(SessionModel, run.session_id)
            if (
                owner_session is None
                or owner_session.deleted_time is not None
                or owner_session.active_run_id != run_id
            ):
                return ()
            rows = (await session.execute(
                select(WorkerContractModel)
                .join(WorkerInstanceModel,
                    (WorkerInstanceModel.contract_id == WorkerContractModel.contract_id)
                    & (WorkerInstanceModel.contract_revision == WorkerContractModel.revision))
                .where(
                    WorkerInstanceModel.status == "ready",
                    WorkerInstanceModel.accepting_tasks.is_(True),
                )
                .order_by(WorkerContractModel.contract_id, WorkerContractModel.revision)
            )).scalars().all()
            unique = {(row.contract_id, row.revision): row for row in rows}
            return tuple(
                WorkerCapabilityRevision(
                    contract_id=row.contract_id,
                    revision=row.revision,
                    can_do=tuple(row.can_do_json),
                    cannot_do=tuple(row.cannot_do_json),
                    input_schema=row.input_schema_json,
                    output_schema=row.output_schema_json,
                )
                for _, row in sorted(unique.items())
            )

    async def read_completed_plan(
        self, run_id: str, plan_id: str, root_wait_generation: int
    ) -> RootPlanFacts | None:
        """按 Root 等待身份读取已关闭 Plan 的任务、尝试和可信资源引用。"""
        async with self._session_factory() as session:
            run = await session.get(AgentRunModel, run_id)
            plan = await session.get(OrchestratorPlanModel, plan_id)
            owner_session = (
                None if run is None else await session.get(SessionModel, run.session_id)
            )
            if (
                run is None or plan is None or plan.run_id != run_id
                or run.status != "running"
                or owner_session is None
                or owner_session.deleted_time is not None
                or owner_session.active_run_id != run_id
                or plan.status != "completed"
                or plan.root_wait_generation != root_wait_generation
                or run.root_wait_generation != root_wait_generation
                or run.orchestrator_plan_sequence != plan.sequence
            ):
                return None
            task_rows = (await session.scalars(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.plan_id == plan_id)
                .order_by(OrchestratorTaskModel.task_ref)
            )).all()
            task_facts: list[RootPlanTaskFact] = []
            for task in task_rows:
                executions = (await session.scalars(
                    select(OrchestratorExecutionModel)
                    .where(OrchestratorExecutionModel.task_id == task.task_id)
                    .order_by(OrchestratorExecutionModel.generation)
                )).all()
                resource_ids = tuple((await session.scalars(
                    select(WorkerResourceModel.resource_id)
                    .where(
                        WorkerResourceModel.task_id == task.task_id,
                        WorkerResourceModel.execution_id == task.active_execution_id,
                        WorkerResourceModel.run_id == run_id,
                        WorkerResourceModel.plan_id == plan_id,
                        WorkerResourceModel.status == "ready",
                    )
                    .order_by(WorkerResourceModel.resource_id)
                )).all())
                task_facts.append(RootPlanTaskFact(
                    task_ref=task.task_ref,
                    status=task.status,
                    output=task.output_json,
                    error=task.error_json,
                    objective=task.objective,
                    acceptance_criteria=task.acceptance_criteria,
                    attempts=tuple(
                        RootExecutionAttemptFact(
                            generation=execution.generation,
                            status=execution.status,
                            error=execution.error_json,
                            started_at=execution.started_at,
                            deadline_at=execution.deadline_at,
                            completed_at=execution.completed_at,
                        )
                        for execution in executions
                    ),
                    resource_ids=resource_ids,
                ))
            if len(task_facts) != plan.expected_task_count or plan.terminal_task_count != plan.expected_task_count:
                return None
            return RootPlanFacts(
                run_id=run.run_id,
                plan_id=plan.plan_id,
                sequence=plan.sequence,
                root_wait_generation=plan.root_wait_generation,
                goal=plan.goal,
                status=plan.status,
                tasks=tuple(task_facts),
            )

    async def claim_dispatch_command(
        self,
        *,
        owner_id: str,
        lease_seconds: float,
        command_types: tuple[str, ...] = ("execute", "cancel"),
    ) -> WorkerDispatchLease | None:
        """用短事务领取一条待发送命令，网络请求不占用数据库锁。

        参数：
        - `owner_id`（str）：当前 Dispatcher 进程实例身份。
        - `lease_seconds`（float）：数据库领取租约秒数，必须大于零。

        返回：一条带新 fencing token 的命令租约；队列为空时为 `None`。

        异常：ValueError 表示租约时长无效；数据库错误原样传播。
        """
        if not owner_id.strip() or lease_seconds <= 0 or not command_types:
            raise ValueError("worker_dispatch_lease_invalid")
        allowed_command_types = tuple(dict.fromkeys(command_types))
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(
                    OrchestratorWorkerCommandModel.command_type.in_(allowed_command_types),
                    OrchestratorWorkerCommandModel.status.in_(
                        ("awaiting_assignment", "pending", "retry", "dispatching")
                    ),
                    OrchestratorWorkerCommandModel.available_at <= now,
                    (OrchestratorWorkerCommandModel.lease_until.is_(None))
                    | (OrchestratorWorkerCommandModel.lease_until <= now),
                )
                .order_by(
                    OrchestratorWorkerCommandModel.available_at,
                    OrchestratorWorkerCommandModel.created_at,
                    OrchestratorWorkerCommandModel.command_id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if command is None:
                return None
            token = self._id_factory()
            command.status = "dispatching"
            command.lease_owner = owner_id
            command.lease_token = token
            command.lease_until = now + timedelta(seconds=lease_seconds)
            command.updated_at = now
            return WorkerDispatchLease(
                command_id=command.command_id,
                execution_id=command.execution_id,
                lease_owner=owner_id,
                lease_token=token,
                attempt=command.attempt,
                command=dict(command.command_json),
                command_type=command.command_type,
            )

    async def start_dispatch(
        self,
        lease: WorkerDispatchLease,
        *,
        lease_seconds: float,
        assignment_retry_seconds: float,
    ) -> WorkerDispatchStart | None:
        """原子分配健康 Worker 并预留容量；首次发送前不启动执行时钟。

        参数：
        - `lease`（WorkerDispatchLease）：刚从命令队列领取的 fencing 租约。
        - `lease_seconds`（float）：覆盖 Grant 构造及单次 HTTP 请求的剩余租约。
        - `assignment_retry_seconds`（float）：暂时没有匹配容量时让出队列的秒数。

        返回：可发送的执行和 Worker 路由快照；无健康容量时释放租约并返回 `None`。

        异常：ValueError 表示身份、状态或租约已失效；数据库错误原样传播。
        """
        async with self._session_factory() as session, session.begin():
            execution_hint = await session.get(
                OrchestratorExecutionModel, lease.execution_id
            )
            if execution_hint is None:
                return None
            task_hint = await session.get(OrchestratorTaskModel, execution_hint.task_id)
            if task_hint is None:
                return None
            plan_hint = await session.get(OrchestratorPlanModel, task_hint.plan_id)
            if plan_hint is None:
                return None
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if any(item is None for item in (run, plan, task, execution, command)):
                return None
            now = await self._now(session)
            if (
                command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return None
            if (
                task.status in {status.value for status in TASK_TERMINAL_STATUSES}
                or task.active_execution_id not in {None, execution.execution_id}
                or execution.status not in {"queued", "running"}
                or run.status != "running"
                or plan.status != "running"
            ):
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return None

            instance = None
            if execution.instance_id is not None:
                instance = await session.scalar(
                    select(WorkerInstanceModel)
                    .where(WorkerInstanceModel.instance_id == execution.instance_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            else:
                # 锁住候选实例后重新数容量；同一 Worker 的并发派发因行锁串行。
                reserved_count = (
                    select(func.count())
                    .select_from(OrchestratorExecutionModel)
                    .where(
                        OrchestratorExecutionModel.instance_id == WorkerInstanceModel.instance_id,
                        OrchestratorExecutionModel.capacity_reserved.is_(True),
                    )
                    .correlate(WorkerInstanceModel)
                    .scalar_subquery()
                )
                candidates = (
                    await session.scalars(
                        select(WorkerInstanceModel)
                        .where(
                            WorkerInstanceModel.contract_id == task.contract_id,
                            WorkerInstanceModel.contract_revision == task.contract_revision,
                            WorkerInstanceModel.status == "ready",
                            WorkerInstanceModel.accepting_tasks.is_(True),
                            WorkerInstanceModel.health_identity_valid.is_(True),
                            reserved_count < WorkerInstanceModel.max_concurrency,
                        )
                        .order_by(WorkerInstanceModel.instance_id)
                        .limit(8)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for candidate in candidates:
                    current_reserved = await session.scalar(
                        select(func.count()).where(
                            OrchestratorExecutionModel.instance_id == candidate.instance_id,
                            OrchestratorExecutionModel.capacity_reserved.is_(True),
                        )
                    )
                    if int(current_reserved or 0) < candidate.max_concurrency:
                        instance = candidate
                        break
            if instance is None or not instance.accepting_tasks or instance.status != "ready":
                command.status = "awaiting_assignment"
                command.available_at = now + timedelta(
                    seconds=max(0.1, assignment_retry_seconds)
                )
                self._clear_command_lease(command, now)
                return None
            if (
                instance.contract_id != task.contract_id
                or instance.contract_revision != task.contract_revision
            ):
                command.status = "awaiting_assignment"
                command.available_at = now + timedelta(
                    seconds=max(0.1, assignment_retry_seconds)
                )
                self._clear_command_lease(command, now)
                return None
            if execution.instance_id is None:
                execution.instance_id = instance.instance_id
                execution.execution_timeout_seconds = instance.execution_timeout_seconds
                execution.capacity_reserved = True
                execution.status = "running"
                task.active_execution_id = execution.execution_id
                task.execution_generation = execution.generation
                task.status = WorkerTaskStatus.RUNNING.value
            elif execution.started_at is None:
                # 分配恢复只保留原 Worker 和容量；耗时授权准备不占 Execution 时限。
                execution.capacity_reserved = True
                execution.status = "running"
                task.active_execution_id = execution.execution_id
                task.execution_generation = execution.generation
                task.status = WorkerTaskStatus.RUNNING.value
            command.instance_id = instance.instance_id
            command.lease_until = now + timedelta(seconds=lease_seconds)
            command.updated_at = now
            return WorkerDispatchStart(
                command_id=command.command_id,
                execution_id=execution.execution_id,
                instance_id=instance.instance_id,
                instance_key=instance.instance_key,
                service_principal=instance.service_principal,
                base_url=instance.base_url,
                command=dict(command.command_json),
                request_hash=command.request_hash,
                started_at=execution.started_at,
                deadline_at=execution.deadline_at,
            )

    async def prepare_cancel_dispatch(
        self, lease: WorkerDispatchLease, *, lease_seconds: float
    ) -> WorkerDispatchStart | None:
        """在当前 fencing 租约下准备一个取消命令的 HTTP 路由快照。

        参数：
        - `lease`（WorkerDispatchLease）：已领取的取消命令租约。
        - `lease_seconds`（float）：网络请求覆盖租约时长，必须大于零。

        返回：命令仍属于该租约且实例地址有效时返回发送快照，否则返回 `None`。

        异常：ValueError 表示租约失效；数据库错误原样传播。
        """
        if lease_seconds <= 0:
            raise ValueError("worker_dispatch_lease_invalid")
        async with self._session_factory() as session, session.begin():
            command_hint = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
            )
            if command_hint is None or command_hint.command_type != "cancel":
                return None
            execution_hint = await session.get(
                OrchestratorExecutionModel, lease.execution_id
            )
            if execution_hint is None:
                return None
            task_hint = await session.get(OrchestratorTaskModel, execution_hint.task_id)
            if task_hint is None:
                return None
            plan_hint = await session.get(OrchestratorPlanModel, task_hint.plan_id)
            if plan_hint is None:
                return None
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            now = await self._now(session)
            if (
                run is None
                or plan is None
                or task is None
                or execution is None
                or command is None
                or command.command_type != "cancel"
                or command.execution_id != execution.execution_id
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return None
            if execution.instance_id != command.instance_id:
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return None
            instance = await session.scalar(
                select(WorkerInstanceModel)
                .where(WorkerInstanceModel.instance_id == execution.instance_id)
                .execution_options(populate_existing=True)
            )
            if instance is None or instance.status == "disabled":
                command.status = "retry"
                command.available_at = now + timedelta(seconds=1)
                self._clear_command_lease(command, now)
                return None
            command.lease_until = now + timedelta(seconds=lease_seconds)
            command.updated_at = now
            return WorkerDispatchStart(
                command_id=command.command_id,
                execution_id=execution.execution_id,
                instance_id=instance.instance_id,
                instance_key=instance.instance_key,
                service_principal=instance.service_principal,
                base_url=instance.base_url,
                command=dict(command.command_json),
                request_hash=command.request_hash,
                started_at=execution.started_at,
                deadline_at=execution.deadline_at,
            )

    async def prepare_resume_dispatch(
        self, lease: WorkerDispatchLease, *, lease_seconds: float
    ) -> WorkerDispatchStart | None:
        """验证审批恢复命令的父级状态、原实例健康和发送租约。"""
        if lease_seconds <= 0:
            raise ValueError("worker_dispatch_lease_invalid")
        async with self._session_factory() as session, session.begin():
            command_hint = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
            )
            if command_hint is None or command_hint.command_type != "resume":
                return None
            execution_hint = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
            )
            task_hint = None if execution_hint is None else await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == execution_hint.task_id)
                .execution_options(populate_existing=True)
            )
            plan_hint = None if task_hint is None else await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
            )
            if execution_hint is None or task_hint is None or plan_hint is None:
                return None
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            now = await self._now(session)
            if (
                run is None
                or plan is None
                or task is None
                or execution is None
                or command is None
                or command.command_type != "resume"
                or command.execution_id != execution.execution_id
                or command.generation != execution.generation
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return None
            request_id = command.command_json.get("approval_request_id")
            request = await session.scalar(
                select(WorkerApprovalRequestModel)
                .where(
                    WorkerApprovalRequestModel.approval_request_id == request_id,
                    WorkerApprovalRequestModel.instance_id == command.instance_id,
                    WorkerApprovalRequestModel.execution_id == execution.execution_id,
                    WorkerApprovalRequestModel.pause_id
                    == command.command_json.get("pause_id"),
                )
                .execution_options(populate_existing=True)
            )
            if (
                request is None
                or request.execution_id != execution.execution_id
                or request.generation != execution.generation
                or request.cancellation_epoch != execution.cancellation_epoch
                or execution.current_approval_request_id != request.approval_request_id
                or execution.current_pause_id != request.pause_id
                or request.pause_id != command.command_json.get("pause_id")
                or request.resume_command_id != command.command_id
                or run.status != "running"
                or plan.status != "running"
                or task.active_execution_id != execution.execution_id
                or task.execution_generation != execution.generation
                or task.cancellation_epoch != execution.cancellation_epoch
                or execution.instance_id != command.instance_id
            ):
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return None
            first_send = "assignment_required" in command.command_json
            if first_send:
                if (
                    task.status != WorkerTaskStatus.WAITING_APPROVAL.value
                    or execution.status != WorkerTaskStatus.WAITING_APPROVAL.value
                    or execution.deadline_at is not None
                ):
                    command.status = "superseded"
                    self._clear_command_lease(command, now)
                    return None
            elif (
                task.status != WorkerTaskStatus.RUNNING.value
                or execution.status != WorkerTaskStatus.RUNNING.value
                or execution.deadline_at is None
                or execution.deadline_at <= now
                or not execution.capacity_reserved
            ):
                command.status = "awaiting_result"
                self._clear_command_lease(command, now)
                return None
            instance = await session.scalar(
                select(WorkerInstanceModel)
                .where(WorkerInstanceModel.instance_id == execution.instance_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if (
                instance is None
                or instance.status != "ready"
                or not instance.accepting_tasks
                or not instance.health_identity_valid
            ):
                command.status = "retry"
                command.available_at = now + timedelta(seconds=2)
                self._clear_command_lease(command, now)
                return None
            command.lease_until = now + timedelta(seconds=lease_seconds)
            if not first_send:
                command.delivery_uncertain_before_send = command.send_attempt_count > 0
                command.send_attempt_count += 1
            command.updated_at = now
            return WorkerDispatchStart(
                command_id=command.command_id,
                execution_id=execution.execution_id,
                instance_id=instance.instance_id,
                instance_key=instance.instance_key,
                service_principal=instance.service_principal,
                base_url=instance.base_url,
                command=dict(command.command_json),
                request_hash=command.request_hash,
                started_at=execution.started_at,
                deadline_at=execution.deadline_at,
            )

    async def prepare_first_resume_send(
        self,
        lease: WorkerDispatchLease,
        *,
        lease_seconds: float,
        command_builder: Callable[[WorkerDispatchStart], dict[str, Any]],
    ) -> WorkerDispatchStart | None:
        """在恢复首次发送前按剩余时长启动执行并冻结命令。"""
        async with self._session_factory() as session, session.begin():
            command_hint = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
            )
            execution_hint = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
            )
            if command_hint is None or execution_hint is None or command_hint.command_type != "resume":
                return None
            task_hint = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == execution_hint.task_id)
                .execution_options(populate_existing=True)
            )
            plan_hint = None if task_hint is None else await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
            )
            if task_hint is None or plan_hint is None:
                return None
            # 依次锁 Run、Plan、Task、Execution、Command，避免恢复与取消/结果事务交叉。
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            now = await self._now(session)
            if (
                run is None or plan is None or task is None or execution is None or command is None
                or command.command_type != "resume"
                or command.execution_id != execution.execution_id
                or command.generation != execution.generation
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
                or run.status != "running"
                or plan.status != "running"
                or task.status != WorkerTaskStatus.WAITING_APPROVAL.value
                or task.active_execution_id != execution.execution_id
                or task.execution_generation != execution.generation
                or task.cancellation_epoch != execution.cancellation_epoch
                or execution.status != WorkerTaskStatus.WAITING_APPROVAL.value
                or execution.instance_id is None
                or execution.deadline_at is not None
                or execution.remaining_seconds is None
                or execution.remaining_seconds <= 0
            ):
                return None
            request = await session.scalar(
                select(WorkerApprovalRequestModel)
                .where(
                    WorkerApprovalRequestModel.approval_request_id
                    == command.command_json.get("approval_request_id"),
                    WorkerApprovalRequestModel.instance_id == command.instance_id,
                    WorkerApprovalRequestModel.execution_id == execution.execution_id,
                    WorkerApprovalRequestModel.pause_id
                    == command.command_json.get("pause_id"),
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                request is None
                or request.resume_command_id != command.command_id
                or request.pause_id != command.command_json.get("pause_id")
                or request.execution_id != execution.execution_id
                or request.generation != execution.generation
                or request.cancellation_epoch != execution.cancellation_epoch
                or execution.current_approval_request_id != request.approval_request_id
                or execution.current_pause_id != request.pause_id
            ):
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return None
            instance = await session.scalar(
                select(WorkerInstanceModel)
                .where(WorkerInstanceModel.instance_id == execution.instance_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if (
                instance is None
                or instance.status != "ready"
                or not instance.accepting_tasks
                or not instance.health_identity_valid
                or instance.contract_id != task.contract_id
                or instance.contract_revision != task.contract_revision
            ):
                command.status = "retry"
                command.available_at = now + timedelta(seconds=2)
                self._clear_command_lease(command, now)
                return None
            reserved = await session.scalar(
                select(func.count())
                .select_from(OrchestratorExecutionModel)
                .where(
                    OrchestratorExecutionModel.instance_id == instance.instance_id,
                    OrchestratorExecutionModel.capacity_reserved.is_(True),
                    OrchestratorExecutionModel.execution_id != execution.execution_id,
                )
            )
            if int(reserved or 0) >= instance.max_concurrency:
                command.status = "retry"
                command.available_at = now + timedelta(seconds=2)
                self._clear_command_lease(command, now)
                return None
            execution.status = WorkerTaskStatus.RUNNING.value
            execution.started_at = now
            execution.deadline_at = now + timedelta(seconds=execution.remaining_seconds)
            execution.capacity_reserved = True
            execution.capacity_released_at = None
            task.status = WorkerTaskStatus.RUNNING.value
            request.resume_wait_deadline_at = None
            command.instance_id = instance.instance_id
            start = WorkerDispatchStart(
                command_id=command.command_id,
                execution_id=execution.execution_id,
                instance_id=instance.instance_id,
                instance_key=instance.instance_key,
                service_principal=instance.service_principal,
                base_url=instance.base_url,
                command=dict(command.command_json),
                request_hash=command.request_hash,
                started_at=execution.started_at,
                deadline_at=execution.deadline_at,
            )
            body = command_builder(start)
            command.command_json = body
            command.request_hash = self._hash_json(body)
            command.send_attempt_count += 1
            command.lease_until = now + timedelta(seconds=lease_seconds)
            command.updated_at = now
            return WorkerDispatchStart(
                command_id=start.command_id,
                execution_id=start.execution_id,
                instance_id=start.instance_id,
                instance_key=start.instance_key,
                service_principal=start.service_principal,
                base_url=start.base_url,
                command=body,
                request_hash=command.request_hash,
                started_at=execution.started_at,
                deadline_at=execution.deadline_at,
            )

    async def record_resume_receipt(
        self, lease: WorkerDispatchLease, receipt: WorkerResumeReceiptModel
    ) -> bool:
        """保存 ResumeReceipt 并释放当前发送租约。"""
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            execution_hint = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
            )
            task_hint = None if execution_hint is None else await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == execution_hint.task_id)
                .execution_options(populate_existing=True)
            )
            plan_hint = None if task_hint is None else await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
            )
            run = None if plan_hint is None else await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            plan = None if plan_hint is None else await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            task = None if task_hint is None else await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            execution = None if execution_hint is None else await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == execution_hint.execution_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            body = {} if command is None else command.command_json
            request = None
            if command is not None and body.get("approval_request_id"):
                request = await session.scalar(
                    select(WorkerApprovalRequestModel)
                    .where(
                        WorkerApprovalRequestModel.approval_request_id
                        == body["approval_request_id"],
                        WorkerApprovalRequestModel.instance_id == command.instance_id,
                        WorkerApprovalRequestModel.execution_id == execution.execution_id,
                        WorkerApprovalRequestModel.pause_id == body.get("pause_id"),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            if command is None or command.command_type != "resume":
                return False
            if (
                command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
                or run is None
                or plan is None
                or task is None
                or execution is None
                or request is None
                or run.status != "running"
                or plan.status != "running"
                or task.active_execution_id != execution.execution_id
                or task.execution_generation != execution.generation
                or execution.instance_id != command.instance_id
                or execution.generation != command.generation
                or execution.status != WorkerTaskStatus.RUNNING.value
                or execution.cancellation_epoch != int(body.get("cancellation_epoch", -1))
                or request.resume_command_id != command.command_id
                or request.pause_id != body.get("pause_id")
                or execution.current_approval_request_id != request.approval_request_id
                or execution.current_pause_id != request.pause_id
            ):
                return False
            if str(receipt.command_id) != command.command_id:
                raise ValueError("worker_resume_receipt_identity_mismatch")
            command.status = "accepted"
            command.receipt_json = receipt.model_dump(mode="json", exclude_none=True)
            self._clear_command_lease(command, await self._now(session))
            return True

    async def record_cancel_receipt(
        self, lease: WorkerDispatchLease, receipt: WorkerCancelReceiptModel
    ) -> bool:
        """记录取消命令的 HTTP 收件回执，保留 StopConfirmation 独立语义。

        参数：
        - `lease`（WorkerDispatchLease）：发送取消命令时持有的 fencing 租约。
        - `receipt`（WorkerCancelReceiptModel）：Worker 返回的 202 收件回执。

        返回：租约仍有效且回执被持久化时返回 `True`，停止确认已经先收敛或租约失效时返回 `False`。

        异常：ValueError 表示回执没有完整匹配已冻结的取消命令身份。
        """
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .with_for_update()
            )
            if (
                command is None
                or command.command_type != "cancel"
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            command_body = command.command_json
            if (
                str(receipt.command_id) != command.command_id
                or str(receipt.execution_id) != command.execution_id
                or receipt.generation != command.generation
                or receipt.cancellation_epoch
                != int(command_body.get("cancellation_epoch", -1))
                or receipt.reason != command_body.get("reason")
                or receipt.accepted is not True
                or receipt.stop_confirmation_required is not True
            ):
                raise ValueError("worker_cancel_receipt_identity_mismatch")
            command.status = "accepted"
            command.receipt_json = receipt.model_dump(mode="json", exclude_none=True)
            self._clear_command_lease(command, now)
            return True

    async def prepare_dispatch_send(
        self, lease: WorkerDispatchLease, *, lease_seconds: float
    ) -> bool:
        """紧贴 HTTP 发送前用数据库时钟检查 deadline 并续足本次请求租约。"""
        async with self._session_factory() as session, session.begin():
            execution_hint = await session.get(
                OrchestratorExecutionModel, lease.execution_id
            )
            if execution_hint is None:
                return False
            task_hint = await session.get(
                OrchestratorTaskModel, execution_hint.task_id
            )
            if task_hint is None:
                return False
            plan_hint = await session.get(OrchestratorPlanModel, task_hint.plan_id)
            if plan_hint is None:
                return False
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            now = await self._now(session)
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if (
                command is None
                or run is None
                or plan is None
                or task is None
                or execution is None
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            if (
                run.status != "running"
                or plan.status != "running"
                or task.status in {status.value for status in TASK_TERMINAL_STATUSES}
                or task.active_execution_id != execution.execution_id
                or task.execution_generation != execution.generation
                or execution.status != "running"
                or task.cancellation_epoch != execution.cancellation_epoch
            ):
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return False
            if execution.deadline_at is None or execution.deadline_at <= now:
                command.status = "awaiting_result"
                self._clear_command_lease(command, now)
                return False
            command.delivery_uncertain_before_send = command.send_attempt_count > 0
            command.send_attempt_count += 1
            command.lease_until = now + timedelta(seconds=lease_seconds)
            command.updated_at = now
            return True

    async def prepare_first_dispatch_send(
        self,
        lease: WorkerDispatchLease,
        *,
        lease_seconds: float,
        command_builder: Callable[[WorkerDispatchStart], dict[str, Any]],
    ) -> WorkerDispatchStart | None:
        """在短事务内启动执行时钟、冻结最终命令并持久化首次发送意图。

        参数：
        - `lease`（WorkerDispatchLease）：当前命令 owner 与 fencing token。
        - `lease_seconds`（float）：覆盖后续有界 HTTP 请求的数据库命令租约时长，必须大于零。
        - `command_builder`（Callable）：仅执行本地 Grant 签名和 DTO/JCS 内容组装，不得进行 I/O。

        返回：deadline、最终 TaskCommand 和发送意图已在同一事务提交时返回发送快照；租约或执行已失效时返回 `None`。

        异常：ValueError 表示租约、命令内容或执行状态无效；数据库错误原样传播。
        """
        if lease_seconds <= 0:
            raise ValueError("worker_dispatch_lease_invalid")
        async with self._session_factory() as session, session.begin():
            execution_hint = await session.get(
                OrchestratorExecutionModel, lease.execution_id
            )
            if execution_hint is None:
                return None
            task_hint = await session.get(
                OrchestratorTaskModel, execution_hint.task_id
            )
            if task_hint is None:
                return None
            plan_hint = await session.get(
                OrchestratorPlanModel, task_hint.plan_id
            )
            if plan_hint is None:
                return None
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(
                    OrchestratorExecutionModel.execution_id == lease.execution_id
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if execution is None or execution.instance_id is None:
                return None
            instance = await session.scalar(
                select(WorkerInstanceModel)
                .where(WorkerInstanceModel.instance_id == execution.instance_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            now = await self._now(session)
            if (
                run is None
                or plan is None
                or task is None
                or instance is None
                or command is None
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return None
            if (
                run.status != "running"
                or plan.status != "running"
                or task.status in {status.value for status in TASK_TERMINAL_STATUSES}
                or task.active_execution_id != execution.execution_id
                or task.execution_generation != execution.generation
                or execution.status != "running"
                or task.cancellation_epoch != execution.cancellation_epoch
                or not execution.capacity_reserved
            ):
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return None
            if execution.started_at is None:
                if execution.deadline_at is not None:
                    raise RuntimeError("worker_dispatch_started_at_missing")
                timeout_seconds = execution.execution_timeout_seconds
                if timeout_seconds is None or timeout_seconds <= 0:
                    raise RuntimeError("worker_dispatch_timeout_missing")
                execution.started_at = now
                execution.deadline_at = now + timedelta(seconds=timeout_seconds)
                execution.updated_at = now
            elif execution.deadline_at is None:
                raise RuntimeError("worker_dispatch_deadline_missing")
            if execution.deadline_at <= now:
                command.status = "awaiting_result"
                self._clear_command_lease(command, now)
                return None
            if (
                command.instance_id != instance.instance_id
                or instance.contract_id != task.contract_id
                or instance.contract_revision != task.contract_revision
            ):
                command.status = "awaiting_assignment"
                command.available_at = now + timedelta(seconds=0.1)
                self._clear_command_lease(command, now)
                return None
            command.instance_id = instance.instance_id
            start = WorkerDispatchStart(
                command_id=command.command_id,
                execution_id=execution.execution_id,
                instance_id=instance.instance_id,
                instance_key=instance.instance_key,
                service_principal=instance.service_principal,
                base_url=instance.base_url,
                command=dict(command.command_json),
                request_hash=command.request_hash,
                started_at=execution.started_at,
                deadline_at=execution.deadline_at,
            )
            command_body = command_builder(start)
            raw_hash = self._hash_json(command_body)
            if "assignment_required" not in command.command_json:
                if command.request_hash != raw_hash:
                    raise ValueError("worker_dispatch_command_body_immutable")
            else:
                command.command_json = command_body
                command.request_hash = raw_hash
            command.delivery_uncertain_before_send = command.send_attempt_count > 0
            command.send_attempt_count += 1
            command.lease_until = now + timedelta(seconds=lease_seconds)
            command.updated_at = now
            return WorkerDispatchStart(
                command_id=start.command_id,
                execution_id=start.execution_id,
                instance_id=start.instance_id,
                instance_key=start.instance_key,
                service_principal=start.service_principal,
                base_url=start.base_url,
                command=command_body,
                request_hash=command.request_hash,
                started_at=execution.started_at,
                deadline_at=execution.deadline_at,
            )

    async def freeze_dispatch_body(
        self,
        lease: WorkerDispatchLease,
        command_body: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        """首次发送前以同一 fencing token 冻结 TaskCommand 和 JCS 摘要。

        参数：
        - `lease`（WorkerDispatchLease）：当前命令领取权。
        - `command_body`（dict[str, Any]）：已按严格 TaskCommand DTO 序列化的最终 body。

        返回：数据库内的不可变命令 body 及其 JCS SHA-256。

        异常：ValueError 表示领取权失效或已发送 body 不匹配。
        """
        raw_hash = self._hash_json(command_body)
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .with_for_update()
            )
            if (
                command is None
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                raise ValueError("worker_dispatch_lease_lost")
            if "assignment_required" not in command.command_json:
                if command.request_hash != raw_hash:
                    raise ValueError("worker_dispatch_command_body_immutable")
                return dict(command.command_json), command.request_hash
            command.command_json = command_body
            command.request_hash = raw_hash
            command.updated_at = now
            return dict(command_body), raw_hash

    async def record_dispatch_receipt(
        self, lease: WorkerDispatchLease, receipt: WorkerCommandReceiptModel
    ) -> bool:
        """仅由当前租约保存合法收件回执，且不覆盖已处理结果。"""
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .with_for_update()
            )
            if (
                command is None
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            if str(receipt.command_id) != command.command_id:
                raise ValueError("worker_dispatch_receipt_identity_mismatch")
            command.status = "accepted"
            command.receipt_json = receipt.model_dump(mode="json", exclude_none=True)
            self._clear_command_lease(command, now)
            return True

    async def retry_dispatch_command(
        self,
        lease: WorkerDispatchLease,
        *,
        delay_seconds: float,
    ) -> bool:
        """按数据库时钟安排同命令重投，或在 deadline 前停止发送。

        参数：
        - `lease`（WorkerDispatchLease）：当前命令的 owner 与 fencing token。
        - `delay_seconds`（float）：退避时长；必须是有限非负秒数。

        返回：命令仍有效且已安排重投或转入等待结果时返回 `True`；租约失效时返回 `False`。

        异常：ValueError 表示退避时长无效；数据库错误原样传播。
        """
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("worker_dispatch_retry_delay_invalid")
        async with self._session_factory() as session, session.begin():
            execution_hint = await session.get(
                OrchestratorExecutionModel, lease.execution_id
            )
            if execution_hint is None:
                return False
            task_hint = await session.get(
                OrchestratorTaskModel, execution_hint.task_id
            )
            if task_hint is None:
                return False
            plan_hint = await session.get(
                OrchestratorPlanModel, task_hint.plan_id
            )
            if plan_hint is None:
                return False
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(
                    OrchestratorExecutionModel.execution_id == lease.execution_id
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            now = await self._now(session)
            if (
                run is None
                or plan is None
                or task is None
                or execution is None
                or command is None
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            if lease.command_type == "cancel":
                invalid_command = (
                    command.command_type != "cancel"
                    or command.instance_id != execution.instance_id
                )
            elif lease.command_type == "resume":
                request_id = command.command_json.get("approval_request_id")
                pause_id = command.command_json.get("pause_id")
                request = await session.scalar(
                    select(WorkerApprovalRequestModel)
                    .where(
                        WorkerApprovalRequestModel.approval_request_id == request_id,
                        WorkerApprovalRequestModel.instance_id == command.instance_id,
                        WorkerApprovalRequestModel.execution_id == execution.execution_id,
                        WorkerApprovalRequestModel.pause_id == pause_id,
                    )
                    .execution_options(populate_existing=True)
                )
                current_pause = (
                    request is not None
                    and request.resume_command_id == command.command_id
                    and execution.current_approval_request_id == request.approval_request_id
                    and execution.current_pause_id == request.pause_id
                    and request.generation == execution.generation
                    and request.cancellation_epoch == execution.cancellation_epoch
                    and run.status == "running"
                    and plan.status == "running"
                    and task.active_execution_id == execution.execution_id
                    and task.execution_generation == execution.generation
                    and task.cancellation_epoch == execution.cancellation_epoch
                )
                if "assignment_required" in command.command_json:
                    invalid_command = not (
                        current_pause
                        and task.status == WorkerTaskStatus.WAITING_APPROVAL.value
                        and execution.status == WorkerTaskStatus.WAITING_APPROVAL.value
                        and execution.deadline_at is None
                    )
                else:
                    invalid_command = not (
                        current_pause
                        and task.status == WorkerTaskStatus.RUNNING.value
                        and execution.status == WorkerTaskStatus.RUNNING.value
                        and execution.deadline_at is not None
                        and execution.deadline_at > now
                        and execution.capacity_reserved
                    )
            else:
                invalid_command = (
                    command.command_type != "execute"
                    or run.status != "running"
                    or plan.status != "running"
                    or task.status in {status.value for status in TASK_TERMINAL_STATUSES}
                    or task.active_execution_id != execution.execution_id
                    or task.execution_generation != execution.generation
                    or execution.status != "running"
                    or task.cancellation_epoch != execution.cancellation_epoch
                )
            if invalid_command:
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return False
            next_available_at = now + timedelta(seconds=delay_seconds)
            if lease.command_type in {"execute", "resume"} and (
                execution.deadline_at is None or next_available_at >= execution.deadline_at
            ):
                if lease.command_type == "execute" or "assignment_required" not in command.command_json:
                    command.status = "awaiting_result"
                    self._clear_command_lease(command, now)
                    return True
            command.attempt += 1
            command.status = "retry"
            command.available_at = next_available_at
            self._clear_command_lease(command, now)
            return True

    async def exhaust_dispatch_retries(self, lease: WorkerDispatchLease) -> bool:
        """停止传输重投但保留可能已接收 Execution 的容量与 deadline。"""
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .with_for_update()
            )
            if (
                command is None
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            command.status = "awaiting_result"
            self._clear_command_lease(command, now)
            return True

    async def await_dispatch_result(self, lease: WorkerDispatchLease) -> bool:
        """响应无法按目录判定时停止重投并保留 Execution 容量等待结果或 deadline。"""
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .with_for_update()
            )
            if (
                command is None
                or command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            command.status = "awaiting_result"
            self._clear_command_lease(command, now)
            return True

    async def fail_dispatch_command(
        self,
        lease: WorkerDispatchLease,
        *,
        error_code: str,
        message: str,
        root_replan: bool,
    ) -> bool:
        """将明确未收件的永久指令错误原子收口到 Task 与 Plan。"""
        async with self._session_factory() as session, session.begin():
            execution_hint = await session.get(
                OrchestratorExecutionModel, lease.execution_id
            )
            if execution_hint is None:
                return False
            task_hint = await session.get(OrchestratorTaskModel, execution_hint.task_id)
            if task_hint is None:
                return False
            plan_hint = await session.get(OrchestratorPlanModel, task_hint.plan_id)
            if plan_hint is None:
                return False
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == lease.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(OrchestratorWorkerCommandModel.command_id == lease.command_id)
                .with_for_update()
            )
            if any(item is None for item in (run, plan, task, execution, command)):
                return False
            now = await self._now(session)
            if (
                command.status != "dispatching"
                or command.lease_owner != lease.lease_owner
                or command.lease_token != lease.lease_token
                or command.lease_until is None
                or command.lease_until <= now
            ):
                return False
            if command.delivery_uncertain_before_send:
                # 旧请求可能已被接收；只停止重投，保留容量等待结果或 S5 deadline。
                command.status = "awaiting_result"
                self._clear_command_lease(command, now)
                return False
            # 同命令可能已被 Worker 接收并先回传结果；终态结果不可回退。
            if (
                execution.status != "running"
                or task.active_execution_id != execution.execution_id
                or task.status in {status.value for status in TASK_TERMINAL_STATUSES}
            ):
                command.status = "superseded"
                self._clear_command_lease(command, now)
                return False
            execution.status = "failed"
            execution.completed_at = execution.completed_at or now
            execution.capacity_reserved = False
            execution.capacity_released_at = now
            execution.error_json = {"code": error_code, "message": message}
            execution.updated_at = now
            task.status = WorkerTaskStatus.FAILED.value
            task.error_json = {
                "code": error_code,
                "message": message,
                "directive": "root_replan" if root_replan else "operator_action",
            }
            task.updated_at = now
            plan.terminal_task_count += 1
            if plan.terminal_task_count >= plan.expected_task_count:
                plan.status = "completed"
                plan.completed_at = now
                if run.status == "running":
                    await self._ensure_root_resume_job(
                        session,
                        run_id=run.run_id,
                        plan_id=plan.plan_id,
                        root_wait_generation=plan.root_wait_generation,
                        now=now,
                    )
            command.status = "failed"
            self._clear_command_lease(command, now)
            return True

    @staticmethod
    def _clear_command_lease(
        command: OrchestratorWorkerCommandModel, now: datetime
    ) -> None:
        """清除当前 fencing 租约并更新时间。"""
        command.lease_owner = None
        command.lease_token = None
        command.lease_until = None
        command.updated_at = now

    async def _has_queued_task_capacity(
        self, session: AsyncSession, *, additional_tasks: int
    ) -> bool:
        """在统一队列锁内检查待派发逻辑任务是否仍有容量。

        参数：
        - `session`（AsyncSession）：当前已经开启事务的 PostgreSQL 会话。
        - `additional_tasks`（int）：本次事务准备新增的 queued Task 数量，必须非负。

        返回：获取锁后统计到的 queued Task 加新增数量未超过配置上限时返回 `True`。

        异常：ValueError 表示新增数量为负数；数据库错误原样传播。
        """
        if additional_tasks < 0:
            raise ValueError("worker_queue_capacity_increment_invalid")
        # submit_plan 与重试都按同一顺序先锁所属 Run，再获取此事务级队列锁，
        # 使“统计现状”和“插入新 Execution”成为一个不可交错的决策。
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": "worker-queued-task-capacity"},
        )
        queued_task_count = await session.scalar(
            select(func.count(func.distinct(OrchestratorTaskModel.task_id)))
            .join(
                OrchestratorExecutionModel,
                OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
            )
            .where(
                OrchestratorExecutionModel.status == "queued",
                OrchestratorTaskModel.status.in_(
                    (WorkerTaskStatus.PENDING.value, WorkerTaskStatus.RETRY_PENDING.value)
                ),
            )
        )
        return int(queued_task_count or 0) + additional_tasks <= self._queue_max_pending_tasks

    async def find_registration_receipt(
        self,
        service_principal: str,
        registration_request_id: str,
        request_hash: str,
    ) -> WorkerRegistrationReceipt | None:
        """只读查找注册回执并比较原始请求摘要。

        参数：
        - `service_principal`（str）：注册请求所属的认证主体。
        - `registration_request_id`（str）：HTTP 请求携带的稳定注册 ID。
        - `request_hash`（str）：原始 JSON 的 RFC 8785 SHA-256 摘要。

        返回：不存在时为 `None`；匹配时返回原回执。

        异常：WorkerIdempotencyConflict 表示该主体已用相同 ID 持久化不同内容。
        """
        async with self._session_factory() as session:
            prior = await session.scalar(
                select(WorkerRegistrationReceiptModel).where(
                    WorkerRegistrationReceiptModel.service_principal == service_principal,
                    WorkerRegistrationReceiptModel.registration_request_id
                    == registration_request_id,
                )
            )
        if prior is None:
            return None
        if prior.request_hash != request_hash:
            raise WorkerIdempotencyConflict("worker_registration_idempotency_conflict")
        response = prior.response_json
        return WorkerRegistrationReceipt(
            instance_id=str(response["worker_instance_id"]),
            instance_key=str(response["worker_instance_key"]),
            contract_id=str(response["contract_id"]),
            contract_revision=int(response["contract_revision"]),
            registered_at=datetime.fromisoformat(response["registered_at"]),
            replayed=True,
        )

    async def submit_approval_request(
        self, intent: WorkerApprovalIntent, *, instance_id: str
    ) -> dict[str, Any]:
        """持久接收审批批次并在同一事务暂停当前 Worker Execution。"""
        async with self._session_factory() as session, session.begin():
            # 先从 Execution 取得真实父级身份，再按照 Run→Plan→Task→Execution
            # 锁定。请求体中的父级 ID 只能在锁后作为一致性声明复验，不能决定锁定对象。
            execution_hint = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == intent.execution_id)
                .execution_options(populate_existing=True)
            )
            if execution_hint is None:
                raise ValueError("worker_approval_execution_unknown")
            task_hint = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == execution_hint.task_id)
                .execution_options(populate_existing=True)
            )
            plan_hint = None if task_hint is None else await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
            )
            if task_hint is None or plan_hint is None:
                raise ValueError("worker_approval_execution_unknown")
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == intent.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            instance = await session.scalar(
                select(WorkerInstanceModel)
                .where(WorkerInstanceModel.instance_id == instance_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if any(item is None for item in (run, plan, task, execution, instance)):
                raise ValueError("worker_approval_execution_identity_unknown")

            if (
                intent.run_id != run.run_id
                or intent.plan_id != plan.plan_id
                or intent.task_id != task.task_id
                or execution.task_id != task.task_id
                or task.plan_id != plan.plan_id
                or plan.run_id != run.run_id
                or intent.execution_id != execution.execution_id
            ):
                raise ValueError("worker_approval_execution_identity_mismatch")

            prior = await session.scalar(
                select(WorkerApprovalRequestModel)
                .where(
                    WorkerApprovalRequestModel.instance_id == instance_id,
                    WorkerApprovalRequestModel.approval_request_id
                    == intent.approval_request_id,
                )
                .with_for_update()
            )
            if prior is not None:
                if prior.request_hash != intent.request_hash:
                    raise WorkerIdempotencyConflict(
                        "worker_approval_request_idempotency_conflict"
                    )
                replay = dict(prior.receipt_json)
                replay["disposition"] = "duplicate"
                return replay

            now = await self._now(session)
            if (
                execution.instance_id != instance_id
                or execution.generation != intent.generation
                or task.execution_generation != intent.generation
                or task.active_execution_id != intent.execution_id
                or task.cancellation_epoch != intent.cancellation_epoch
                or execution.cancellation_epoch != intent.cancellation_epoch
                or task.status != "running"
                or execution.status != "running"
                or run.status != "running"
                or plan.status != "running"
                or execution.deadline_at is None
                or execution.deadline_at <= now
            ):
                raise ValueError("worker_approval_execution_not_eligible")
            remaining_seconds = (execution.deadline_at - now).total_seconds()
            if remaining_seconds <= 0:
                raise ValueError("worker_approval_execution_deadline_exceeded")

            if execution.current_approval_request_id is not None:
                old_commands = (
                    await session.scalars(
                        select(OrchestratorWorkerCommandModel)
                        .where(
                            OrchestratorWorkerCommandModel.execution_id
                            == execution.execution_id,
                            OrchestratorWorkerCommandModel.command_type == "resume",
                            OrchestratorWorkerCommandModel.command_json[
                                "approval_request_id"
                            ].as_string()
                            == execution.current_approval_request_id,
                            OrchestratorWorkerCommandModel.status.in_(
                                ("pending", "retry", "dispatching", "accepted")
                            ),
                        )
                        .with_for_update()
                    )
                ).all()
                for old_command in old_commands:
                    old_command.status = "superseded"
                    self._clear_command_lease(old_command, now)
                    old_command.updated_at = now

            batch_id = self._id_factory() if len(intent.items) > 1 else None
            expires_at = now + timedelta(seconds=120)
            receipt_id = self._id_factory()
            item_receipts: list[dict[str, str]] = []
            for item in intent.items:
                approval_id = self._id_factory()
                item_receipts.append(
                    {
                        "item_id": item.item_id,
                        "approval_id": approval_id,
                        "arguments_sha256": item.arguments_sha256,
                    }
                )
                session.add(
                    ApprovalRecordModel(
                        approval_id=approval_id,
                        session_id=run.session_id,
                        run_id=run.run_id,
                        approval_batch_id=batch_id,
                        approval_request_id=intent.approval_request_id,
                        pause_id=intent.pause_id,
                        item_id=item.item_id,
                        plan_id=plan.plan_id,
                        task_id=task.task_id,
                        execution_id=execution.execution_id,
                        generation=execution.generation,
                        cancellation_epoch=task.cancellation_epoch,
                        # 远程审批身份由 request/pause/item 唯一确定，不复用旧
                        # LangGraph worker_thread/interrupt 唯一键。
                        worker_thread_id=None,
                        interrupt_id=None,
                        tool_call_id=item.item_id,
                        tool_name=item.tool_name,
                        arguments_json=item.arguments,
                        risk_level=item.risk_level,
                        args_hash=item.arguments_sha256,
                        arguments_sha256=item.arguments_sha256,
                        operation_key=f"{execution.execution_id}:{item.item_id}",
                        status="pending",
                        decision=None,
                        created_at=now,
                        expires_at=expires_at,
                        decided_at=None,
                    )
                )
                session.add(
                    OrchestrationOutboxModel(
                        message_id=self._id_factory(),
                        dedupe_key=f"worker-approval:{intent.approval_request_id}:{item.item_id}",
                        event_type="ApprovalRequested",
                        correlation_id=run.run_id,
                        plan_id=plan.plan_id,
                        plan_version=plan.sequence,
                        round_id=None,
                        payload_json={
                            "approval_id": approval_id,
                            "approval_request_id": intent.approval_request_id,
                            "pause_id": intent.pause_id,
                            "item_id": item.item_id,
                            "task_id": task.task_id,
                            "execution_id": execution.execution_id,
                            "tool_name": item.tool_name,
                            "summary": item.summary,
                            "risk_level": item.risk_level,
                        },
                        schema_version=1,
                        status="pending",
                        attempt=0,
                        available_at=now,
                        occurred_at=now,
                    )
                )

            execution.remaining_seconds = remaining_seconds
            execution.deadline_at = None
            execution.status = "waiting_approval"
            execution.current_approval_request_id = intent.approval_request_id
            execution.current_pause_id = intent.pause_id
            execution.capacity_reserved = False
            execution.capacity_released_at = now
            execution.updated_at = now
            task.status = "waiting_approval"
            task.updated_at = now
            receipt_data = {
                "protocol_version": "1.0",
                "approval_request_id": intent.approval_request_id,
                "pause_id": intent.pause_id,
                "approval_batch_id": batch_id,
                "disposition": "accepted",
                "status": "accepted",
                "expires_at": expires_at.isoformat(),
                "items": item_receipts,
                "received_at": now.isoformat(),
            }
            # 先插回执聚合，再由重试请求读取相同 item→approval 映射。
            session.add(
                WorkerApprovalRequestModel(
                    receipt_id=receipt_id,
                    instance_id=instance_id,
                    approval_request_id=intent.approval_request_id,
                    pause_id=intent.pause_id,
                    run_id=run.run_id,
                    plan_id=plan.plan_id,
                    task_id=task.task_id,
                    execution_id=execution.execution_id,
                    generation=execution.generation,
                    cancellation_epoch=task.cancellation_epoch,
                    request_hash=intent.request_hash,
                    request_json={
                        "items": [
                            {
                                "item_id": item.item_id,
                                "tool_name": item.tool_name,
                                "summary": item.summary,
                                "risk_level": item.risk_level,
                                "arguments": item.arguments,
                                "arguments_sha256": item.arguments_sha256,
                            }
                            for item in intent.items
                        ]
                    },
                    status="pending",
                    expires_at=expires_at,
                    received_at=now,
                    receipt_json=receipt_data,
                    resume_command_id=None,
                    resume_wait_deadline_at=None,
                )
            )
            return receipt_data

    async def submit_plan(
        self, submission: WorkerPlanSubmission
    ) -> WorkerPlanReceipt:
        return await self._submit_plan(submission, root_lease=None)

    async def submit_root_plan(
        self, submission: WorkerPlanSubmission, *, lease: LeaseGrant
    ) -> WorkerPlanReceipt:
        """以当前 Root execution fencing lease 提交一轮 Plan。"""
        if lease.key != LeaseKey("root_execution", submission.run_id):
            raise WorkerPlanRejected("root_execution_lease_mismatch")
        return await self._submit_plan(submission, root_lease=lease)

    async def _submit_plan(
        self, submission: WorkerPlanSubmission, *, root_lease: LeaseGrant | None
    ) -> WorkerPlanReceipt:
        """校验并原子保存 Run 的一轮 Plan 及其无依赖任务。

        参数：
        - `submission`（WorkerPlanSubmission）：完整的 Plan、幂等键和任务输入。

        返回：首次接受或相同请求重放的 `WorkerPlanReceipt`。

        异常：
        - WorkerIdempotencyConflict：同一 Run 幂等键对应不同 Plan 内容时抛出。
        - WorkerPlanRejected：Run 已停止、序号冲突或任务契约版本不存在时抛出。
        """
        request_hash = self._plan_hash(submission)
        async with self._session_factory() as session, session.begin():
            # 重放先查原回执，使 Run 后续停止或 Plan 已完成不改变旧提交的结果。
            session_row = None
            if root_lease is not None:
                # Root 提交遵循 Session → Run → Plan 的锁序，并确认仍属于当前活动会话。
                session_id = await session.scalar(
                    select(AgentRunModel.session_id).where(
                        AgentRunModel.run_id == submission.run_id
                    )
                )
                if session_id is None:
                    raise WorkerPlanRejected("worker_run_unknown")
                session_row = await session.scalar(
                    select(SessionModel)
                    .where(SessionModel.id == session_id)
                    .with_for_update()
                )
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == submission.run_id)
                .with_for_update()
            )
            if run is None:
                raise WorkerPlanRejected("worker_run_unknown")
            if root_lease is not None:
                if (
                    session_row is None
                    or session_row.deleted_time is not None
                    or session_row.active_run_id != run.run_id
                ):
                    raise WorkerPlanRejected("worker_run_session_not_active")
                await self._leases.assert_current_in_transaction(session, root_lease)
            # Run行锁串行化首次提交；重查必须早于停止状态和新序号校验。
            existing = await session.scalar(
                select(OrchestratorPlanModel).where(
                    OrchestratorPlanModel.run_id == submission.run_id,
                    OrchestratorPlanModel.idempotency_key == submission.idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise WorkerIdempotencyConflict("worker_plan_idempotency_conflict")
                return WorkerPlanReceipt(
                    plan_id=existing.plan_id,
                    run_id=existing.run_id,
                    sequence=existing.sequence,
                    accepted=True,
                    replayed=True,
                )
            if run.status != "running":
                raise WorkerPlanRejected("worker_run_not_accepting_plans")
            if submission.sequence != run.orchestrator_plan_sequence + 1:
                raise WorkerPlanRejected("worker_plan_sequence_conflict")

            # 所有 Run 共用一个待派发容量上限；事务锁把计数与整轮插入串行化。
            if not await self._has_queued_task_capacity(
                session, additional_tasks=len(submission.tasks)
            ):
                raise WorkerPlanRejected("worker_queue_capacity_exceeded")

            contract_keys = {
                (task.contract_id, task.contract_revision) for task in submission.tasks
            }
            contract_result = await session.execute(
                select(
                    WorkerContractModel.contract_id,
                    WorkerContractModel.revision,
                    WorkerContractModel.input_schema_json,
                ).where(
                    tuple_(WorkerContractModel.contract_id, WorkerContractModel.revision).in_(
                        list(contract_keys)
                    )
                )
            )
            contract_schemas = {
                (contract_id, revision): input_schema
                for contract_id, revision, input_schema in contract_result.all()
            }
            available_contracts = set(contract_schemas)
            if not contract_keys.issubset(available_contracts):
                raise WorkerPlanRejected("worker_plan_contract_revision_unknown")
            for task_spec in submission.tasks:
                input_schema = contract_schemas[
                    (task_spec.contract_id, task_spec.contract_revision)
                ]
                try:
                    Draft202012Validator.check_schema(input_schema)
                    Draft202012Validator(
                        input_schema,
                        format_checker=FormatChecker(),
                    ).validate(task_spec.input)
                except (jsonschema.SchemaError, jsonschema.ValidationError) as exc:
                    raise WorkerPlanRejected("worker_task_input_schema_invalid") from exc

            active_plan_id = await session.scalar(
                select(OrchestratorPlanModel.plan_id)
                .where(
                    OrchestratorPlanModel.run_id == submission.run_id,
                    OrchestratorPlanModel.status.in_(("created", "running")),
                )
                .limit(1)
            )
            if active_plan_id is not None:
                raise WorkerPlanRejected("worker_run_has_active_plan")

            now = await self._now(session)
            plan = OrchestratorPlanModel(
                plan_id=submission.plan_id,
                run_id=submission.run_id,
                sequence=submission.sequence,
                idempotency_key=submission.idempotency_key,
                request_hash=request_hash,
                goal=submission.goal,
                status="running",
                expected_task_count=len(submission.tasks),
                terminal_task_count=0,
                root_wait_generation=run.root_wait_generation + 1,
                created_at=now,
            )
            session.add(plan)
            for task_spec in submission.tasks:
                task_id = self._id_factory()
                operation_id = self._id_factory()
                execution_id = self._id_factory()
                command_id = self._id_factory()
                session.add(
                    OrchestratorTaskModel(
                        task_id=task_id,
                        operation_id=operation_id,
                        plan_id=submission.plan_id,
                        task_ref=task_spec.task_ref,
                        contract_id=task_spec.contract_id,
                        contract_revision=task_spec.contract_revision,
                        objective=task_spec.objective,
                        input_json=task_spec.input,
                        acceptance_criteria=task_spec.acceptance_criteria,
                        status=WorkerTaskStatus.PENDING.value,
                        execution_generation=1,
                        cancellation_epoch=run.cancellation_epoch,
                        active_execution_id=execution_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                command_document = {
                    "protocol_version": "1.0",
                    "command_id": command_id,
                    "run_id": submission.run_id,
                    "plan_id": submission.plan_id,
                    "task_id": task_id,
                    "execution_id": execution_id,
                    "generation": 1,
                    "cancellation_epoch": run.cancellation_epoch,
                    "operation_id": operation_id,
                    "contract_id": task_spec.contract_id,
                    "contract_revision": task_spec.contract_revision,
                    "objective": task_spec.objective,
                    "acceptance_criteria": task_spec.acceptance_criteria,
                    "input": task_spec.input,
                    "assignment_required": True,
                }
                session.add(
                    OrchestratorExecutionModel(
                        execution_id=execution_id,
                        task_id=task_id,
                        instance_id=None,
                        generation=1,
                        status="queued",
                        cancellation_epoch=run.cancellation_epoch,
                        capacity_reserved=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                session.add(
                    OrchestratorWorkerCommandModel(
                        command_id=command_id,
                        execution_id=execution_id,
                        instance_id=None,
                        generation=1,
                        idempotency_key=command_id,
                        command_type="execute",
                        request_hash=self._hash_json(command_document),
                        command_json=command_document,
                        status="awaiting_assignment",
                        attempt=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
            run.orchestrator_plan_sequence = submission.sequence
            run.root_wait_generation = plan.root_wait_generation
            run.root_wait_interrupt_id = None
            run.root_wait_checkpoint_id = None
            run.root_wait_armed_at = None
            return WorkerPlanReceipt(
                plan_id=plan.plan_id,
                run_id=plan.run_id,
                sequence=plan.sequence,
                accepted=True,
                replayed=False,
            )

    async def _ensure_root_resume_job(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        plan_id: str,
        root_wait_generation: int,
        now: datetime,
    ) -> None:
        """按等待身份幂等创建 Root 恢复作业，兼容 checkpoint 先 arm 的竞态。"""
        await session.execute(
            pg_insert(OrchestratorRootResumeJobModel)
            .values(
                job_id=self._id_factory(),
                run_id=run_id,
                plan_id=plan_id,
                root_wait_generation=root_wait_generation,
                status="pending",
                attempt=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["run_id", "plan_id", "root_wait_generation"]
            )
        )

    async def register_worker(
        self, intent: WorkerRegistrationIntent
    ) -> WorkerRegistrationReceipt:
        """不可变保存能力契约并幂等登记 Worker 实例。

        参数：
        - `intent`（WorkerRegistrationIntent）：已认证的实例身份、运行配置和四项能力说明。

        返回：新建或幂等重放的实例与契约版本回执。

        异常：
        - WorkerIdempotencyConflict：同一主体的注册请求 ID 被用于不同内容。
        """
        contract_document = {
            "can_do": intent.can_do,
            "cannot_do": intent.cannot_do,
            "input_schema": intent.input_schema,
            "output_schema": intent.output_schema,
        }
        fingerprint_hash = self._hash_json(contract_document)
        request_hash = intent.request_hash or self._hash_json(
            {
                "registration_request_id": intent.registration_request_id,
                "service_principal": intent.service_principal,
                "instance_key": intent.instance_key,
                "base_url": intent.base_url,
                "execution_timeout_seconds": intent.execution_timeout_seconds,
                "max_concurrency": intent.max_concurrency,
                "contract_fingerprint": fingerprint_hash,
            }
        )
        async with self._session_factory() as session, session.begin():
            lock_keys = sorted(
                {
                    "worker-registration-request:"
                    f"{intent.service_principal}:{intent.registration_request_id}",
                    f"worker-instance-key:{intent.instance_key}",
                    f"worker-instance:{intent.service_principal}:{intent.instance_key}",
                    f"worker-contract:{fingerprint_hash}",
                }
            )
            for lock_key in lock_keys:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": lock_key},
                )
            instance_owner = await session.scalar(
                select(WorkerInstanceModel.service_principal)
                .where(WorkerInstanceModel.instance_key == intent.instance_key)
                .limit(1)
            )
            if instance_owner is not None and instance_owner != intent.service_principal:
                raise WorkerInstancePrincipalMismatch(
                    "worker_instance_key_owned_by_another_principal"
                )
            prior = await session.scalar(
                select(WorkerRegistrationReceiptModel).where(
                    WorkerRegistrationReceiptModel.service_principal
                    == intent.service_principal,
                    WorkerRegistrationReceiptModel.registration_request_id
                    == intent.registration_request_id,
                )
            )
            if prior is not None:
                if prior.request_hash != request_hash:
                    raise WorkerIdempotencyConflict(
                        "worker_registration_idempotency_conflict"
                    )
                response = prior.response_json
                return WorkerRegistrationReceipt(
                    instance_id=str(response["worker_instance_id"]),
                    instance_key=str(response["worker_instance_key"]),
                    contract_id=str(response["contract_id"]),
                    contract_revision=int(response["contract_revision"]),
                    registered_at=datetime.fromisoformat(response["registered_at"]),
                    replayed=True,
                )

            now = await self._now(session)
            instance = await session.scalar(
                select(WorkerInstanceModel)
                .where(
                    WorkerInstanceModel.service_principal == intent.service_principal,
                    WorkerInstanceModel.instance_key == intent.instance_key,
                )
                .with_for_update()
            )
            if instance is not None:
                contract_lock_key = f"worker-contract-id:{instance.contract_id}"
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": contract_lock_key},
                )
                latest = await session.scalar(
                    select(WorkerContractModel)
                    .where(WorkerContractModel.contract_id == instance.contract_id)
                    .order_by(WorkerContractModel.revision.desc())
                    .limit(1)
                )
                current = await session.get(
                    WorkerContractModel,
                    (instance.contract_id, instance.contract_revision),
                )
                if latest is None or current is None:
                    raise RuntimeError("worker_instance_contract_history_missing")
                if current.fingerprint_hash == fingerprint_hash:
                    contract = current
                else:
                    contract = WorkerContractModel(
                        contract_id=instance.contract_id,
                        revision=latest.revision + 1,
                        can_do_json=list(intent.can_do),
                        cannot_do_json=list(intent.cannot_do),
                        input_schema_json=intent.input_schema,
                        output_schema_json=intent.output_schema,
                        fingerprint_hash=fingerprint_hash,
                        created_at=now,
                    )
                    session.add(contract)
            else:
                contract = await session.scalar(
                    select(WorkerContractModel)
                    .where(WorkerContractModel.fingerprint_hash == fingerprint_hash)
                    .order_by(
                        WorkerContractModel.created_at.desc(),
                        WorkerContractModel.revision.desc(),
                    )
                    .limit(1)
                )
                if contract is None:
                    contract = WorkerContractModel(
                        contract_id=self._id_factory(),
                        revision=1,
                        can_do_json=list(intent.can_do),
                        cannot_do_json=list(intent.cannot_do),
                        input_schema_json=intent.input_schema,
                        output_schema_json=intent.output_schema,
                        fingerprint_hash=fingerprint_hash,
                        created_at=now,
                    )
                    session.add(contract)

            if instance is None:
                instance = WorkerInstanceModel(
                    instance_id=self._id_factory(),
                    service_principal=intent.service_principal,
                    instance_key=intent.instance_key,
                    contract_id=contract.contract_id,
                    contract_revision=contract.revision,
                    base_url=intent.base_url,
                    execution_timeout_seconds=intent.execution_timeout_seconds,
                    max_concurrency=intent.max_concurrency,
                    status="not_ready",
                    accepting_tasks=False,
                    consecutive_health_failures=0,
                    consecutive_health_successes=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(instance)
            else:
                instance.contract_id = contract.contract_id
                instance.contract_revision = contract.revision
                instance.base_url = intent.base_url
                instance.execution_timeout_seconds = intent.execution_timeout_seconds
                instance.max_concurrency = intent.max_concurrency
                instance.status = "not_ready"
                instance.accepting_tasks = False
                instance.consecutive_health_failures = 0
                instance.consecutive_health_successes = 0
                instance.health_identity_valid = True
                instance.updated_at = now

            response_json = WorkerRegistrationResponseModel.model_validate(
                {
                    "worker_instance_id": instance.instance_id,
                    "worker_instance_key": instance.instance_key,
                    "contract_id": contract.contract_id,
                    "contract_revision": contract.revision,
                    "status": "inactive",
                    "registered_at": now,
                }
            ).model_dump(mode="json", exclude_none=True)
            session.add(
                WorkerRegistrationReceiptModel(
                    receipt_id=self._id_factory(),
                    service_principal=intent.service_principal,
                    registration_request_id=intent.registration_request_id,
                    request_hash=request_hash,
                    instance_id=instance.instance_id,
                    response_json=response_json,
                    created_at=now,
                )
            )
            return WorkerRegistrationReceipt(
                instance_id=instance.instance_id,
                instance_key=instance.instance_key,
                contract_id=contract.contract_id,
                contract_revision=contract.revision,
                registered_at=now,
            )

    async def find_timeout_candidates(
        self, *, batch_size: int, queue_max_wait_seconds: float = 300.0
    ) -> list[str]:
        """按数据库时钟读取当前到期且仍有效的执行候选。

        参数：
        - `batch_size`（int）：本轮最多返回的 Execution 数量，必须为正数。

        返回：可能需要内部超时裁决的 Execution ID；候选仍须由结果用例加锁复验。

        异常：ValueError 表示批量大小无效；数据库错误原样传播。
        """
        if batch_size < 1 or queue_max_wait_seconds <= 0:
            raise ValueError("worker_timeout_batch_size_invalid")
        async with self._session_factory() as session, session.begin():
            now = await self._now(session)
            result = await session.scalars(
                select(OrchestratorExecutionModel.execution_id)
                .join(
                    OrchestratorTaskModel,
                    OrchestratorTaskModel.task_id == OrchestratorExecutionModel.task_id,
                )
                .where(
                    (
                        (
                            (OrchestratorExecutionModel.status == "running")
                            & OrchestratorExecutionModel.deadline_at.is_not(None)
                            & (OrchestratorExecutionModel.deadline_at <= now)
                            & (OrchestratorTaskModel.status == "running")
                            & (
                                OrchestratorTaskModel.active_execution_id
                                == OrchestratorExecutionModel.execution_id
                            )
                            & (
                                OrchestratorTaskModel.execution_generation
                                == OrchestratorExecutionModel.generation
                            )
                        )
                        | (
                            (OrchestratorExecutionModel.status == "queued")
                            & (
                                OrchestratorExecutionModel.created_at
                                <= now - timedelta(seconds=queue_max_wait_seconds)
                            )
                            & OrchestratorTaskModel.status.in_(("pending", "retry_pending"))
                        )
                        | (
                            (OrchestratorExecutionModel.status == "waiting_approval")
                            & (OrchestratorTaskModel.status == "waiting_approval")
                            & (
                                OrchestratorTaskModel.active_execution_id
                                == OrchestratorExecutionModel.execution_id
                            )
                            & (
                                OrchestratorExecutionModel.generation
                                == OrchestratorTaskModel.execution_generation
                            )
                            & select(WorkerApprovalRequestModel.execution_id)
                            .where(
                                WorkerApprovalRequestModel.execution_id
                                == OrchestratorExecutionModel.execution_id,
                                WorkerApprovalRequestModel.approval_request_id
                                == OrchestratorExecutionModel.current_approval_request_id,
                                WorkerApprovalRequestModel.pause_id
                                == OrchestratorExecutionModel.current_pause_id,
                                WorkerApprovalRequestModel.resume_command_id.is_not(None),
                                WorkerApprovalRequestModel.resume_wait_deadline_at.is_not(None),
                                WorkerApprovalRequestModel.resume_wait_deadline_at <= now,
                            )
                            .exists()
                        )
                    )
                )
                .order_by(
                    OrchestratorExecutionModel.deadline_at,
                    OrchestratorExecutionModel.execution_id,
                )
                .limit(batch_size)
            )
            return list(result.all())

    async def expire_queued_execution(
        self, *, execution_id: str, queue_max_wait_seconds: float
    ) -> bool:
        """收束排队或恢复等待超过上限的执行，并保留必要清理命令。

        参数：
        - `execution_id`（str）：扫描候选执行标识。
        - `queue_max_wait_seconds`（float）：排队允许的最长等待时间，必须大于零。

        返回：本事务首次将任务收束时返回 `True`；候选已变化时返回 `False`。

        异常：ValueError 表示等待上限无效；数据库错误原样传播。
        """
        if queue_max_wait_seconds <= 0:
            raise ValueError("worker_queue_max_wait_invalid")
        async with self._session_factory() as session, session.begin():
            execution_hint = await session.get(OrchestratorExecutionModel, execution_id)
            if execution_hint is None:
                return False
            task_hint = await session.get(OrchestratorTaskModel, execution_hint.task_id)
            if task_hint is None:
                return False
            plan_hint = await session.get(OrchestratorPlanModel, task_hint.plan_id)
            if plan_hint is None:
                return False
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if any(item is None for item in (run, plan, task, execution)):
                return False
            now = await self._now(session)
            if (
                execution.status == WorkerTaskStatus.WAITING_APPROVAL.value
                and task.status == WorkerTaskStatus.WAITING_APPROVAL.value
                and task.active_execution_id == execution.execution_id
                and task.execution_generation == execution.generation
                and run.status == "running"
                and plan.status == "running"
            ):
                request = await session.scalar(
                    select(WorkerApprovalRequestModel)
                    .where(
                        WorkerApprovalRequestModel.execution_id == execution.execution_id,
                        WorkerApprovalRequestModel.approval_request_id
                        == execution.current_approval_request_id,
                        WorkerApprovalRequestModel.pause_id == execution.current_pause_id,
                        WorkerApprovalRequestModel.resume_command_id.is_not(None),
                        WorkerApprovalRequestModel.resume_wait_deadline_at.is_not(None),
                        WorkerApprovalRequestModel.resume_wait_deadline_at <= now,
                    )
                    .with_for_update()
                )
                if request is None:
                    return False
                execution.status = WorkerTaskStatus.FAILED.value
                execution.completed_at = execution.completed_at or now
                execution.error_json = {
                    "code": "TEMPORARY_UNAVAILABLE",
                    "message": "Worker did not resume before the configured wait deadline",
                    "retryable": False,
                }
                execution.deadline_at = None
                execution.remaining_seconds = None
                execution.capacity_reserved = False
                execution.capacity_released_at = now
                execution.updated_at = now
                task.status = WorkerTaskStatus.FAILED.value
                task.error_json = execution.error_json
                task.updated_at = now
                resume_commands = (
                    await session.scalars(
                        select(OrchestratorWorkerCommandModel)
                        .where(
                            OrchestratorWorkerCommandModel.execution_id == execution.execution_id,
                            OrchestratorWorkerCommandModel.command_type == "resume",
                            OrchestratorWorkerCommandModel.status.in_(
                                ("pending", "retry", "dispatching", "awaiting_assignment")
                            ),
                        )
                        .with_for_update()
                    )
                ).all()
                for command in resume_commands:
                    command.status = "superseded"
                    command.updated_at = now
                    self._clear_command_lease(command, now)
                cancel_id = self._id_factory()
                cancel_body = {
                    "protocol_version": "1.0",
                    "command_id": cancel_id,
                    "execution_id": execution.execution_id,
                    "generation": execution.generation,
                    "cancellation_epoch": execution.cancellation_epoch,
                    "reason": "execution_superseded",
                }
                session.add(
                    OrchestratorWorkerCommandModel(
                        command_id=cancel_id,
                        execution_id=execution.execution_id,
                        instance_id=execution.instance_id,
                        generation=execution.generation,
                        idempotency_key=f"resume-timeout-cancel:{request.approval_request_id}",
                        command_type="cancel",
                        request_hash=self._hash_json(cancel_body),
                        command_json=cancel_body,
                        status="pending",
                        attempt=0,
                        send_attempt_count=0,
                        delivery_uncertain_before_send=False,
                        lease_owner=None,
                        lease_token=None,
                        lease_until=None,
                        available_at=now,
                        receipt_json=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                plan.terminal_task_count += 1
                if plan.terminal_task_count >= plan.expected_task_count:
                    plan.status = "completed"
                    plan.completed_at = now
                    await self._ensure_root_resume_job(
                        session,
                        run_id=run.run_id,
                        plan_id=plan.plan_id,
                        root_wait_generation=plan.root_wait_generation,
                        now=now,
                    )
                request.status = "resume_timed_out"
                return True
            if (
                execution.status != "queued"
                or task.status not in {WorkerTaskStatus.PENDING.value, WorkerTaskStatus.RETRY_PENDING.value}
                or task.active_execution_id != execution.execution_id
                or task.execution_generation != execution.generation
                or run.status != "running"
                or plan.status != "running"
            ):
                return False
            if execution.created_at > now - timedelta(seconds=queue_max_wait_seconds):
                return False
            execution.status = "failed"
            execution.completed_at = execution.completed_at or now
            execution.error_json = {
                "code": "CAPABILITY_UNAVAILABLE",
                "message": "No healthy Worker capacity became available before queue timeout",
                "retryable": False,
            }
            execution.updated_at = now
            task.status = WorkerTaskStatus.FAILED.value
            task.error_json = execution.error_json
            task.updated_at = now
            pending_commands = (
                await session.scalars(
                    select(OrchestratorWorkerCommandModel)
                    .where(
                        OrchestratorWorkerCommandModel.execution_id == execution.execution_id,
                        OrchestratorWorkerCommandModel.command_type == "execute",
                        OrchestratorWorkerCommandModel.status.in_(("awaiting_assignment", "pending", "retry")),
                    )
                    .with_for_update()
                )
            ).all()
            for command in pending_commands:
                command.status = "superseded"
                command.updated_at = now
            plan.terminal_task_count += 1
            if plan.terminal_task_count >= plan.expected_task_count:
                plan.status = "completed"
                plan.completed_at = now
                if run.status == "running":
                    await self._ensure_root_resume_job(
                        session,
                        run_id=run.run_id,
                        plan_id=plan.plan_id,
                        root_wait_generation=plan.root_wait_generation,
                        now=now,
                    )
            return True

    async def timeout_execution(self, *, execution_id: str) -> WorkerResultReceipt | None:
        """将一个执行候选转交稳定的内部超时结果裁决。

        参数：
        - `execution_id`（str）：扫描器读出的候选执行 ID。

        返回：候选当前仍有效并已裁决时返回结果回执；状态已变化时返回 `None`。

        异常：ValueError 表示执行身份不存在；数据库错误原样传播。
        """
        async with self._session_factory() as session:
            execution = await session.get(OrchestratorExecutionModel, execution_id)
            if execution is None:
                return None
            instance_id = execution.instance_id
            if instance_id is None:
                return None
            submission = WorkerResultSubmission(
                execution_id=execution.execution_id,
                instance_id=instance_id,
                generation=execution.generation,
                result_id=str(uuid5(NAMESPACE_URL, f"athena:execution-timeout:{execution_id}")),
                status="failed",
                error=WorkerExecutionError(
                    code="EXECUTION_DEADLINE_EXCEEDED",
                    message="Worker execution exceeded its persisted deadline",
                    retryable=False,
                ),
                internal_timeout=True,
            )
        return await self._record_result(
            submission=submission, result_source="internal_timeout"
        )

    async def record_result(
        self,
        *,
        submission: WorkerResultSubmission,
    ) -> WorkerResultReceipt:
        """记录远程 Worker 的结果，并保证调用方始终收到稳定回执。

        参数：
        - `submission`（WorkerResultSubmission）：Worker 提交的执行结果。

        返回：首次裁决或幂等重放的非空结果回执。

        异常：身份无效、幂等冲突或数据库错误时向调用方抛出。
        """
        if submission.internal_timeout:
            raise ValueError("worker_result_internal_timeout_not_public")
        receipt = await self._record_result(submission=submission, result_source="worker")
        if receipt is None:
            raise RuntimeError("remote_worker_result_cannot_be_internal_timeout")
        return receipt

    async def _record_result(
        self,
        *,
        submission: WorkerResultSubmission,
        result_source: Literal["worker", "internal_timeout"],
    ) -> WorkerResultReceipt | None:
        """原子记录 Worker 结果、Task 终态计数和唯一 Root 恢复作业。

        参数：
        - `submission`（WorkerResultSubmission）：结果身份、代次、状态和完整业务内容。

        返回：首次裁定或幂等重放的 `WorkerResultReceipt`。

        异常：
        - WorkerIdempotencyConflict：相同 Execution/result ID 的内容不同。
        - ValueError：身份不存在或结果状态不合法。
        """
        if (result_source == "internal_timeout") != submission.internal_timeout:
            raise ValueError("worker_result_source_mismatch")
        request_hash = submission.request_hash or self._hash_json(asdict(submission))
        async with self._session_factory() as session, session.begin():
            # 先无锁读取父级身份，再统一按 Run→Plan→Task→Execution 顺序加锁。
            execution_hint = await session.get(
                OrchestratorExecutionModel, submission.execution_id
            )
            if execution_hint is None:
                raise ValueError("worker_result_execution_identity_unknown")
            resolved_task_id = execution_hint.task_id
            task_hint = await session.get(OrchestratorTaskModel, resolved_task_id)
            if task_hint is None:
                raise ValueError("worker_result_task_identity_unknown")
            plan_hint = await session.get(OrchestratorPlanModel, task_hint.plan_id)
            if plan_hint is None:
                raise ValueError("worker_result_plan_identity_unknown")
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == task_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == resolved_task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == submission.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if run is None or plan is None or task is None or execution is None:
                raise ValueError("worker_result_identity_disappeared")
            if execution.instance_id != submission.instance_id:
                raise ValueError("worker_result_instance_mismatch")

            # Run 锁使同一逻辑任务上的并发结果串行；锁后重查幂等回执。
            prior = await session.scalar(
                select(WorkerResultReceiptModel).where(
                    WorkerResultReceiptModel.execution_id == submission.execution_id,
                    WorkerResultReceiptModel.result_source == result_source,
                    WorkerResultReceiptModel.result_id == submission.result_id,
                )
            )
            if prior is not None:
                if prior.request_hash != request_hash:
                    raise WorkerIdempotencyConflict("worker_result_idempotency_conflict")
                return WorkerResultReceipt(
                    receipt_id=prior.receipt_id,
                    execution_id=prior.execution_id,
                    result_id=prior.result_id,
                    disposition=prior.disposition,
                    reason=prior.reason,
                    generation=int(prior.receipt_json["generation"]),
                    received_at=prior.created_at,
                    next_execution_id=prior.next_execution_id,
                    replayed=True,
                )

            now = await self._now(session)
            if submission.internal_timeout and (
                execution.status != "running"
                or task.status != "running"
                or task.active_execution_id != execution.execution_id
                or task.execution_generation != execution.generation
                or execution.deadline_at is None
                or now < execution.deadline_at
            ):
                return None
            stale_reason = None
            disposition = "recorded"
            reason = "success_recorded"
            next_execution_id = None
            queue_capacity_exhausted = False
            if task.status in {status.value for status in TASK_TERMINAL_STATUSES}:
                stale_reason = "task_cancelled" if task.status == "cancelled" else "execution_superseded"
            elif task.active_execution_id != submission.execution_id:
                stale_reason = "execution_superseded"
            elif execution.generation != submission.generation or task.execution_generation != submission.generation:
                stale_reason = "execution_superseded"
            elif execution.cancellation_epoch != task.cancellation_epoch:
                stale_reason = "task_cancelled"
            elif run.status != "running" or execution.status == "cancelled":
                stale_reason = "task_cancelled"
            elif execution.status != "running" or task.status != "running":
                stale_reason = "execution_superseded"
            elif (
                not submission.internal_timeout
                and execution.deadline_at is not None
                and now >= execution.deadline_at
            ):
                stale_reason = "execution_deadline_exceeded"

            if stale_reason is not None:
                disposition = "ignored"
                reason = stale_reason
            else:
                # 只有当前执行的有效结果才能收束命令。审批等待期的迟到同代结果
                # 会被忽略，不能提前把仍待派发的 ResumeCommand 标成已完成。
                dispatch_command = await session.scalar(
                    select(OrchestratorWorkerCommandModel)
                    .where(
                        OrchestratorWorkerCommandModel.execution_id
                        == submission.execution_id,
                        OrchestratorWorkerCommandModel.generation
                        == submission.generation,
                        OrchestratorWorkerCommandModel.command_type == "execute",
                    )
                    .with_for_update()
                )
                if dispatch_command is not None and dispatch_command.status not in {
                    "result_received",
                    "failed",
                    "superseded",
                }:
                    dispatch_command.status = "result_received"
                    self._clear_command_lease(dispatch_command, now)
                # Resume receipt and terminal result are independent HTTP requests.
                # A valid result can win the race, so converge the matching command.
                resume_command_id = await session.scalar(
                    select(WorkerApprovalRequestModel.resume_command_id)
                    .where(
                        WorkerApprovalRequestModel.execution_id
                        == submission.execution_id,
                        WorkerApprovalRequestModel.generation
                        == submission.generation,
                        WorkerApprovalRequestModel.cancellation_epoch
                        == execution.cancellation_epoch,
                        WorkerApprovalRequestModel.approval_request_id
                        == execution.current_approval_request_id,
                        WorkerApprovalRequestModel.pause_id == execution.current_pause_id,
                        WorkerApprovalRequestModel.resume_command_id.is_not(None),
                    )
                    .order_by(WorkerApprovalRequestModel.received_at.desc())
                    .limit(1)
                )
                if resume_command_id is not None:
                    resume_command = await session.scalar(
                        select(OrchestratorWorkerCommandModel)
                        .where(
                            OrchestratorWorkerCommandModel.command_id
                            == resume_command_id,
                            OrchestratorWorkerCommandModel.execution_id
                            == submission.execution_id,
                            OrchestratorWorkerCommandModel.generation
                            == submission.generation,
                            OrchestratorWorkerCommandModel.command_type == "resume",
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if resume_command is not None and resume_command.status not in {
                        "result_received",
                        "failed",
                        "superseded",
                    }:
                        resume_command.status = "result_received"
                        self._clear_command_lease(resume_command, now)
                result_status = submission.status
                output = submission.output
                error = (
                    None
                    if submission.error is None
                    else {
                        "code": submission.error.code,
                        "message": submission.error.message,
                        "retryable": submission.error.retryable,
                        "details": submission.error.details,
                    }
                )
                output_valid = True
                if result_status == "completed":
                    contract = await session.get(
                        WorkerContractModel,
                        (task.contract_id, task.contract_revision),
                    )
                    if contract is None:
                        raise ValueError("worker_result_contract_revision_unknown")
                    try:
                        Draft202012Validator(
                            contract.output_schema_json,
                            format_checker=FormatChecker(),
                        ).validate(output)
                    except jsonschema.ValidationError:
                        output_valid = False
                if not output_valid:
                    result_status = "failed"
                    error = {
                        "code": "WORKER_OUTPUT_INVALID",
                        "message": "Worker output does not satisfy output_schema",
                        "retryable": False,
                    }
                    reason = "output_schema_invalid"
                elif result_status == "completed":
                    reason = "success_recorded"
                else:
                    error_code = submission.error.code if submission.error else ""
                    retryable_codes = {
                        "WORKER_UPSTREAM_TIMEOUT",
                        "WORKER_RESOURCE_UNAVAILABLE",
                        "WORKER_CAPACITY_LOST",
                    }
                    if (
                        (submission.internal_timeout or error_code in retryable_codes)
                        and submission.generation < self._max_execution_attempts
                    ):
                        if await self._has_queued_task_capacity(
                            session, additional_tasks=1
                        ):
                            disposition = "retry_scheduled"
                            reason = "worker_error_retryable"
                        else:
                            # 队列容量不足时保留原错误内容，但把当前逻辑 Task
                            # 收束为最终失败，避免重试路径绕过全局队列上限。
                            queue_capacity_exhausted = True
                            reason = "worker_error_final"
                    elif error_code in retryable_codes:
                        reason = "execution_attempts_exhausted"
                    else:
                        reason = "worker_error_final"

                if queue_capacity_exhausted:
                    original_details = (
                        dict(error.get("details") or {})
                        if isinstance(error, dict)
                        and isinstance(error.get("details"), dict)
                        else {}
                    )
                    original_details["queue_capacity"] = {
                        "code": "CAPABILITY_UNAVAILABLE",
                        "message": "Worker retry queue capacity is exhausted",
                    }
                    error = {
                        **(error or {}),
                        "details": original_details,
                    }

                execution.status = result_status
                execution.completed_at = execution.completed_at or now
                execution.worker_result_id = submission.result_id
                execution.result_json = output if output_valid else None
                execution.error_json = error
                execution.updated_at = now
                # 有效终态回执证明该 Execution 已停止；取消后的迟到结果走 ignored，不能释放旧槽位。
                if execution.capacity_reserved and not submission.internal_timeout:
                    execution.capacity_reserved = False
                    execution.capacity_released_at = now
                if disposition == "retry_scheduled":
                    next_execution_id = self._id_factory()
                    next_generation = submission.generation + 1
                    next_command_id = self._id_factory()
                    retry_delay = self._execution_retry_delay(submission.generation - 1)
                    retry_available_at = now + timedelta(seconds=retry_delay)
                    session.add(
                        OrchestratorExecutionModel(
                            execution_id=next_execution_id,
                            task_id=task.task_id,
                            instance_id=None,
                            generation=next_generation,
                            status="queued",
                            cancellation_epoch=task.cancellation_epoch,
                            capacity_reserved=False,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                    retry_command = {
                        "protocol_version": "1.0",
                        "command_id": next_command_id,
                        "run_id": run.run_id,
                        "plan_id": plan.plan_id,
                        "task_id": task.task_id,
                        "execution_id": next_execution_id,
                        "generation": next_generation,
                        "cancellation_epoch": task.cancellation_epoch,
                        "operation_id": task.operation_id,
                        "contract_id": task.contract_id,
                        "contract_revision": task.contract_revision,
                        "objective": task.objective,
                        "acceptance_criteria": task.acceptance_criteria,
                        "input": task.input_json,
                        "assignment_required": True,
                    }
                    session.add(
                        OrchestratorWorkerCommandModel(
                            command_id=next_command_id,
                            execution_id=next_execution_id,
                            instance_id=None,
                            generation=next_generation,
                            idempotency_key=next_command_id,
                            command_type="execute",
                            request_hash=self._hash_json(retry_command),
                            command_json=retry_command,
                            status="awaiting_assignment",
                            attempt=0,
                            available_at=retry_available_at,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    task.execution_generation = next_generation
                    task.active_execution_id = next_execution_id
                    task.status = WorkerTaskStatus.RETRY_PENDING.value
                    task.error_json = error
                elif result_status == "completed":
                    task.status = WorkerTaskStatus.COMPLETED.value
                    task.output_json = output or {}
                    task.error_json = None
                else:
                    task.status = WorkerTaskStatus.FAILED.value
                    task.error_json = error
                if submission.internal_timeout and execution.capacity_reserved:
                    # 最后一次超时也必须发送物理停止命令，否则容量会永久占用。
                    cancel_command_id = self._id_factory()
                    cancel_body = {
                        "protocol_version": "1.0",
                        "command_id": cancel_command_id,
                        "execution_id": execution.execution_id,
                        "generation": execution.generation,
                        "cancellation_epoch": execution.cancellation_epoch,
                        "reason": "execution_timed_out",
                    }
                    session.add(
                        OrchestratorWorkerCommandModel(
                            command_id=cancel_command_id,
                            execution_id=execution.execution_id,
                            instance_id=execution.instance_id,
                            generation=execution.generation,
                            idempotency_key=cancel_command_id,
                            command_type="cancel",
                            request_hash=self._hash_json(cancel_body),
                            command_json=cancel_body,
                            status="pending",
                            attempt=0,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                task.updated_at = now
                if task.status in {status.value for status in TASK_TERMINAL_STATUSES}:
                    plan.terminal_task_count += 1
                    if plan.terminal_task_count == plan.expected_task_count:
                        plan.status = "completed"
                        plan.completed_at = now
                        if run.status == "running":
                            await self._ensure_root_resume_job(
                                session,
                                run_id=run.run_id,
                                plan_id=plan.plan_id,
                                root_wait_generation=plan.root_wait_generation,
                                now=now,
                            )
            receipt_id = self._id_factory()
            receipt_data = WorkerResultReceiptProtocolModel.model_validate(
                {
                    "execution_id": submission.execution_id,
                    "result_id": submission.result_id,
                    "generation": submission.generation,
                    "disposition": disposition,
                    "reason": reason,
                    "next_execution_id": next_execution_id,
                    "received_at": now,
                }
            ).model_dump(mode="json", exclude_none=True)
            session.add(
                WorkerResultReceiptModel(
                    receipt_id=receipt_id,
                    execution_id=submission.execution_id,
                    task_id=resolved_task_id,
                    result_source=result_source,
                    result_id=submission.result_id,
                    request_hash=request_hash,
                    disposition=disposition,
                    reason=reason,
                    next_execution_id=next_execution_id,
                    receipt_json=receipt_data,
                    created_at=now,
                )
            )
            return WorkerResultReceipt(
                receipt_id=receipt_id,
                execution_id=submission.execution_id,
                result_id=submission.result_id,
                disposition=disposition,
                reason=reason,
                generation=submission.generation,
                received_at=now,
                next_execution_id=next_execution_id,
            )

    def _execution_retry_delay(self, retry_index: int) -> float:
        """计算执行级重试的指数退避间隔。

        参数：
        - `retry_index`（int）：即将创建的重试序号，从零开始；负数会被拒绝。

        返回：应用配置上限和抖动后的秒数。

        异常：ValueError 表示序号为负数。
        """
        if retry_index < 0:
            raise ValueError("worker_execution_retry_index_invalid")
        base = min(
            self._execution_retry_max,
            self._execution_retry_initial * (2**retry_index),
        )
        spread = base * self._execution_retry_jitter_ratio
        if spread == 0:
            return base
        return min(
            self._execution_retry_max,
            max(0.0, random.uniform(base - spread, base + spread)),
        )

    async def cancel_task(self, *, run_id: str, task_id: str) -> str | None:
        """逻辑取消一个 Task 并持久化对应远端取消指令。

        参数：
        - `run_id`（str）：Task 所属的 Run。
        - `task_id`（str）：要取消的逻辑 Task。

        返回：当前 Execution 需要远程停止时返回其 CancelCommand ID；未派发时返回 None。

        异常：
        - ValueError：Run 或 Task 不存在，或 Task 与 Run 不匹配。
        """
        async with self._session_factory() as session, session.begin():
            task_hint = await session.get(OrchestratorTaskModel, task_id)
            if task_hint is None:
                raise ValueError("worker_task_unknown")
            plan_hint = await session.get(OrchestratorPlanModel, task_hint.plan_id)
            if plan_hint is None or plan_hint.run_id != run_id:
                raise ValueError("worker_task_run_identity_mismatch")
            run = await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            plan = await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            task = await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if run is None or plan is None or task is None:
                raise ValueError("worker_task_identity_disappeared")
            now = await self._now(session)
            task_was_terminal = task.status in {
                status.value for status in TASK_TERMINAL_STATUSES
            }
            if not task_was_terminal:
                task.cancellation_epoch += 1
                task.status = WorkerTaskStatus.CANCELLED.value
                task.updated_at = now
            cancellation_epoch = task.cancellation_epoch
            command_id: str | None = None
            executions = (
                await session.scalars(
                    select(OrchestratorExecutionModel)
                    .where(OrchestratorExecutionModel.task_id == task.task_id)
                    .order_by(OrchestratorExecutionModel.generation)
                    .with_for_update()
                )
            ).all()
            for execution in executions:
                commands = (
                    await session.scalars(
                        select(OrchestratorWorkerCommandModel)
                        .where(
                            OrchestratorWorkerCommandModel.execution_id
                            == execution.execution_id,
                            OrchestratorWorkerCommandModel.status.in_(
                                ("pending", "retry", "awaiting_assignment")
                            ),
                        )
                        .order_by(OrchestratorWorkerCommandModel.command_id)
                        .with_for_update()
                    )
                ).all()
                reason = (
                    "task_cancelled"
                    if task.status == WorkerTaskStatus.CANCELLED.value
                    else "execution_superseded"
                )
                matching_cancel = next(
                    (
                        command
                        for command in commands
                        if command.command_type == "cancel"
                        and command.command_json.get("reason") == reason
                        and command.command_json.get("cancellation_epoch")
                        == cancellation_epoch
                    ),
                    None,
                )
                pending_physical_stop = next(
                    (
                        command
                        for command in commands
                        if command.command_type == "cancel"
                        and command.command_json.get("reason")
                        in {"execution_timed_out", "execution_superseded"}
                    ),
                    None,
                )
                if (
                    task_was_terminal
                    and execution.capacity_reserved
                    and pending_physical_stop is not None
                ):
                    # 已有的超时/替代停止意图绑定旧 epoch，逻辑取消不能改写其确认身份。
                    command_id = pending_physical_stop.command_id
                    continue
                for command in commands:
                    if (
                        command.command_type == "cancel"
                        and execution.capacity_reserved
                        and execution.instance_id is not None
                        and command is matching_cancel
                    ):
                        continue
                    command.status = "superseded"
                    command.updated_at = now

                if execution.capacity_reserved and execution.instance_id is not None:
                    execution.status = "cancelling"
                    execution.cancellation_epoch = cancellation_epoch
                    execution.updated_at = now
                    if matching_cancel is not None:
                        command_id = matching_cancel.command_id
                        continue
                    command_id = self._id_factory()
                    command_body = {
                        "protocol_version": "1.0",
                        "command_id": command_id,
                        "execution_id": execution.execution_id,
                        "generation": execution.generation,
                        "cancellation_epoch": cancellation_epoch,
                        "reason": reason,
                    }
                    session.add(
                        OrchestratorWorkerCommandModel(
                            command_id=command_id,
                            execution_id=execution.execution_id,
                            instance_id=execution.instance_id,
                            generation=execution.generation,
                            idempotency_key=command_id,
                            command_type="cancel",
                            request_hash=self._hash_json(command_body),
                            command_json=command_body,
                            status="pending",
                            attempt=0,
                            available_at=now,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                elif execution.status not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "superseded",
                    "timed_out",
                }:
                    execution.status = "cancelled"
                    execution.completed_at = execution.completed_at or now
                    execution.cancellation_epoch = cancellation_epoch
                    execution.capacity_reserved = False
                    execution.updated_at = now

            if not task_was_terminal:
                plan.terminal_task_count += 1
            if (
                plan.terminal_task_count >= plan.expected_task_count
                and plan.status in {"created", "running"}
            ):
                plan.status = "completed"
                plan.completed_at = now
                if run.status == "running":
                    await self._ensure_root_resume_job(
                        session,
                        run_id=run_id,
                        plan_id=plan.plan_id,
                        root_wait_generation=plan.root_wait_generation,
                        now=now,
                    )
                else:
                    plan.status = "cancelled"
            return command_id

    async def confirm_stopped(
        self, confirmation: WorkerStopConfirmation
    ) -> WorkerStopReceipt:
        """验证 Worker 停止回执并恰好释放对应 Execution 的容量槽。

        参数：
        - `confirmation`（WorkerStopConfirmation）：绑定 Worker 实例、取消命令和代次的停止确认。

        返回：标记 `capacity_released` 或 `already_released` 的稳定回执。

        异常：
        - WorkerIdempotencyConflict：相同确认 ID 被用于不同请求内容。
        - ValueError：Worker 实例或停止确认与持久 CancelCommand 不匹配。
        """
        body = WorkerStopConfirmationProtocolModel.model_validate(
            {
                "stop_confirmation_id": confirmation.stop_confirmation_id,
                "cancel_command_id": confirmation.cancel_command_id,
                "execution_id": confirmation.execution_id,
                "generation": confirmation.generation,
                "cancellation_epoch": confirmation.cancellation_epoch,
                "reason": confirmation.reason,
                "stopped": True,
                "stopped_at": confirmation.stopped_at,
            }
        ).model_dump(mode="json", exclude_none=True)
        request_hash = confirmation.request_hash or self._hash_json(body)
        async with self._session_factory() as session, session.begin():
            stop_lock_key = (
                "worker-stop-confirmation:"
                f"{confirmation.instance_id}:{confirmation.stop_confirmation_id}"
            )
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": stop_lock_key},
            )
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(
                    OrchestratorWorkerCommandModel.command_id
                    == confirmation.cancel_command_id
                )
                .execution_options(populate_existing=True)
            )
            if command is None or command.command_type != "cancel":
                raise ValueError("worker_stop_cancel_command_unknown")
            if command.instance_id != confirmation.instance_id:
                raise ValueError("worker_stop_confirmation_command_mismatch")
            execution_hint = await session.get(
                OrchestratorExecutionModel, confirmation.execution_id
            )
            task_hint = (
                None
                if execution_hint is None
                else await session.get(OrchestratorTaskModel, execution_hint.task_id)
            )
            plan_hint = (
                None
                if task_hint is None
                else await session.get(OrchestratorPlanModel, task_hint.plan_id)
            )
            if plan_hint is None:
                raise ValueError("worker_stop_execution_identity_unknown")
            await session.scalar(
                select(AgentRunModel)
                .where(AgentRunModel.run_id == plan_hint.run_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            await session.scalar(
                select(OrchestratorPlanModel)
                .where(OrchestratorPlanModel.plan_id == plan_hint.plan_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            await session.scalar(
                select(OrchestratorTaskModel)
                .where(OrchestratorTaskModel.task_id == task_hint.task_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            execution = await session.scalar(
                select(OrchestratorExecutionModel)
                .where(OrchestratorExecutionModel.execution_id == confirmation.execution_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            # 按 Run→Plan→Task→Execution→Command 顺序锁定命令，令 StopConfirmation
            # 与正在返回的 CancelReceipt 在同一条命令状态线上原子收敛。
            command = await session.scalar(
                select(OrchestratorWorkerCommandModel)
                .where(
                    OrchestratorWorkerCommandModel.command_id
                    == confirmation.cancel_command_id
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if command is None or command.command_type != "cancel":
                raise ValueError("worker_stop_cancel_command_unknown")
            prior = await session.scalar(
                select(WorkerStopConfirmationModel).where(
                    WorkerStopConfirmationModel.instance_id == confirmation.instance_id,
                    WorkerStopConfirmationModel.confirmation_id
                    == confirmation.stop_confirmation_id,
                )
            )
            if prior is not None:
                if prior.request_hash != request_hash:
                    raise WorkerIdempotencyConflict(
                        "worker_stop_confirmation_idempotency_conflict"
                    )
                return WorkerStopReceipt(
                    stop_confirmation_id=confirmation.stop_confirmation_id,
                    cancel_command_id=confirmation.cancel_command_id,
                    execution_id=confirmation.execution_id,
                    generation=confirmation.generation,
                    cancellation_epoch=confirmation.cancellation_epoch,
                    reason=confirmation.reason,
                    disposition=prior.disposition,
                    received_at=datetime.fromisoformat(prior.receipt_json["received_at"]),
                    replayed=True,
                )
            command_body = command.command_json
            if (
                command.execution_id != confirmation.execution_id
                or command.instance_id != confirmation.instance_id
                or command.generation != confirmation.generation
                or command_body.get("cancellation_epoch")
                != confirmation.cancellation_epoch
                or command_body.get("reason") != confirmation.reason
                or execution is None
            ):
                raise ValueError("worker_stop_confirmation_command_mismatch")
            disposition = (
                "already_released"
                if not execution.capacity_reserved
                else "capacity_released"
            )
            now = await self._now(session)
            if execution.capacity_reserved:
                execution.capacity_reserved = False
                execution.capacity_released_at = now
                execution.updated_at = now
            if execution.status == "cancelling":
                execution.status = "cancelled"
                execution.completed_at = execution.completed_at or now
                execution.updated_at = now
            receipt_data = WorkerStopConfirmationReceiptModel.model_validate(
                {
                    "stop_confirmation_id": confirmation.stop_confirmation_id,
                    "cancel_command_id": confirmation.cancel_command_id,
                    "execution_id": confirmation.execution_id,
                    "generation": confirmation.generation,
                    "cancellation_epoch": confirmation.cancellation_epoch,
                    "reason": confirmation.reason,
                    "disposition": disposition,
                    "task_status_unchanged": True,
                    "received_at": now,
                }
            ).model_dump(mode="json", exclude_none=True)
            session.add(
                WorkerStopConfirmationModel(
                    confirmation_id=confirmation.stop_confirmation_id,
                    instance_id=confirmation.instance_id,
                    execution_id=confirmation.execution_id,
                    cancel_command_id=confirmation.cancel_command_id,
                    generation=confirmation.generation,
                    cancellation_epoch=confirmation.cancellation_epoch,
                    reason=confirmation.reason,
                    stopped_at=confirmation.stopped_at,
                    request_hash=request_hash,
                    disposition=disposition,
                    receipt_json=receipt_data,
                    created_at=now,
                )
            )
            command.status = "stopped"
            command.receipt_json = receipt_data
            self._clear_command_lease(command, now)
            return WorkerStopReceipt(
                stop_confirmation_id=confirmation.stop_confirmation_id,
                cancel_command_id=confirmation.cancel_command_id,
                execution_id=confirmation.execution_id,
                generation=confirmation.generation,
                cancellation_epoch=confirmation.cancellation_epoch,
                reason=confirmation.reason,
                disposition=disposition,
                received_at=now,
            )

    @staticmethod
    def _plan_hash(submission: WorkerPlanSubmission) -> str:
        """生成 Plan 请求的确定性 SHA-256 摘要。

        参数：
        - `submission`（WorkerPlanSubmission）：要摘要的完整 Plan 请求。

        返回：规范 JSON 字节的十六进制 SHA-256 字符串。

        异常：此函数不主动抛出业务异常。
        """
        body = {
            "run_id": submission.run_id,
            "plan_id": submission.plan_id,
            "sequence": submission.sequence,
            "idempotency_key": submission.idempotency_key,
            "goal": submission.goal,
            "tasks": [
                {
                    "task_ref": task.task_ref,
                    "contract_id": task.contract_id,
                    "contract_revision": task.contract_revision,
                    "objective": task.objective,
                    "input": task.input,
                    "acceptance_criteria": task.acceptance_criteria,
                }
                for task in submission.tasks
            ],
        }
        return PostgresWorkerOrchestrationRepository._hash_json(body)

    @staticmethod
    def _hash_json(value: dict[str, Any]) -> str:
        """按排序键和紧凑分隔符计算 JSON 内容摘要。

        参数：
        - `value`（dict[str, Any]）：仅含可 JSON 序列化值的对象。

        返回：UTF-8 JSON 字节的十六进制 SHA-256 摘要。

        异常：
        - TypeError：对象包含非 JSON 值时传播序列化错误。
        """
        encoded = rfc8785.dumps(value)
        return hashlib.sha256(encoded).hexdigest()

    async def _now(self, session: AsyncSession) -> datetime:
        """读取注入时钟或 PostgreSQL 当前事务时间。

        参数：
        - `session`（AsyncSession）：当前业务事务会话。

        返回：带时区的当前时间，供事务内创建和比较时间戳。

        异常：
        - RuntimeError：数据库未返回可用时间时抛出。
        """
        if self._clock is not None:
            value = self._clock()
        else:
            value = await session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("postgres_clock_unavailable")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


__all__ = [
    "PostgresWorkerOrchestrationRepository",
    "WorkerIdempotencyConflict",
    "WorkerPlanRejected",
]
