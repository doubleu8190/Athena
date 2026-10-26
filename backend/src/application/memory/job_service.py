"""记忆写入队列的管理查询用例。"""

from __future__ import annotations

from domain.memory import MemoryJobStatus, MemoryJobStatusPort


class MemoryJobManagementService:
    """读取有限、脱敏的记忆作业状态。"""

    def __init__(self, repository: MemoryJobStatusPort) -> None:
        self._repository = repository

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None
    ) -> tuple[list[MemoryJobStatus], int]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("memory_job_pagination_invalid")
        return await self._repository.list_recent(
            limit=limit, offset=offset, session_id=session_id
        )


__all__ = ["MemoryJobManagementService"]
