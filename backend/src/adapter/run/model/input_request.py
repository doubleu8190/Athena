"""Run 用户输入请求 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class RunInputRequestModel(Base):
    """与 Run 和同 Session 澄清消息关联的一次性回答请求。"""

    __tablename__ = "run_input_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'answered', 'closed')",
            name="ck_run_input_requests_status",
        ),
        ForeignKeyConstraint(
            ["run_id", "session_id"],
            ["runs.id", "runs.session_id"],
            name="fk_run_input_requests_run_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["question_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_run_input_requests_question_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["answer_message_id", "session_id"],
            ["conversation_messages.id", "conversation_messages.session_id"],
            name="fk_run_input_requests_answer_session",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_run_input_requests_pending",
            "run_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    question_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    answer_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["RunInputRequestModel"]
