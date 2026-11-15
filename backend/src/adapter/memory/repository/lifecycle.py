"""记忆生命周期、批次原子提交与运维调度的聚合门面（向后兼容导出路径）。"""

from __future__ import annotations

from adapter.memory.repository._shared import (
    MemoryBatchDiscarded,
    _mark_batch_runs_locked,
    _mark_user_runs_skipped_locked,
    _normalized_content_hash,
    _scope_key,
    disable_project_memory_scope,
    enqueue_cleanup_job,
    ensure_scan_job_locked,
    invalidate_session_memory_generation,
    lock_scope_state,
    lock_scope_states,
    lock_user_memory_setting,
    read_user_memory_generation,
)
from adapter.memory.repository.batch_extract import MemoryBatchExtractRepositoryMixin
from adapter.memory.repository.control import MemoryControlRepositoryMixin
from adapter.memory.repository.jobs import MemoryJobsRepositoryMixin
from adapter.memory.repository.retrieval import MemoryRetrievalRepositoryMixin


class MemoryLifecycleRepositoryMixin(
    MemoryRetrievalRepositoryMixin,
    MemoryBatchExtractRepositoryMixin,
    MemoryControlRepositoryMixin,
    MemoryJobsRepositoryMixin,
):
    """聚合四类高内聚生命周期能力；具体仓储负责注入 session factory。"""


__all__ = [
    "MemoryBatchDiscarded",
    "MemoryLifecycleRepositoryMixin",
    "_mark_batch_runs_locked",
    "_mark_user_runs_skipped_locked",
    "_normalized_content_hash",
    "_scope_key",
    "disable_project_memory_scope",
    "enqueue_cleanup_job",
    "ensure_scan_job_locked",
    "invalidate_session_memory_generation",
    "lock_scope_state",
    "lock_scope_states",
    "lock_user_memory_setting",
    "read_user_memory_generation",
]
