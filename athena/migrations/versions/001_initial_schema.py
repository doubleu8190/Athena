"""Initial schema — all 10 tables + 12 indexes.

Revision ID: 001
Revises: None
Create Date: 2026-06-11

Tables: sessions, user_memories, tasks, subtask_executions, mcp_servers,
        tools, skills, device_registry, harness_rules, audit_logs,
        token_usage_log

Includes partial indexes for recovery scanning and cooling-off lookups.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── sessions ─────────────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        sa.Column("delete_time", sa.DateTime(), nullable=True),
        sa.Column("modified_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index("idx_sessions_user", "sessions", ["user_id", "channel"])
    op.create_index("idx_sessions_chat", "sessions", ["user_id", "channel", "chat_id"])

    # ── user_memories ─────────────────────────────────────────────────
    op.create_table(
        "user_memories",
        sa.Column("memory_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("vector_id", sa.String(), nullable=True),
        sa.Column("sync_status", sa.String(), server_default="pending"),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index("idx_memories_user", "user_memories", ["user_id"])
    op.create_index("idx_memories_sync_status", "user_memories", ["sync_status"])
    op.create_index("idx_memories_vector_id", "user_memories", ["vector_id"])

    # ── tasks ─────────────────────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending"),
        sa.Column("recovery_attempts", sa.Integer(), server_default="0"),
        sa.Column("max_recovery_attempts", sa.Integer(), server_default="3"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        "idx_tasks_recovery",
        "tasks",
        ["status", "updated_at"],
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )

    # ── mcp_servers ───────────────────────────────────────────────────
    op.create_table(
        "mcp_servers",
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("connection_config", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), server_default="disconnected"),
        sa.Column("source", sa.String(), server_default="external"),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("server_id"),
    )

    # ── tools ─────────────────────────────────────────────────────────
    op.create_table(
        "tools",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("parameters_schema", sa.Text(), nullable=True),
        sa.Column("risk_level", sa.String(), server_default="medium"),
        sa.Column("supports_preview", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("idempotent", sa.Boolean(), server_default=sa.text("1")),
        sa.Column("capability_tags", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_server_id", sa.String(), nullable=False),
        sa.Column("handler_info", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["source_server_id"], ["mcp_servers.server_id"]
        ),
    )

    # ── skills ────────────────────────────────────────────────────────
    op.create_table(
        "skills",
        sa.Column("skill_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("image_uri", sa.String(), nullable=True),
        sa.Column("allowed_domains", sa.Text(), nullable=True),
        sa.Column("container_id", sa.String(), nullable=True),
        sa.Column("mcp_server_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="installed"),
        sa.Column("installed_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("skill_id"),
    )

    # ── device_registry ───────────────────────────────────────────────
    op.create_table(
        "device_registry",
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("connection_info", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="offline"),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("device_id"),
    )

    # ── harness_rules ─────────────────────────────────────────────────
    op.create_table(
        "harness_rules",
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("rule_type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0"),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("revision", sa.Integer(), server_default="1"),
        sa.PrimaryKeyConstraint("rule_id"),
    )
    op.create_index(
        "idx_harness_rules_type",
        "harness_rules",
        ["rule_type", "enabled"],
    )
    op.create_index(
        "idx_harness_rules_cooling_off",
        "harness_rules",
        ["rule_type", "enabled"],
        postgresql_where=sa.text("rule_type = 'cooling_off'"),
        sqlite_where=sa.text("rule_type = 'cooling_off'"),
    )

    # ── subtask_executions ────────────────────────────────────────────
    op.create_table(
        "subtask_executions",
        sa.Column("execution_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=True),
        sa.Column("mcp_server_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending"),
        sa.Column("retry_count", sa.Integer(), server_default="0"),
        sa.Column("is_recovered", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.text("0")),
        sa.Column("fallback_from", sa.String(), nullable=True),
        sa.Column("fallback_depth", sa.Integer(), server_default="0"),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("input_args", sa.Text(), nullable=True),
        sa.Column("output_preview", sa.Text(), nullable=True),
        sa.Column("token_usage_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
    )
    op.create_index(
        "idx_subtask_task", "subtask_executions", ["task_id", "step"]
    )
    op.create_index(
        "idx_subtask_idempotency",
        "subtask_executions",
        ["task_id", "step", "retry_count"],
    )

    # ── audit_logs ────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "idx_audit_type_time", "audit_logs", ["event_type", "timestamp"]
    )

    # ── token_usage_log ───────────────────────────────────────────────
    op.create_table(
        "token_usage_log",
        sa.Column("log_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("step", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(), server_default="planner"),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), server_default="0"),
        sa.Column("total_tokens", sa.Integer(), server_default="0"),
        sa.Column("recorded_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("log_id"),
    )
    op.create_index(
        "idx_token_usage_session",
        "token_usage_log",
        ["session_id", "recorded_at"],
    )
    op.create_index(
        "idx_token_usage_source",
        "token_usage_log",
        ["source", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_table("token_usage_log")
    op.drop_table("audit_logs")
    op.drop_table("subtask_executions")
    op.drop_table("harness_rules")
    op.drop_table("device_registry")
    op.drop_table("skills")
    op.drop_table("tools")
    op.drop_table("mcp_servers")
    op.drop_table("tasks")
    op.drop_table("user_memories")
    op.drop_table("sessions")
