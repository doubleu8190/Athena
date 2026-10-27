"""文档解析、分块和索引业务用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4
import asyncio

from domain.files import (
    Attachment,
    AttachmentRepository,
    AttachmentStatus,
    DocumentParserPort,
    FileChunk,
    FileChunkRepository,
    GraphIndexerPort,
    ParsedDocument,
    VectorIndexerPort,
)
from domain.leases import LeaseCenterPort, LeaseGrant, LeaseKey


class IngestionService:
    """将已上传附件推进到解析、分块、向量和图索引状态。"""

    def __init__(
        self,
        attachments: AttachmentRepository,
        chunks: FileChunkRepository,
        parser: DocumentParserPort,
        vector_indexer: VectorIndexerPort,
        graph_indexer: GraphIndexerPort | None = None,
        *,
        chunker: Callable[[str], list[FileChunk]],
        clock: Callable[[], datetime] | None = None,
        lease_center: LeaseCenterPort | None = None,
    ) -> None:
        self._attachments = attachments
        self._chunks = chunks
        self._parser = parser
        self._vector_indexer = vector_indexer
        self._graph_indexer = graph_indexer
        self._chunker = chunker
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._leases = lease_center or getattr(chunks, "_leases")

    async def process_attachment(
        self, attachment_id: str, *, lease: LeaseGrant
    ) -> dict[str, object]:
        if lease.key != LeaseKey("document_ingestion", attachment_id):
            raise ValueError("document_ingestion_lease_mismatch")
        await self._leases.assert_current(lease)
        attachment = await self._attachments.get(attachment_id)
        if attachment is None:
            raise LookupError(attachment_id)
        if attachment.status is AttachmentStatus.DELETED:
            raise LookupError("attachment_deleted")
        parsed = await self._parser.parse(attachment)
        revision_id = str(uuid4())
        parsed_chunks = await asyncio.to_thread(self._chunker, parsed.text)
        from dataclasses import replace

        values = [
            replace(
                value,
                id=str(uuid4()),
                attachment_id=attachment_id,
                index_revision_id=revision_id,
            )
            for value in parsed_chunks
        ]
        await self._chunks.replace_for_attachment(attachment_id, values, lease=lease)
        await self._vector_indexer.index_attachment(attachment, values, lease=lease)
        if self._graph_indexer is not None:
            await self._leases.assert_current(lease)
            await self._graph_indexer.index_attachment(attachment, values, lease=lease)
        return {
            "attachment_id": attachment_id,
            "chunks": len(values),
            "status": AttachmentStatus.READY.value,
            "index_revision_id": revision_id,
            "_parsed_metadata": parsed.metadata.values,
            "_adapter_name": parsed.adapter_name or attachment.adapter_name,
            "_adapter_version": parsed.adapter_version or attachment.adapter_version,
            "_capabilities": list(parsed.capabilities or attachment.capabilities),
        }

    async def delete_indexes(self, attachment_id: str) -> None:
        await self._vector_indexer.delete_attachment(attachment_id)
        if self._graph_indexer is not None:
            await self._graph_indexer.delete_attachment(attachment_id)
