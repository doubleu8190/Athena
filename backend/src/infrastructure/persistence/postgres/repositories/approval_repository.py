"""PostgreSQL approval persistence implementation."""

from domain.approval import ApprovalRepository

from ._runtime import PostgresApprovalRepository as _RuntimeApprovalRepository


class PostgresApprovalRepository(_RuntimeApprovalRepository, ApprovalRepository):
    """PostgreSQL approval persistence repository."""

__all__ = ["PostgresApprovalRepository"]
