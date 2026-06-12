"""TokenUsageLog model — token_usage_log table.

Per-LLM-call token consumption records. Used for cost analysis and
budget tracking. source field distinguishes: planner, executor,
summarizer, context_compressor.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class TokenUsageLog(Base):
    __tablename__ = "token_usage_log"

    log_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(
        String, default="planner", index=True
    )  # 'planner', 'executor', 'summarizer', 'context_compressor'
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
