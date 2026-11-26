"""上下文摘要及其压缩操作的类型化领域数据。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ContextSummary:
    """一个完整提交的 Session 摘要版本。"""

    id: str
    session_id: str
    owner_id: str
    covered_through_sequence: int
    parent_summary_id: str | None
    text: str
    run_id: str
    execution_epoch: int
    operation_id: str
    policy_version_id: str
    compression_model_version_id: str
    compression_prompt_version_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SaveContextSummaryCommand:
    """在有效执行租约下提交的最终摘要。"""

    run_id: str
    worker_id: str
    execution_epoch: int
    operation_id: str
    summary_id: str
    covered_through_sequence: int
    parent_summary_id: str | None
    text: str
    policy_version_id: str
    compression_model_version_id: str
    created_at: datetime
    batch_count: int = 1


__all__ = ["ContextSummary", "SaveContextSummaryCommand"]
