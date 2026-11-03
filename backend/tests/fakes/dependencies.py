"""Build an isolated dependency graph for unit and smoke tests."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from application.approval import ApprovalService
from application.configuration import ProviderQueryService, SettingsQueryService
from application.memory.query_service import MemoryQueryService
from application.retrieval import RetrievalQueryService
from application.events import EventStreamService
from application.files import AttachmentService
from application.knowledge import KnowledgeBaseQueryService, KnowledgeBaseService
from application.memory import MemoryService
from application.runs import RunQueryService, RunService
from application.sessions import SessionMessageQueryService, SessionService
from application.sessions.history_port import HistoryRunRecord, SessionHistorySnapshot
from application.tools import MCPService, ToolQueryService, ToolService
from bootstrap import ApplicationDependencies, build_dependencies
from configuration import get_settings

from .in_memory import (
    InMemoryApprovalRepository,
    InMemoryAttachmentRepository,
    InMemoryDocumentJobs,
    InMemoryEventStore,
    InMemoryFileAdapters,
    InMemoryFileStorage,
    InMemoryKnowledgeDocuments,
    InMemoryKnowledgeRepository,
    InMemoryMCPPort,
    InMemoryMemoryPort,
    InMemoryMessageRepository,
    InMemoryProviderQueries,
    InMemoryRunStore,
    InMemorySessionRepository,
    InMemoryToolExecution,
    InMemoryToolRepository,
)


def build_test_dependencies() -> ApplicationDependencies:
    """Build a stateful in-process graph without external services."""
    sessions = InMemorySessionRepository()
    messages = InMemoryMessageRepository()
    memory = InMemoryMemoryPort()
    tools = InMemoryToolRepository()
    mcp = InMemoryMCPPort()
    approvals = InMemoryApprovalRepository()
    events = InMemoryEventStore()
    runs = InMemoryRunStore(sessions, messages, events)
    storage = InMemoryFileStorage()
    adapters = InMemoryFileAdapters()
    jobs = InMemoryDocumentJobs()
    attachments = InMemoryAttachmentRepository()
    documents = InMemoryKnowledgeDocuments()
    knowledge = InMemoryKnowledgeRepository()
    settings = get_settings()
    queries = InMemoryProviderQueries(
        {
            "providers": [
                {
                    "name": item.name,
                    "provider": item.provider,
                    "model": item.model,
                    "base_url": item.base_url,
                    "api_key_configured": bool(item.api_key),
                    "api_key_masked": "***" if item.api_key else "",
                    "temperature": item.temperature,
                    "max_tokens": item.max_tokens,
                }
                for item in settings.llm_providers
            ]
        },
        memory,
    )
    now_id = lambda: uuid4().hex

    class SessionHistoryReader:
        async def read_snapshot(self, session_id, *, before_sequence, limit):
            terminal_runs = [
                run for run in await runs.list_for_session(session_id)
                if run.status.value in {"completed", "failed", "cancelled"}
                and (before_sequence is None or run.sequence < before_sequence)
            ]
            terminal_runs.sort(key=lambda run: run.sequence, reverse=True)
            selected_runs = terminal_runs[:limit]
            events_for_runs = []
            for run in selected_runs:
                batch, has_more_events = await events.list_events_for_run(session_id, run.run_id, limit=10000)
                events_for_runs.extend(batch)
                while has_more_events and batch:
                    batch, has_more_events = await events.list_events_for_run(
                        session_id, run.run_id, after=batch[-1].session_seq or 0, limit=10000
                    )
                    events_for_runs.extend(batch)
            return SessionHistorySnapshot(
                runs=tuple(HistoryRunRecord(run.run_id, run.message_id, run.sequence) for run in selected_runs),
                messages=tuple(await messages.list_by_session(session_id)),
                events=tuple(sorted(events_for_runs, key=lambda event: event.session_seq or 0)),
                has_more=len(terminal_runs) > limit,
                snapshot_event_seq=max(
                    (event.session_seq or 0 for event in events_for_runs),
                    default=0,
                ),
            )

    async def settings_payload() -> dict[str, Any]:
        current = get_settings()
        return {"host": current.host, "port": current.port, "debug": current.debug}

    class StorageJob:
        async def enqueue(self, attachment_id: str) -> bool:
            return await jobs.enqueue(attachment_id)

        async def cancel(self, attachment_id: str) -> None:
            await jobs.cancel(attachment_id)

    class KnowledgeDocs:
        async def create(self, value):
            return await documents.create(value)

        async def get(self, key, *, knowledge_base_id):
            return await documents.get(key, knowledge_base_id=knowledge_base_id)

        async def list_documents(self, key):
            return await documents.list_documents(key)

        async def list_versions(self, key, *, knowledge_base_id):
            return await documents.list_versions(key, knowledge_base_id=knowledge_base_id)

        async def latest_for_filename(self, filename, *, knowledge_base_id):
            return await documents.latest_for_filename(filename, knowledge_base_id=knowledge_base_id)

        async def soft_delete(self, key, *, knowledge_base_id):
            return await documents.soft_delete(key, knowledge_base_id=knowledge_base_id)

    services = {
        "session_service": SessionService(sessions, id_factory=now_id),
        "message_query_service": SessionMessageQueryService(sessions, SessionHistoryReader()),
        "run_query_service": RunQueryService(sessions, runs, events),
        "run_service": RunService(runs, runs, id_factory=now_id),
        "event_stream_service": EventStreamService(sessions, events),
        "tool_query_service": ToolQueryService(tools, queries),
        "tool_service": ToolService(tools, InMemoryToolExecution()),
        "approval_service": ApprovalService(approvals),
        "mcp_service": MCPService(mcp),
        "provider_query_service": ProviderQueryService(queries),
        "settings_query_service": SettingsQueryService(type("SettingsPort", (), {"get_settings": staticmethod(settings_payload)})()),
        "memory_query_service": MemoryQueryService(queries),
        "memory_service": MemoryService(memory),
        "retrieval_query_service": RetrievalQueryService(queries),
        "knowledge_query_service": KnowledgeBaseQueryService(knowledge),
        "attachment_service": AttachmentService(sessions, attachments, storage, adapters, StorageJob(), id_factory=now_id),
        "knowledge_service": KnowledgeBaseService(knowledge, KnowledgeDocs(), storage, adapters, StorageJob(), id_factory=now_id),
    }
    return build_dependencies(services=services)


__all__ = ["build_test_dependencies"]
