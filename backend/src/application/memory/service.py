"""长期记忆应用服务。"""

from __future__ import annotations

from domain.memory import (
    MemoryListRequest,
    MemoryPage,
    MemoryPort,
    MemoryRecord,
    MemorySearchRequest,
    MemoryWriteCommand,
)


class MemoryService:
    """协调记忆检索、写入、版本和生命周期操作。"""

    def __init__(self, port: MemoryPort) -> None:
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

    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]:
        """执行记忆检索。"""
        return await self._port.search(request)

    async def list(self, request: MemoryListRequest) -> MemoryPage:
        """分页列出记忆。"""
        return await self._port.list(request)

    async def get(self, memory_id: str) -> MemoryRecord | None:
        """按 ID 获取记忆。"""
        return await self._port.get(memory_id)

    async def revisions(self, memory_id: str) -> list[MemoryRecord]:
        """获取记忆的不可变修订历史。"""
        return await self._port.revisions(memory_id)

    async def add(self, command: MemoryWriteCommand) -> str:
        """创建记忆并返回稳定 ID。"""
        return await self._port.add(command)

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

    async def set_validity(
        self,
        memory_id: str,
        validity_status: str,
        *,
        valid_until: str | None = None,
    ) -> bool:
        """更新事实有效性。"""
        return await self._port.set_validity(
            memory_id,
            validity_status,
            valid_until=valid_until,
        )

    async def delete(self, memory_id: str) -> None:
        """软删除一条记忆。"""
        await self._port.delete(memory_id)

    async def flush_access_stats(self) -> int:
        """刷新检索访问统计。"""
        return await self._port.flush_access_stats()


__all__ = ["MemoryService"]
