"""Replace mcp_servers.status (String) with enabled (Boolean).

Revision ID: 002
Revises: 001
Create Date: 2026-06-23

The status column conflated admin intent (enabled/disabled) with runtime
connection state (connected/disconnected).  This migration replaces it with
a simple enabled boolean.  Connection state is now managed in-memory by
MCPClient.

Strategy: SQLite has limited ALTER TABLE support, so we rebuild the table:
  1. Create _mcp_servers_new with the desired schema
  2. Copy data, mapping status values: 'disabled' -> 0, everything else -> 1
  3. Drop the old table
  4. Rename the new table

Idempotent: if the table already has the target schema (enabled column,
no status column), the migration is a no-op.  This handles the case where
the Celery worker or another process has already applied the schema change.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_mcp_servers_columns(conn) -> set[str]:
    """Return the set of column names in the mcp_servers table."""
    result = conn.execute(sa.text("PRAGMA table_info('mcp_servers')"))
    return {row[1] for row in result.fetchall()}


def upgrade() -> None:
    conn = op.get_bind()

    # ── Idempotency check ─────────────────────────────────────────────
    # If the table already has 'enabled' and no 'status', the migration
    # has already been applied (e.g. by a parallel Celery worker process).
    columns = _get_mcp_servers_columns(conn)
    if "enabled" in columns and "status" not in columns:
        # Already migrated — nothing to do
        return

    # ── Clean up from prior failed attempts ───────────────────────────
    # If _mcp_servers_new exists from a previous partial run, drop it.
    result = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='_mcp_servers_new'")
    )
    if result.fetchone():
        op.drop_table("_mcp_servers_new")

    # ── 1. Create the new table with enabled (Boolean) instead of status ──
    op.create_table(
        "_mcp_servers_new",
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("connection_config", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("source", sa.String(), server_default="external"),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("server_id"),
    )

    # ── 2. Copy data ──────────────────────────────────────────────────
    if "status" in columns:
        # Old schema: map status values to boolean
        # 'disabled' -> False (0), everything else -> True (1)
        op.execute(
            "INSERT INTO _mcp_servers_new "
            "(server_id, name, transport, connection_config, enabled, source, registered_at) "
            "SELECT server_id, name, transport, connection_config, "
            "CASE WHEN status = 'disabled' THEN 0 ELSE 1 END, "
            "source, registered_at "
            "FROM mcp_servers"
        )
    else:
        # status column doesn't exist — copy as-is (already has enabled-like data)
        # Fallback: just copy with enabled=True as default
        op.execute(
            "INSERT INTO _mcp_servers_new "
            "(server_id, name, transport, connection_config, enabled, source, registered_at) "
            "SELECT server_id, name, transport, connection_config, 1, "
            "source, registered_at "
            "FROM mcp_servers"
        )

    # ── 3. Drop the old table ─────────────────────────────────────────
    op.drop_table("mcp_servers")

    # ── 4. Rename the new table ───────────────────────────────────────
    op.rename_table("_mcp_servers_new", "mcp_servers")


def downgrade() -> None:
    conn = op.get_bind()

    # ── Idempotency check ─────────────────────────────────────────────
    # If the table already has 'status' and no 'enabled', migration
    # was already downgraded.
    columns = _get_mcp_servers_columns(conn)
    if "status" in columns and "enabled" not in columns:
        return

    # ── Clean up from prior failed attempts ───────────────────────────
    result = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='_mcp_servers_old'")
    )
    if result.fetchone():
        op.drop_table("_mcp_servers_old")

    # ── 1. Create a table with the old schema ─────────────────────────
    op.create_table(
        "_mcp_servers_old",
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("connection_config", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), server_default="disconnected"),
        sa.Column("source", sa.String(), server_default="external"),
        sa.Column("registered_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("server_id"),
    )

    # ── 2. Copy data back ─────────────────────────────────────────────
    if "enabled" in columns:
        # New schema: enabled=0 -> 'disabled', enabled=1 -> 'disconnected'
        op.execute(
            "INSERT INTO _mcp_servers_old "
            "(server_id, name, transport, connection_config, status, source, registered_at) "
            "SELECT server_id, name, transport, connection_config, "
            "CASE WHEN enabled = 0 THEN 'disabled' ELSE 'disconnected' END, "
            "source, registered_at "
            "FROM mcp_servers"
        )
    else:
        # enabled column doesn't exist — copy as-is
        op.execute(
            "INSERT INTO _mcp_servers_old "
            "(server_id, name, transport, connection_config, status, source, registered_at) "
            "SELECT server_id, name, transport, connection_config, 'disconnected', "
            "source, registered_at "
            "FROM mcp_servers"
        )

    # ── 3. Drop the new table ─────────────────────────────────────────
    op.drop_table("mcp_servers")

    # ── 4. Rename the old-style table back ────────────────────────────
    op.rename_table("_mcp_servers_old", "mcp_servers")
