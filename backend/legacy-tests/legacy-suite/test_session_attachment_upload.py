from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from athena.core.files.adapter_registry import AdapterRegistry
from athena.models.file import Attachment
from athena.container import RuntimeContainer


def test_session_attachment_upload_creates_unbound_file_and_queues_processing():
    from athena.gateway.routes.files import router

    now = datetime.now()
    attachment = Attachment(
        id="attachment-1",
        session_id="session-1",
        filename="notes.txt",
        mime_type="text/plain",
        size_bytes=3,
        sha256="a" * 64,
        storage_key="blobs/a1",
        created_at=now,
        updated_at=now,
    )
    repository = MagicMock()
    repository.create_attachment = AsyncMock(return_value=attachment)
    storage = MagicMock()
    storage.save_stream = AsyncMock(
        return_value=SimpleNamespace(
            size_bytes=3, sha256="a" * 64, storage_key="blobs/a1"
        )
    )
    file_runtime = SimpleNamespace(
        adapter_registry=AdapterRegistry(),
        storage=storage,
        repository=repository,
        cleanup_unreferenced_blobs=AsyncMock(return_value=0),
    )
    db = MagicMock()
    db.sessions.get = AsyncMock(return_value=object())
    db.knowledge_document_jobs.enqueue_job = AsyncMock(return_value=True)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = RuntimeContainer(
        db=db,
        retrieval_trace_reader=MagicMock(),
        event_publisher=MagicMock(),
        approval_manager=MagicMock(),
        tool_manager=MagicMock(),
        tool_catalog=MagicMock(),
        mcp_manager=MagicMock(),
        llm=MagicMock(),
        file_runtime=file_runtime,
        memory_service=MagicMock(),
        command_store=MagicMock(),
        run_store=MagicMock(),
        approval_store=MagicMock(),
        event_store=MagicMock(),
        realtime_transport=MagicMock(),
        sandbox_runner=MagicMock(),
        workspace_manager=MagicMock(),
        neo4j_graph_store=MagicMock(),
    )

    response = TestClient(app).post(
        "/sessions/session-1/attachments",
        files=[("files", ("notes.txt", b"abc", "text/plain"))],
    )

    assert response.status_code == 202
    assert response.json()[0]["id"] == "attachment-1"
    assert response.json()[0]["message_id"] is None
    repository.create_attachment.assert_awaited_once()
    assert repository.create_attachment.await_args.kwargs["session_id"] == "session-1"
    assert repository.create_attachment.await_args.kwargs.get("message_id") is None
    db.knowledge_document_jobs.enqueue_job.assert_awaited_once_with("attachment-1")
