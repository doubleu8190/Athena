from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from athena.core.files.ingestion import FileIngestionService


@pytest.mark.asyncio
async def test_session_attachment_uses_document_pipeline_without_shared_graph_index():
    service = object.__new__(FileIngestionService)
    attachment = SimpleNamespace(
        id="attachment-1",
        session_id="session-1",
        knowledge_base_id=None,
        document_version=1,
    )
    repository = MagicMock()
    repository.get_attachment = AsyncMock(side_effect=[attachment, attachment])
    repository.update_attachment = AsyncMock(return_value=attachment)
    repository.get_chunks = AsyncMock(return_value=[])
    graph_indexer = MagicMock()
    graph_indexer.index_document = AsyncMock()
    service._locks = {}
    service.initialize = AsyncMock()
    service.repository = repository
    service.parse_attachment = AsyncMock(return_value={"chunk_count": 1})
    service.index_attachment = AsyncMock(return_value={"chunks": 1})
    service.delete_attachment_vectors = AsyncMock()
    service.graph_indexer = graph_indexer

    result = await service.process_knowledge_document("attachment-1")

    assert result == {"chunk_count": 1, "chunks": 1}
    repository.update_attachment.assert_awaited_once_with(
        "attachment-1", status="ready", error_message=None
    )
    graph_indexer.index_document.assert_not_awaited()
