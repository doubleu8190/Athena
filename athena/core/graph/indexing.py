"""LLM-backed document to graph projection."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import Sequence

from langchain_core.messages import HumanMessage, SystemMessage

from athena.config.settings import Settings
from athena.core.llm.provider import LLMProvider
from athena.models.file import Attachment, FileChunk
from athena.utils.logging import get_logger
from athena.utils.prompt_loader import get_prompt

from .contracts import (
    ExtractedEntity,
    GraphChunk,
    GraphEntity,
    GraphEntityMention,
    GraphExtractionResult,
    GraphRelation,
)
from .ports import GraphStore

logger = get_logger(__name__)

def normalize_entity_name(value: str) -> str:
    """Normalize names for deterministic identity within one knowledge base."""

    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


class GraphDocumentIndexer:
    """Extract and atomically project one parsed document into a GraphStore."""

    def __init__(
        self,
        graph_store: GraphStore,
        llm: LLMProvider,
        settings: Settings,
    ) -> None:
        self._graph_store = graph_store
        self._llm = llm
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.graph_extraction_concurrency)

    async def index_document(
        self, attachment: Attachment, chunks: Sequence[FileChunk]
    ) -> dict[str, int]:
        if not attachment.knowledge_base_id:
            raise ValueError("graph indexing requires a knowledge base document")

        extracted = await asyncio.gather(
            *(self._extract_chunk(chunk) for chunk in chunks)
        )
        entities: dict[str, GraphEntity] = {}
        mentions: list[GraphEntityMention] = []
        relations: list[GraphRelation] = []
        for chunk, result in zip(chunks, extracted):
            aliases: dict[str, str] = {}
            for item in result.entities:
                if item.confidence < self._settings.graph_min_confidence:
                    logger.info(
                        "skipping low-confidence entity %s (%.2f)",
                        item.name,
                        item.confidence,
                    )
                    continue
                normalized = normalize_entity_name(item.name)
                if not normalized:
                    continue
                entity_key = f"{attachment.knowledge_base_id}:{item.entity_type.casefold()}:{normalized}"
                existing = entities.get(entity_key)
                if existing is None:
                    entities[entity_key] = GraphEntity(
                        entity_key=entity_key,
                        canonical_name=item.name.strip(),
                        entity_type=item.entity_type.strip() or "Concept",
                        aliases=list(dict.fromkeys(item.aliases)),
                        description=item.description,
                        confidence=item.confidence,
                    )
                elif item.description and not existing.description:
                    entities[entity_key] = existing.model_copy(
                        update={"description": item.description}
                    )
                aliases[normalized] = entity_key
                for alias in item.aliases:
                    aliases[normalize_entity_name(alias)] = entity_key
                mentions.append(
                    GraphEntityMention(
                        entity_key=entity_key,
                        chunk_id=chunk.id,
                        surface=item.name.strip(),
                        confidence=item.confidence,
                    )
                )
            for item in result.relations:
                source = aliases.get(normalize_entity_name(item.source))
                target = aliases.get(normalize_entity_name(item.target))
                if (
                    source is None
                    or target is None
                    or source == target
                    or item.confidence < self._settings.graph_min_confidence
                ):
                    logger.info(
                        "skipping low-confidence or invalid relation %s -> %s (%.2f)",
                        item.source,
                        item.target,
                        item.confidence,
                    )
                    continue
                relations.append(
                    GraphRelation(
                        source_entity_key=source,
                        target_entity_key=target,
                        relation_type=self._normalize_relation_type(item.relation_type),
                        evidence=item.evidence,
                        evidence_chunk_id=chunk.id,
                        confidence=item.confidence,
                    )
                )

        await self._graph_store.replace_document_graph(
            knowledge_base_id=attachment.knowledge_base_id,
            attachment_id=attachment.id,
            logical_document_id=attachment.logical_document_id or attachment.id,
            document_version=attachment.document_version,
            filename=attachment.filename,
            chunks=[
                GraphChunk(
                    id=chunk.id,
                    ordinal=chunk.ordinal,
                    locator=chunk.locator.model_dump(mode="json", exclude_none=True),
                    document_version=attachment.document_version,
                )
                for chunk in chunks
            ],
            entities=list(entities.values()),
            mentions=mentions,
            relations=relations,
        )
        return {
            "chunks": len(chunks),
            "entities": len(entities),
            "mentions": len(mentions),
            "relations": len(relations),
        }

    async def delete_document(self, attachment_id: str) -> None:
        await self._graph_store.delete_document_graph(attachment_id=attachment_id)

    async def _extract_chunk(self, chunk: FileChunk) -> GraphExtractionResult:
        async with self._semaphore:
            async with asyncio.timeout(self._settings.graph_timeout_seconds):
                return await self._llm.ainvoke_structured(
                    GraphExtractionResult,
                    [
                        SystemMessage(content=get_prompt("graph_extraction")),
                        HumanMessage(content=f"[文档分块]\n{chunk.content}"),
                    ],
                )

    @staticmethod
    def _normalize_relation_type(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().casefold())
        return value.strip("_")[:100] or "related_to"


__all__ = ["GraphDocumentIndexer", "normalize_entity_name"]
