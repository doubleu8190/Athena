"""Focused tests for deterministic graph document projection."""

from __future__ import annotations

from datetime import datetime

import pytest

from athena.config.settings import Settings
from athena.core.graph.contracts import GraphExtractionResult
from athena.core.graph.indexing import GraphDocumentIndexer, normalize_entity_name
from athena.models.file import Attachment, FileChunk


class FakeLLM:
    async def ainvoke_structured(self, schema, _messages):
        assert schema is GraphExtractionResult
        return GraphExtractionResult.model_validate(
            {
                "entities": [
                    {"name": "系统 A", "entity_type": "System"},
                    {"name": "系统 B", "entity_type": "System"},
                ],
                "relations": [
                    {
                        "source": "系统 A",
                        "target": "系统 B",
                        "relation_type": "depends on",
                        "evidence": "系统 A 依赖系统 B",
                    }
                ],
            }
        )


class FakeStore:
    def __init__(self) -> None:
        self.payload = None

    async def replace_document_graph(self, **kwargs):
        self.payload = kwargs


def _attachment() -> Attachment:
    now = datetime.now()
    return Attachment(
        id="attachment-1",
        logical_document_id="document-1",
        document_version=2,
        knowledge_base_id="kb-1",
        filename="architecture.md",
        mime_type="text/markdown",
        size_bytes=10,
        sha256="hash",
        storage_key="files/hash",
        status="ready",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_indexer_normalizes_entities_and_writes_evidence():
    store = FakeStore()
    indexer = GraphDocumentIndexer(
        store,
        FakeLLM(),
        Settings(_env_file=None, graph_extraction_concurrency=1),
    )
    chunks = [FileChunk(id="chunk-1", attachment_id="attachment-1", ordinal=0, content="系统 A 依赖系统 B")]

    result = await indexer.index_document(_attachment(), chunks)

    assert result == {"chunks": 1, "entities": 2, "mentions": 2, "relations": 1}
    assert store.payload is not None
    assert store.payload["relations"][0].relation_type == "depends_on"
    assert store.payload["relations"][0].evidence_chunk_id == "chunk-1"
    assert normalize_entity_name("  系统  A ") == "系统 a"

