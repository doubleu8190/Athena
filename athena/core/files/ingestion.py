"""Attachment parsing, persistence, and vector-index lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from athena.config.settings import Settings
from athena.core.files.adapter_registry import AdapterRegistry
from athena.core.files.chunking import FileChunker
from athena.core.files.extraction import ExtractionContext
from athena.core.files.storage import StorageLayer
from athena.core.files.ports import FileVectorStore
from athena.core.graph.indexing import GraphDocumentIndexer
from athena.infrastructure.postgre.repositories.file_repository import FileRepository
from athena.models.file import AttachmentStatus
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class FileIngestionService:
    """Own the stateful parse/index workflow for an attachment."""

    def __init__(
        self,
        repository: FileRepository,
        *,
        settings: Settings,
        storage: StorageLayer,
        adapter_registry: AdapterRegistry,
        vector_store: FileVectorStore,
        chunker: FileChunker,
        cache_key: Callable[..., str],
        emit_attachment: Callable[..., Awaitable[None]],
        initialize: Callable[[], Awaitable[None]],
        graph_indexer: GraphDocumentIndexer | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.storage = storage
        self.adapter_registry = adapter_registry
        self.vector_store = vector_store
        self.chunker = chunker
        self.cache_key = cache_key
        self.emit_attachment = emit_attachment
        self.initialize = initialize
        self.graph_indexer = graph_indexer
        self._locks: dict[str, asyncio.Lock] = {}

    async def parse_attachment(
        self, attachment_id: str, *, run_id: str | None = None
    ) -> dict[str, Any]:
        attachment = await self.repository.get_attachment(attachment_id)
        if attachment is None:
            raise FileNotFoundError("附件不存在")
        adapter = self.adapter_registry.select(attachment.filename, attachment.mime_type)
        processing = await self.repository.update_attachment(
            attachment.id,
            status=AttachmentStatus.PROCESSING.value,
            adapter_name=adapter.info.name,
            adapter_version=adapter.info.version,
            capabilities=adapter.info.capabilities,
            error_message=None,
        )
        if processing is None:
            raise RuntimeError("无法更新附件状态")
        logger.info("file_processing_started", attachment_id=attachment.id, run_id=run_id)
        await self.emit_attachment(processing, run_id=run_id)
        path = self.storage.resolve(attachment.storage_key)
        workspace = self.storage.create_workspace(attachment.id)
        try:
            result = await adapter.extract(
                ExtractionContext(
                    path=path,
                    workspace=workspace,
                    filename=attachment.filename,
                    mime_type=attachment.mime_type,
                ),
                self.settings,
            )
            for unit in result.units:
                if unit.locator.get("path") == path.name:
                    unit.locator["path"] = attachment.filename
            for symbol in result.symbols:
                if symbol.get("path") == path.name:
                    symbol["path"] = attachment.filename
            for dependency in result.dependencies:
                if dependency.get("source") == path.name:
                    dependency["source"] = attachment.filename
            chunks = self.chunker.chunk(
                attachment.id,
                result.units,
                max_tokens=self.settings.file_chunk_tokens,
                overlap_tokens=self.settings.file_chunk_overlap_tokens,
            )
            await self.repository.replace_chunks(attachment.id, chunks)
            await self.repository.replace_code_index(
                attachment.id, result.symbols, result.dependencies
            )
            if result.tables:
                key = self.cache_key(
                    attachment,
                    "tables",
                    {},
                    adapter.info.version,
                    self.settings.primary_llm.model,
                )
                await self.repository.put_artifact(
                    attachment.id,
                    "tables",
                    key,
                    json.dumps(result.tables, ensure_ascii=False, default=str),
                    {"count": len(result.tables)},
                )
            metadata = {
                **result.metadata,
                "chunk_count": len(chunks),
                "symbol_count": len(result.symbols),
                "dependency_count": len(result.dependencies),
                "document_version": attachment.document_version,
            }
            await self.repository.update_attachment(attachment.id, metadata=metadata)
            return metadata
        finally:
            self.storage.cleanup_workspace(workspace)

    async def index_attachment(self, attachment_id: str) -> dict[str, Any]:
        attachment = await self.repository.get_attachment(attachment_id)
        if attachment is None:
            raise FileNotFoundError("附件不存在")
        chunks = await self.repository.get_chunks(attachment_id, limit=100_000)
        try:
            await self.vector_store.replace_attachment(
                attachment_id,
                [
                    {
                        "id": chunk.id,
                        "content": chunk.content,
                        "metadata": {
                            "attachment_id": attachment_id,
                            "document_version": attachment.document_version,
                            "ordinal": chunk.ordinal,
                            "locator_json": json.dumps(
                                chunk.locator.model_dump(mode="json", exclude_none=True),
                                ensure_ascii=False,
                            ),
                        },
                    }
                    for chunk in chunks
                ],
            )
        except Exception as exc:
            logger.warning(
                "file_embedding_index_failed", attachment_id=attachment_id, error=str(exc)
            )
            raise
        return {"chunks": len(chunks)}

    async def process_knowledge_document(self, attachment_id: str) -> dict[str, Any]:
        async with self._lock(attachment_id):
            await self.initialize()
            attachment = await self.repository.get_attachment(attachment_id)
            if attachment is None or attachment.knowledge_base_id is None:
                raise FileNotFoundError("知识库文档不存在")
            metadata = await self.parse_attachment(attachment_id)
            attachment = await self.repository.get_attachment(attachment_id)
            if attachment is None or attachment.knowledge_base_id is None:
                raise FileNotFoundError("知识库文档已删除")
            indexed = await self.index_attachment(attachment_id)
            updated = await self.repository.update_attachment(
                attachment_id, status=AttachmentStatus.READY.value, error_message=None
            )
            if updated is None:
                await self.delete_attachment_vectors(attachment_id)
                raise FileNotFoundError("知识库文档已删除")
            result: dict[str, Any] = {**metadata, **indexed}
            if self.graph_indexer is not None:
                try:
                    graph = await self.graph_indexer.index_document(
                        updated,
                        await self.repository.get_chunks(attachment_id, limit=100_000),
                    )
                    result["graph"] = {"status": "succeeded", **graph}
                except Exception as exc:
                    # Graph indexing is optional; vector and keyword RAG remain
                    # usable when extraction or Neo4j writes fail.
                    logger.warning(
                        "knowledge_graph_index_failed",
                        attachment_id=attachment_id,
                        error=str(exc),
                    )
                    result["graph"] = {"status": "failed", "error": str(exc)}
            return result

    async def delete_knowledge_document(
        self, attachment_id: str, knowledge_base_id: str
    ) -> bool:
        async with self._lock(attachment_id):
            attachment = await self.repository.get_attachment(attachment_id)
            if attachment is None or attachment.knowledge_base_id != knowledge_base_id:
                return False
            await self.initialize()
            try:
                await self.delete_attachment_vectors(attachment_id)
                if self.graph_indexer is not None:
                    await self.graph_indexer.delete_document(attachment_id)
                deleted = await self.repository.soft_delete_knowledge_base_attachment(
                    attachment_id, knowledge_base_id
                )
            except Exception:
                try:
                    current = await self.repository.get_attachment(
                        attachment_id, include_deleted=True
                    )
                    if current is not None and current.deleted_time is None:
                        await self.index_attachment(attachment_id)
                except Exception:
                    logger.exception(
                        "knowledge_delete_vector_compensation_failed",
                        attachment_id=attachment_id,
                    )
                raise
            if not deleted:
                current = await self.repository.get_attachment(
                    attachment_id, include_deleted=True
                )
                if current is not None and current.deleted_time is None:
                    await self.index_attachment(attachment_id)
            return deleted

    async def delete_attachment_vectors(self, attachment_id: str) -> None:
        await self.vector_store.delete_attachment(attachment_id)

    def _lock(self, attachment_id: str) -> asyncio.Lock:
        return self._locks.setdefault(attachment_id, asyncio.Lock())


__all__ = ["FileIngestionService"]
