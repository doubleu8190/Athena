"""文档解析、分块和索引业务用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

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
    ) -> None:
        self._attachments = attachments
        self._chunks = chunks
        self._parser = parser
        self._vector_indexer = vector_indexer
        self._graph_indexer = graph_indexer
        self._chunker = chunker
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def process(self, attachment_id: str) -> dict[str, int | str]:
        attachment = await self._attachments.get(attachment_id)
        if attachment is None:
            raise LookupError(attachment_id)
        processing = self._with_status(attachment, AttachmentStatus.PROCESSING)
        await self._attachments.save(processing)
        try:
            parsed = await self._parser.parse(processing)
            values = self._chunker(parsed.text)
            values = [
                __import__("dataclasses").replace(value, attachment_id=attachment_id)
                for value in values
            ]
            await self._chunks.replace_for_attachment(attachment_id, values)
            await self._vector_indexer.index(processing, values)
            if self._graph_indexer is not None:
                await self._graph_indexer.index(processing, values)
            ready = self._with_status(
                processing,
                AttachmentStatus.READY,
                metadata=parsed.metadata,
                adapter_name=parsed.adapter_name or processing.adapter_name,
                adapter_version=parsed.adapter_version or processing.adapter_version,
                capabilities=parsed.capabilities or processing.capabilities,
            )
            await self._attachments.save(ready)
            return {"attachment_id": attachment_id, "chunks": len(values), "status": ready.status.value}
        except Exception as exc:
            failed = self._with_status(processing, AttachmentStatus.FAILED, error_message=str(exc))
            await self._attachments.save(failed)
            raise

    async def delete_indexes(self, attachment_id: str) -> None:
        await self._vector_indexer.delete(attachment_id)
        if self._graph_indexer is not None:
            await self._graph_indexer.delete(attachment_id)

    def _with_status(self, value: Attachment, status: AttachmentStatus, **changes) -> Attachment:
        from dataclasses import replace

        return replace(value, status=status, updated_at=self._clock(), **changes)
