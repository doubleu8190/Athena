"""长期记忆应用服务。"""

from __future__ import annotations

from domain.memory import (
    MemoryListRequest,
    MemoryPage,
    MemoryRepositoryPort,
    MemoryRecord,
    MemoryRelationRecord,
    MemorySearchRequest,
    MemoryWriteCommand,
)
from application.common.responses import MemoryPageResponse, MemoryResponse


class MemoryService:
    """协调记忆检索、写入、版本和生命周期操作。"""

    def __init__(self, port: MemoryRepositoryPort) -> None:
        """初始化服务。

        参数：
            port: 由 infrastructure 提供的记忆能力端口。
        返回值：
            无。
        异常：
            不主动抛出业务异常。
        """
        self._port = port

    async def initialize(self) -> None:
        """初始化记忆索引等外部资源。"""
        await self._port.initialize()

    async def search(self, request: MemorySearchRequest) -> list[MemoryResponse]:
        """执行记忆检索。"""
        return [_to_response(value) for value in await self._port.search(request)]

    async def list_memories(self, request: MemoryListRequest) -> MemoryPageResponse:
        """分页列出记忆。"""
        value = await self._port.list_memories(request)
        return MemoryPageResponse(tuple(_to_response(item) for item in value.items), value.total, dict(value.stats))

    async def get(self, memory_id: str) -> MemoryResponse | None:
        """按 ID 获取记忆。"""
        value = await self._port.get(memory_id)
        return None if value is None else _to_response(value)

    async def revisions(self, memory_id: str) -> list[MemoryResponse]:
        """获取记忆的不可变修订历史。"""
        return [_to_response(value) for value in await self._port.revisions(memory_id)]

    async def related_entries(self, memory_id: str) -> list[MemoryRelationRecord]:
        """返回与记忆直接关联的替代或矛盾条目。"""
        return await self._port.related_entries(memory_id)

    async def create_memory(self, command: MemoryWriteCommand) -> str:
        """创建记忆并返回稳定 ID。"""
        return await self._port.create_memory(command)

    async def revise(
        self,
        memory_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        operation_key: str | None = None,
    ) -> str | None:
        """创建新修订版并保留旧版本。"""
        if not content.strip():
            raise ValueError("memory content must not be empty")
        return await self._port.revise(
            memory_id,
            content,
            metadata=metadata,
            operation_key=operation_key,
        )

    async def update_validity(
        self,
        memory_id: str,
        validity_status: str,
        *,
        valid_until: str | None = None,
    ) -> bool:
        """更新事实有效性。"""
        return await self._port.update_validity(
            memory_id,
            validity_status,
            valid_until=valid_until,
        )

    async def delete(self, memory_id: str) -> bool:
        """遗忘一条记忆并返回目标是否存在。"""
        return await self._port.delete(memory_id)

    async def flush_access_stats(self) -> int:
        """刷新检索访问统计。"""
        return await self._port.flush_access_stats()


__all__ = ["MemoryService"]


def _to_response(value: MemoryRecord) -> MemoryResponse:
    """将记忆实体转换为应用响应。"""
    return MemoryResponse(
        id=value.id,
        content=value.content,
        metadata=dict(value.metadata),
        score=value.score,
        source=value.source,
        status=value.status.value,
        created_at=value.created_at,
        expires_at=value.expires_at,
        last_accessed_at=value.last_accessed_at,
        access_count=value.access_count,
        last_observed_at=value.last_observed_at,
        valid_until=value.valid_until,
        validity_status=value.validity_status,

    )
