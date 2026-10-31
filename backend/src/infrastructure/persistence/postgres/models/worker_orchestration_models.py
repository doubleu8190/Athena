"""远程 Worker 注册与逐轮执行的 PostgreSQL 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

_JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class WorkerContractModel(Base):
    """保存 Worker 能力契约的不可变版本。"""

    __tablename__ = "worker_contracts"
    __table_args__ = (Index("idx_worker_contract_fingerprint", "fingerprint_hash"),)

    contract_id: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    can_do_json: Mapped[list[str]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    cannot_do_json: Mapped[list[str]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    input_schema_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    output_schema_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    fingerprint_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerInstanceModel(Base):
    """保存远程 Worker 实例地址、容量、超时和健康状态。"""

    __tablename__ = "worker_instances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["contract_id", "contract_revision"],
            ["worker_contracts.contract_id", "worker_contracts.revision"],
            name="fk_worker_instance_contract_revision",
        ),
        UniqueConstraint(
            "service_principal", "instance_key", name="uq_worker_instance_identity"
        ),
        Index("idx_worker_instances_available", "status", "accepting_tasks"),
    )

    instance_id: Mapped[str] = mapped_column(String, primary_key=True)
    service_principal: Mapped[str] = mapped_column(String, nullable=False)
    instance_key: Mapped[str] = mapped_column(String, nullable=False)
    contract_id: Mapped[str] = mapped_column(String, nullable=False)
    contract_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    execution_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="not_ready")
    accepting_tasks: Mapped[bool] = mapped_column(default=False, nullable=False)
    consecutive_health_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_health_successes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    health_identity_valid: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerRegistrationReceiptModel(Base):
    """保存注册请求幂等回执，防止重放创建新实例身份。"""

    __tablename__ = "worker_registration_receipts"
    __table_args__ = (
        UniqueConstraint(
            "service_principal",
            "registration_request_id",
            name="uq_worker_registration_idempotency",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    service_principal: Mapped[str] = mapped_column(String, nullable=False)
    registration_request_id: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    instance_id: Mapped[str] = mapped_column(ForeignKey("worker_instances.instance_id"), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrchestratorPlanModel(Base):
    """保存 Run 中一轮不可变 Plan 及其完成计数。"""

    __tablename__ = "agent_plans"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_orchestrator_plan_run_sequence"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_orchestrator_plan_idempotency"),
        Index(
            "uq_orchestrator_plan_active_run",
            "run_id",
            unique=True,
            postgresql_where=text("status IN ('created', 'running')"),
            sqlite_where=text("status IN ('created', 'running')"),
        ),
        Index("idx_orchestrator_plan_run", "run_id", "sequence"),
    )

    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="created")
    expected_task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    root_wait_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrchestratorTaskModel(Base):
    """保存 Plan 中不带依赖的逻辑任务和能力引用。"""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["contract_id", "contract_revision"],
            ["worker_contracts.contract_id", "worker_contracts.revision"],
            name="fk_orchestrator_task_contract_revision",
        ),
        UniqueConstraint("plan_id", "task_ref", name="uq_orchestrator_task_plan_ref"),
        Index("idx_orchestrator_tasks_plan_state", "plan_id", "status"),
    )

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    operation_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id", ondelete="CASCADE"), nullable=False)
    task_ref: Mapped[str] = mapped_column(String, nullable=False)
    contract_id: Mapped[str] = mapped_column(String, nullable=False)
    contract_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    acceptance_criteria: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    output_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancellation_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrchestratorExecutionModel(Base):
    """保存一次执行尝试、截止时间和中心裁定的结果。"""

    __tablename__ = "task_executions"
    __table_args__ = (
        UniqueConstraint("task_id", "generation", name="uq_orchestrator_execution_generation"),
        CheckConstraint(
            "execution_timeout_seconds IS NULL OR execution_timeout_seconds > 0",
            name="ck_orchestrator_execution_timeout_positive",
        ),
        Index("idx_orchestrator_execution_deadline", "status", "deadline_at"),
        Index(
            "idx_orchestrator_execution_reserved_instance",
            "instance_id",
            "capacity_reserved",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id", ondelete="CASCADE"), nullable=False)
    instance_id: Mapped[str | None] = mapped_column(ForeignKey("worker_instances.instance_id"), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    execution_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancellation_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    capacity_reserved: Mapped[bool] = mapped_column(default=False, nullable=False)
    capacity_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remaining_seconds: Mapped[float | None] = mapped_column(nullable=True)
    worker_result_id: Mapped[str | None] = mapped_column(String, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerCommandReceiptModel(Base):
    """保留每条 Worker 命令的稳定接收回执与幂等摘要。"""

    __tablename__ = "worker_command_receipts"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_worker_command_receipt_command"),
        Index("idx_worker_command_receipt_execution", "execution_id"),
    )

    command_id: Mapped[str] = mapped_column(String, primary_key=True)
    worker_command_id: Mapped[str] = mapped_column(
        ForeignKey("orchestrator_worker_commands.command_id"), nullable=False
    )
    instance_id: Mapped[str] = mapped_column(ForeignKey("worker_instances.instance_id"), nullable=False)
    execution_id: Mapped[str] = mapped_column(ForeignKey("task_executions.execution_id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrchestratorWorkerCommandModel(Base):
    """持久保存发送给 Worker 的执行、恢复或取消指令。"""

    __tablename__ = "orchestrator_worker_commands"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_orchestrator_worker_command_idempotency"),
        Index("idx_orchestrator_worker_commands_ready", "status", "available_at"),
    )

    command_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("task_executions.execution_id"), nullable=False)
    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("worker_instances.instance_id"), nullable=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    command_type: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    command_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    send_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_uncertain_before_send: Mapped[bool] = mapped_column(
        nullable=False, default=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    receipt_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerResultReceiptModel(Base):
    """保存结果提交回执；包括 ignored 结果，供重放和审计使用。"""

    __tablename__ = "worker_result_receipts"
    __table_args__ = (
        UniqueConstraint("execution_id", "result_id", name="uq_worker_result_receipt_idempotency"),
        Index("idx_worker_result_receipt_task", "task_id", "created_at"),
    )

    receipt_id: Mapped[str] = mapped_column(String, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("task_executions.execution_id"), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), nullable=False)
    result_id: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    disposition: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    next_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkerStopConfirmationModel(Base):
    """记录 Worker 对取消命令的物理停止确认，作为容量释放依据。"""

    __tablename__ = "worker_stop_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "instance_id", "confirmation_id", name="uq_worker_stop_confirmation_idempotency"
        ),
        Index("idx_worker_stop_confirmation_execution", "execution_id"),
    )

    confirmation_id: Mapped[str] = mapped_column(String, primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("worker_instances.instance_id"), primary_key=True
    )
    execution_id: Mapped[str] = mapped_column(ForeignKey("task_executions.execution_id"), nullable=False)
    cancel_command_id: Mapped[str] = mapped_column(String, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    cancellation_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    stopped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    disposition: Mapped[str] = mapped_column(String, nullable=False)
    receipt_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrchestratorRootResumeJobModel(Base):
    """以 Run、Plan 和等待代次唯一标识的 Root 恢复作业。"""

    __tablename__ = "orchestrator_root_resume_jobs"
    __table_args__ = (
        UniqueConstraint("run_id", "plan_id", "root_wait_generation", name="uq_orchestrator_root_resume_identity"),
        Index("idx_orchestrator_root_resume_ready", "status", "available_at"),
    )

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id"), nullable=False)
    root_wait_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "OrchestratorExecutionModel",
    "OrchestratorPlanModel",
    "OrchestratorRootResumeJobModel",
    "OrchestratorTaskModel",
    "OrchestratorWorkerCommandModel",
    "WorkerCommandReceiptModel",
    "WorkerContractModel",
    "WorkerInstanceModel",
    "WorkerRegistrationReceiptModel",
    "WorkerResultReceiptModel",
    "WorkerStopConfirmationModel",
]
