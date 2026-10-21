"""持久化 Plan 版本、Round、Execution 和消息投递记录。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
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
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base

_JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class PlanVersionModel(Base):
    """保存不可变的 Plan DAG 版本。"""
    __tablename__ = "plan_versions"

    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    dag_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    based_on_round_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlanRoundModel(Base):
    """保存一次 Root Review 对应的固定任务轮次。"""
    __tablename__ = "plan_rounds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_version"],
            ["plan_versions.plan_id", "plan_versions.version"],
            name="fk_plan_round_version",
        ),
        UniqueConstraint("plan_id", "round_number", name="uq_plan_round_number"),
        Index("uq_plan_round_active", "plan_id", unique=True, postgresql_where=text("status = 'running'")),
    )

    round_id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(String, nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default="running")
    expected_task_count: Mapped[int] = mapped_column(Integer, default=0)
    terminal_task_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RoundTaskModel(Base):
    """保存 Round 启动时冻结的 Task 成员。"""

    __tablename__ = "round_tasks"
    __table_args__ = (
        Index("idx_round_tasks_round_state", "round_id", "status"),
    )

    round_id: Mapped[str] = mapped_column(ForeignKey("plan_rounds.round_id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.task_id"), primary_key=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    active_execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskExecutionModel(Base):
    """保存 Task 的独立执行尝试和租约。"""
    __tablename__ = "task_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "task_id"],
            ["round_tasks.round_id", "round_tasks.task_id"],
            name="fk_task_execution_round_task",
        ),
        UniqueConstraint("task_id", "attempt", name="uq_task_execution_attempt"),
        UniqueConstraint("worker_thread_id", name="uq_task_execution_thread"),
        Index("idx_task_executions_lease", "status", "lease_until"),
    )

    execution_id: Mapped[str] = mapped_column(String, primary_key=True)
    round_id: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_thread_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="queued")
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    cancellation_epoch: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrchestrationCommandModel(Base):
    """保存命令幂等键、请求摘要和稳定回执。"""
    __tablename__ = "orchestration_commands"
    __table_args__ = (
        UniqueConstraint("scope_key", "idempotency_key", name="uq_orchestration_command_idempotency"),
        Index("idx_orchestration_commands_plan", "plan_id", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    scope_key: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    command_type: Mapped[str] = mapped_column(String, nullable=False)
    plan_id: Mapped[str] = mapped_column(String, nullable=False)
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    round_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="accepted")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrchestrationOutboxModel(Base):
    """保存业务事务产生、可重复投递的事件。"""
    __tablename__ = "orchestration_outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_orchestration_outbox_dedupe"),
        Index("idx_orchestration_outbox_ready", "status", "available_at"),
    )

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String, nullable=False)
    plan_id: Mapped[str] = mapped_column(String, nullable=False)
    plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    round_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrchestrationInboxModel(Base):
    """保存消费者收到和处理中的消息。"""
    __tablename__ = "orchestration_inbox"
    __table_args__ = (Index("idx_orchestration_inbox_state", "consumer_name", "status", "created_at"),)

    consumer_name: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, default=dict)
    payload_hash: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="received")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    receipt_json: Mapped[dict[str, Any] | None] = mapped_column(_JSON_DOCUMENT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerJobModel(Base):
    """保存 Worker 执行、恢复和取消作业。"""
    __tablename__ = "worker_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_worker_jobs_dedupe"),
        Index("idx_worker_jobs_ready", "status", "available_at"),
    )

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    job_type: Mapped[str] = mapped_column(String, nullable=False)
    execution_id: Mapped[str | None] = mapped_column(ForeignKey("task_executions.execution_id"), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.approval_id"), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(_JSON_DOCUMENT, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RootResumeJobModel(Base):
    """保存由轮次完成事件触发的 Root 恢复作业。"""
    __tablename__ = "root_resume_jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_root_resume_jobs_dedupe"),
        Index("idx_root_resume_jobs_ready", "status", "available_at"),
    )

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id"), nullable=False)
    round_id: Mapped[str] = mapped_column(ForeignKey("plan_rounds.round_id"), nullable=False)
    root_wait_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = [
    "OrchestrationCommandModel",
    "OrchestrationInboxModel",
    "OrchestrationOutboxModel",
    "PlanRoundModel",
    "PlanVersionModel",
    "RootResumeJobModel",
    "RoundTaskModel",
    "TaskExecutionModel",
    "WorkerJobModel",
]
