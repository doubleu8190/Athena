"""供记忆应用服务使用的存储端口。"""

from __future__ import annotations

from typing import Any, Protocol


class MemoryRepository(Protocol):
    """基于 SQLite 的记忆生命周期和关键词搜索操作。"""

    async def add(self, record: dict[str, Any]) -> None:
        """写入一条记忆记录。"""
        ...

    async def flush_access_stats(self, stats: dict[str, Any], expires_at: str) -> list[dict[str, Any]]:
        """批量持久化访问统计并返回更新后的记录。"""
        ...

    async def keyword_search(self, query: str, limit: int, where: dict[str, Any] | None) -> list[dict[str, Any]]:
        """按关键词检索记忆记录。"""
        ...

    async def list(self, **filters: Any) -> list[dict[str, Any]]:
        """按过滤条件列出记忆记录。"""
        ...

    async def counts(self) -> dict[str, int]:
        """返回记忆统计计数。"""
        ...

    async def update_content(self, memory_id: str, content: str) -> str | None:
        """更新记忆内容并返回旧内容；记录不存在时返回 ``None``。"""
        ...

    async def soft_delete(self, memory_ids: list[str]) -> None:
        """软删除指定记忆。"""
        ...

    async def set_pin(self, memory_id: str, pinned: bool, expires_at: str | None) -> tuple[bool, str | None] | None:
        """更新固定状态并返回旧状态；记录不存在时返回 ``None``。"""
        ...

    async def expired_ids(self, now_iso: str) -> list[str]:
        """返回截至指定时间已过期的记忆 ID。"""
        ...

    async def hard_delete(self, memory_id: str) -> None:
        """永久删除指定记忆。"""
        ...

    async def restore_deleted(self, memory_ids: list[str]) -> None:
        """恢复指定的软删除记忆。"""
        ...


class MemoryVectorStore(Protocol):
    """可替换的向量索引端口。"""

    async def initialize(self) -> None:
        """初始化向量存储。"""
        ...

    async def add(self, memory_id: str, content: str, metadata: dict[str, Any]) -> None:
        """添加带元数据的向量记录。"""
        ...

    async def query(self, query: str, limit: int, where: dict[str, Any] | None) -> dict[str, Any]:
        """执行向量相似度检索。"""
        ...

    async def get(self, memory_id: str) -> dict[str, Any]:
        """按 ID 获取向量记录。"""
        ...

    async def update(self, memory_id: str, content: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        """更新向量记录的内容或元数据。"""
        ...

    async def delete(self, memory_ids: list[str]) -> None:
        """删除指定向量记录。"""
        ...
