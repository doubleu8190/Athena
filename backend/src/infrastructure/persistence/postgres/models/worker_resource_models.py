"""跨进程 Worker 资源的可信元数据模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class WorkerResourceModel(Base):
    """记录资源归属；资源字节保存在独立的持久文件存储。"""

    __tablename__ = "worker_resources"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'ready')", name="ck_worker_resources_status"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_worker_resources_size_nonnegative"),
        CheckConstraint(
            "length(sha256) = 64", name="ck_worker_resources_sha256_length"
        ),
    )

    resource_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    worker_instance_id: Mapped[str] = mapped_column(
        ForeignKey("worker_instances.instance_id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("agent_plans.plan_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("task_executions.execution_id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = ["WorkerResourceModel"]
