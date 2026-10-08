"""PostgreSQL approval persistence implementation."""

from .runtime_repositories import PostgresApprovalRepository as _RuntimeApprovalRepository


class PostgresApprovalRepository(_RuntimeApprovalRepository):
    """PostgreSQL approval persistence repository."""

__all__ = ["PostgresApprovalRepository"]
