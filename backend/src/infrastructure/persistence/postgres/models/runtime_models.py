"""ORM mappings for runs, orchestration, memory, events and retrieval.

These mappings keep the deployed PostgreSQL schema intact while the use cases
move to the target domain ports.  They deliberately contain no repositories or
application behavior.
"""

from __future__ import annotations

from sqlalchemy import Computed, ForeignKey, Index, Integer, String, Text, Float, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector

from .base import Base


class EmbeddingMetadataModel(Base):
    __tablename__ = "embedding_metadata"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)


class AgentRunModel(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("idx_agent_runs_session_status", "session_id", "status"),
        Index("one_active_run_per_session", "session_id", unique=True, postgresql_where=text("status IN ('created', 'running')")),
    )
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="created")
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_thread_id: Mapped[str] = mapped_column(String)
    root_llm_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    worker_llm_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    total_worker_llm_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    root_wait_generation: Mapped[int] = mapped_column(Integer, default=0)
    root_wait_checkpoint_id: Mapped[str | None] = mapped_column(String, nullable=True)
    root_wait_armed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class AgentCommandModel(Base):
    __tablename__ = "agent_commands"
    __table_args__ = (
        Index("idx_agent_commands_status_queued", "status", "queued_at"),
        UniqueConstraint("command_type", "idempotency_key", name="uq_agent_commands_idempotency"),
    )
    command_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    command_type: Mapped[str] = mapped_column(String)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String, default="queued")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[str] = mapped_column(String)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class AgentPlanModel(Base):
    __tablename__ = "agent_plans"
    __table_args__ = (Index("idx_agent_plans_run", "run_id"), UniqueConstraint("run_id", name="one_plan_per_run"))
    plan_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String)
    session_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="created")
    user_goal: Mapped[str] = mapped_column(Text)
    max_parallelism: Mapped[int] = mapped_column(Integer, default=1)
    plan_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class AgentTaskModel(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (Index("idx_agent_tasks_plan_status", "plan_id", "status"), Index("idx_agent_tasks_run_status", "run_id", "status"))
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("agent_plans.plan_id"))
    run_id: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    objective: Mapped[str] = mapped_column(Text)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    expected_output_json: Mapped[str] = mapped_column(Text, default="{}")
    allowed_tools_json: Mapped[str] = mapped_column(Text, default="[]")
    worker_type: Mapped[str] = mapped_column(String, default="general")
    status: Mapped[str] = mapped_column(String, default="pending")
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_generation: Mapped[int] = mapped_column(Integer, default=0)
    worker_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    llm_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class AgentEventModel(Base):
    __tablename__ = "agent_events"
    __table_args__ = (Index("idx_agent_events_run", "run_id"),)
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attachment_id: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String)
    durability: Mapped[str] = mapped_column(String)
    stream_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stream_type: Mapped[str | None] = mapped_column(String, nullable=True)
    is_complete: Mapped[int] = mapped_column(Integer, default=0)
    parent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    transition_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    occurred_at: Mapped[str] = mapped_column(String)


class ApprovalRecordModel(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("idx_approvals_session_status", "session_id", "status"),)
    approval_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str] = mapped_column(String)
    approval_batch_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    plan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    worker_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_call_id: Mapped[str] = mapped_column(String)
    tool_name: Mapped[str] = mapped_column(String)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    risk_level: Mapped[str] = mapped_column(String, default="low")
    status: Mapped[str] = mapped_column(String, default="pending")
    decision: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)


class StepModel(Base):
    __tablename__ = "steps"
    __table_args__ = (Index("idx_steps_session", "session_id"), Index("idx_steps_run", "run_id"), Index("idx_steps_number", "step_number"), Index("idx_steps_parent", "parent_step_id"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str] = mapped_column(String)
    step_number: Mapped[int] = mapped_column(Integer)
    step_type: Mapped[str] = mapped_column(String)
    parent_step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class ToolCallModel(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (Index("idx_tool_calls_session", "session_id"), Index("idx_tool_calls_step", "step_id"), Index("idx_tool_calls_status", "status"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String)
    step_id: Mapped[str | None] = mapped_column(String, ForeignKey("steps.id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_name: Mapped[str] = mapped_column(String)
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float] = mapped_column(Float, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class MemoryModel(Base):
    __tablename__ = "memories"
    __table_args__ = (Index("idx_memories_session", "session_id"), Index("idx_memories_content_fts", "content_fts", postgresql_using="gin"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    logical_memory_id: Mapped[str] = mapped_column(String, index=True)
    session_id: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    content_fts: Mapped[str] = mapped_column(TSVECTOR, Computed("to_tsvector('chinese'::regconfig, content)", persisted=True))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    last_accessed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    memory_type: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    source_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_observed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    validity_status: Mapped[str] = mapped_column(String, default="valid")
    valid_until: Mapped[str | None] = mapped_column(String, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    deleted_time: Mapped[str | None] = mapped_column(String, nullable=True)


class KnowledgeDocumentJobModel(Base):
    __tablename__ = "knowledge_document_jobs"
    __table_args__ = (Index("idx_knowledge_document_jobs_queue", "status", "available_at"),)
    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id"), unique=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    updated_at: Mapped[str] = mapped_column(String)


class CodeSymbolModel(Base):
    __tablename__ = "code_symbols"
    __table_args__ = (Index("idx_code_symbols_attachment", "attachment_id"), Index("idx_code_symbols_name", "name"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id"))
    path: Mapped[str] = mapped_column(String)
    language: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    qualified_name: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)


class CodeDependencyModel(Base):
    __tablename__ = "code_dependencies"
    __table_args__ = (Index("idx_code_deps_attachment", "attachment_id"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id"))
    source: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="reference")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class RetrievalRunModel(Base):
    __tablename__ = "retrieval_runs"
    __table_args__ = (Index("idx_retrieval_runs_created_at", "created_at"), Index("idx_retrieval_runs_scope", "scope"), Index("idx_retrieval_runs_session", "session_id"), Index("idx_retrieval_runs_agent_run", "agent_run_id"), Index("idx_retrieval_runs_message", "message_id"))
    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="running")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    index_generation: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    injected_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)


class RetrievalCandidateModel(Base):
    __tablename__ = "retrieval_candidates"
    __table_args__ = (Index("idx_retrieval_candidates_run_stage", "run_id", "stage"), Index("idx_retrieval_candidates_source", "source_type", "source_id"))
    candidate_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("retrieval_runs.run_id"), index=True)
    provider: Mapped[str] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String)
    source_type: Mapped[str] = mapped_column(String)
    source_id: Mapped[str] = mapped_column(String)
    logical_source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    native_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    native_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fused_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fused_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rerank_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rerank_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    filter_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_for_result: Mapped[int] = mapped_column(Integer, default=0)
    injected_into_context: Mapped[int] = mapped_column(Integer, default=0)
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_title: Mapped[str | None] = mapped_column(String, nullable=True)
    locator_json: Mapped[str] = mapped_column(Text, default="{}")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String)


__all__ = [name for name in globals() if name.endswith("Model")]
