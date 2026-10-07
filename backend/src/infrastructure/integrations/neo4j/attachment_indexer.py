"""Optional Neo4j graph index adapter for file attachments."""

from __future__ import annotations

from collections.abc import Sequence

from domain.files import Attachment, FileChunk

from .resource import Neo4jResource

class OptionalNeo4jAttachmentIndexer:
    """Delegate attachment indexing when the Neo4j resource is available."""

    def __init__(self, resource: Neo4jResource) -> None:
        """保存 Neo4j 生命周期资源；资源未启动时跳过索引操作。"""
        self._resource = resource

    async def index(self, attachment: Attachment, chunks: Sequence[FileChunk]) -> None:
        """将附件及其分块写入已启动的 Neo4j adapter。"""
        if self._resource.adapter is not None:
            await self._resource.adapter.index_attachment(attachment, chunks)

    async def delete(self, attachment_id: str) -> None:
        """删除附件对应的 Neo4j 派生节点。"""
        if self._resource.adapter is not None:
            await self._resource.adapter.delete_attachment(attachment_id)


__all__ = ["OptionalNeo4jAttachmentIndexer"]
