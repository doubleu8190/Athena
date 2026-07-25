"""Initial schema — all 6 tables + indexes.

Includes:
- sessions: conversation sessions per (user_id, channel, chat_id)
- mcp_servers: MCP server registrations (builtin, skill, external)
- skills: installed Docker-based skills
- device_registry: registered device agents
- harness_rules: security rule definitions
- audit_logs: immutable audit trail
"""

from __future__ import annotations

from datetime import datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all initial tables and indexes."""
    # ── sessions ──────────────────────────────────────────────────────
    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        sa.Column("delete_time", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_offset", sa.Integer(), nullable=False),
        sa.Column("last_extracted_message_count", sa.Integer(), nullable=False),
        sa.Column("modified_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)

    # ── mcp_servers ───────────────────────────────────────────────────
    op.create_table(
        "mcp_servers",
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("connection_config", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("server_id"),
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
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("installed_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("skill_id"),
    )

    # ── device_registry ───────────────────────────────────────────────
    op.create_table(
        "device_registry",
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("connection_info", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
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
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("rule_id"),
    )
    op.create_index(op.f("ix_harness_rules_rule_type"), "harness_rules", ["rule_type"], unique=False)

    # ── audit_logs ────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.String(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(op.f("ix_audit_logs_event_type"), "audit_logs", ["event_type"], unique=False)
    op.create_index(op.f("ix_audit_logs_timestamp"), "audit_logs", ["timestamp"], unique=False)


def downgrade() -> None:
    """Drop all tables."""
    op.drop_index(op.f("ix_audit_logs_timestamp"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_event_type"), table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index(op.f("ix_harness_rules_rule_type"), table_name="harness_rules")
    op.drop_table("harness_rules")

    op.drop_table("device_registry")

    op.drop_table("skills")

    op.drop_table("mcp_servers")

    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
