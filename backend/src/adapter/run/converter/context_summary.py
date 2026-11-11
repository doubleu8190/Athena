"""上下文摘要 ORM 行与领域对象的转换。"""

from __future__ import annotations

from datetime import UTC

from adapter.run.model import SessionContextSummaryModel
from domain.run.model import ContextSummary


def context_summary_to_domain(row: SessionContextSummaryModel) -> ContextSummary:
    """把内部摘要映射为不可变领域对象。"""
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return ContextSummary(
        id=row.id,
        session_id=row.session_id,
        owner_id=row.owner_id,
        covered_through_sequence=row.covered_through_sequence,
        parent_summary_id=row.parent_summary_id,
        text=row.text,
        run_id=row.run_id,
        execution_epoch=row.execution_epoch,
        operation_id=row.operation_id,
        policy_version_id=row.policy_version_id,
        compression_model_version_id=row.compression_model_version_id,
        prompt_version=row.prompt_version,
        compression_prompt_version_id=row.compression_prompt_version_id,
        created_at=created_at,
    )


__all__ = ["context_summary_to_domain"]
