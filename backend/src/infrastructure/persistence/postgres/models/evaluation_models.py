"""ORM models for immutable evaluation datasets and runs."""

from __future__ import annotations

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class EvalDatasetModel(Base):
    __tablename__ = "eval_datasets"
    __table_args__ = (Index("idx_eval_datasets_name", "name"),)

    dataset_id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    task_type: Mapped[str] = mapped_column(default="retrieval")
    owner: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[str] = mapped_column()
    updated_at: Mapped[str] = mapped_column()


class EvalDatasetVersionModel(Base):
    __tablename__ = "eval_dataset_versions"
    __table_args__ = (UniqueConstraint("manifest_sha256", name="uq_eval_dataset_versions_manifest"), Index("idx_eval_dataset_versions_dataset", "dataset_id"))

    version_id: Mapped[str] = mapped_column(primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("eval_datasets.dataset_id"), index=True)
    version: Mapped[str] = mapped_column()
    manifest_sha256: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column(default="frozen")
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    manifest_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column()


class EvalCaseModel(Base):
    __tablename__ = "eval_cases"
    __table_args__ = (Index("idx_eval_cases_version", "version_id"),)

    case_id: Mapped[str] = mapped_column(primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("eval_dataset_versions.version_id"), primary_key=True, index=True)
    task_type: Mapped[str] = mapped_column()
    case_json: Mapped[str] = mapped_column(Text, default="{}")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[str] = mapped_column()


class EvalRunModel(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (Index("idx_eval_runs_created_at", "created_at"), Index("idx_eval_runs_version", "version_id"), Index("idx_eval_runs_status", "status"))

    run_id: Mapped[str] = mapped_column(primary_key=True)
    version_id: Mapped[str] = mapped_column(index=True)
    mode: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column(default="queued")
    name: Mapped[str] = mapped_column()
    git_sha: Mapped[str | None] = mapped_column(nullable=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    model_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    judge_config_json: Mapped[str] = mapped_column(Text, default="{}")
    baseline_run_id: Mapped[str | None] = mapped_column(nullable=True)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[str | None] = mapped_column(nullable=True)
    completed_at: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[str] = mapped_column()
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalCaseAttemptModel(Base):
    __tablename__ = "eval_case_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", "attempt_number", name="uq_eval_case_attempt"),
        ForeignKeyConstraint(["version_id", "case_id"], ["eval_cases.version_id", "eval_cases.case_id"]),
        Index("idx_eval_attempts_run_case", "run_id", "case_id"),
    )

    attempt_id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.run_id"), index=True)
    case_id: Mapped[str] = mapped_column(index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("eval_dataset_versions.version_id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column()
    input_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    output_text: Mapped[str] = mapped_column(Text, default="")
    trace_json: Mapped[str] = mapped_column(Text, default="{}")
    retrieval_run_id: Mapped[str | None] = mapped_column(nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column()


class EvalMetricValueModel(Base):
    __tablename__ = "eval_metric_values"
    __table_args__ = (Index("idx_eval_metrics_run_name", "run_id", "metric_name"), Index("idx_eval_metrics_case", "case_id"))

    metric_id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.run_id"), index=True)
    case_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    version_id: Mapped[str | None] = mapped_column(nullable=True, index=True)
    attempt_id: Mapped[str | None] = mapped_column(nullable=True)
    metric_name: Mapped[str] = mapped_column()
    value_numeric: Mapped[float | None] = mapped_column(nullable=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    evaluator_version: Mapped[str] = mapped_column(default="1")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[str] = mapped_column()


class EvalGateModel(Base):
    __tablename__ = "eval_gates"
    __table_args__ = (Index("idx_eval_gates_run", "run_id"),)

    gate_id: Mapped[str] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.run_id"), index=True)
    metric: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column()
    rule_json: Mapped[str] = mapped_column(Text, default="{}")
    actual: Mapped[float | None] = mapped_column(nullable=True)
    baseline: Mapped[float | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column()


__all__ = [name for name in globals() if name.endswith("Model")]
