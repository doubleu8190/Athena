"""Session model — sessions table.

Tracks conversation sessions per (user_id, channel, chat_id).
context_snapshot is a JSON column serving as cold backup / recovery point.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class Session(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String, nullable=False)
    chat_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, default="active"
    )  # 'active', 'idle', 'expired'
    context_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    last_active_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
