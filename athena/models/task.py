"""Task model — tasks table.

Tracks top-level task execution plans, recovery state, and status.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    status: Mapped[str] = mapped_column(
        String, default="pending"
    )  # 'pending', 'running', 'recovering', 'completed', 'failed', 'cancelled', 'abandoned'
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_recovery_attempts: Mapped[int] = mapped_column(Integer, default=3)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
