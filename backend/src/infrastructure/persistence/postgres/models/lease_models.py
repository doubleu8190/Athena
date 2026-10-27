"""所有 Worker 共用的执行租约 ORM 模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ExecutionLeaseModel(Base):
    """以资源身份为主键保存当前授权和单调 fencing token。"""

    __tablename__ = "execution_leases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('active', 'revoked', 'released')",
            name="ck_execution_leases_state",
        ),
        CheckConstraint(
            "(state = 'released' AND owner_id IS NULL AND lease_until IS NULL) "
            "OR (state IN ('active', 'revoked') AND owner_id IS NOT NULL "
            "AND lease_until IS NOT NULL)",
            name="ck_execution_leases_owner_state",
        ),
        CheckConstraint(
            "fencing_token >= 0", name="ck_execution_leases_token_nonnegative"
        ),
        Index("idx_execution_leases_expiry", "state", "lease_until"),
    )

    resource_type: Mapped[str] = mapped_column(String, primary_key=True)
    resource_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String, default="released")
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
