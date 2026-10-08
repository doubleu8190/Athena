"""Compatibility import for the provider-neutral approval event publisher."""

from __future__ import annotations

from infrastructure.approval.events import ApprovalEventPublisher

PostgresApprovalEventPublisher = ApprovalEventPublisher


__all__ = ["PostgresApprovalEventPublisher"]
