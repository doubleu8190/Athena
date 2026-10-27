"""文件向量和图索引端口。"""

from __future__ import annotations

from typing import Protocol
from domain.leases import LeaseGrant

from .entities import Attachment, FileChunk


class VectorIndexerPort(Protocol):
    """附件向量索引写入能力。"""

    async def index_attachment(self, attachment: Attachment, chunks: list[FileChunk], *, lease: LeaseGrant) -> None: ...
    async def delete_attachment(self, attachment_id: str) -> None: ...


class FileVectorSearchPort(Protocol):
    """按查询文本检索文件分块向量。"""

    async def query(
        self,
        text: str,
        limit: int = 10,
        *,
        attachment_ids: set[str] | None = None,
    ) -> list[dict[str, object]]: ...


class GraphIndexerPort(Protocol):
    """附件图索引写入能力。"""

    async def index_attachment(self, attachment: Attachment, chunks: list[FileChunk], *, lease: LeaseGrant) -> None: ...
    async def delete_attachment(self, attachment_id: str) -> None: ...


__all__ = ["FileVectorSearchPort", "GraphIndexerPort", "VectorIndexerPort"]
