"""AuditLog model — audit_logs table.

Immutable security/operational audit trail. All intercept events,
permission changes, and config modifications are recorded here.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )  # e.g. 'harness_block', 'subtask_executed', 'confirm_timeout'
    actor_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    timestamp: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
