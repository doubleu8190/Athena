"""Session model — sessions table.

Tracks conversation sessions per (user_id, channel, chat_id).
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
    delete_time: Mapped[datetime | None] = mapped_column(nullable=True, default=None)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    """Cumulative summary of older messages, produced by the summarization model."""

    summary_offset: Mapped[int] = mapped_column(default=0)
    """Number of messages already included in the summary.
    Messages[:summary_offset] are summarized; Messages[summary_offset:] are raw."""

    modified_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
