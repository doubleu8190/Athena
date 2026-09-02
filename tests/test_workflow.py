"""Focused tests for LangGraph runtime request helpers."""

from datetime import datetime
import asyncio

import pytest

from agent_runtime.graph_runtime import DEFAULT_SYSTEM_PROMPT, LangGraphRuntime
from agent_runtime.langgraph_graph import invoke_graph
from agent_runtime.nodes.prepare_request import prepare_and_persist_request
from athena.models import Message, MessageRole
from athena.models.file import Attachment, AttachmentRef, AttachmentStatus


class _RecordingGraph:
    def __init__(self):
        self.state = None
        self.config = None

    async def ainvoke(self, state, *, config):
        self.state = state
        self.config = config
        return {"result": "ok"}


@pytest.mark.asyncio
async def test_invoke_graph_uses_run_id_as_checkpoint_thread_id():
    graph = _RecordingGraph()
    stop_signal = asyncio.Event()

    assert (
        await invoke_graph(
            graph,
            session_id="session-1",
            run_id="run-1",
            user_message="hello",
            stop_signal=stop_signal,
        )
        == "ok"
    )
    assert graph.config["configurable"]["thread_id"] == "run-1"
    assert graph.config["configurable"]["stop_signal"] is stop_signal
    assert "system_prompt" not in graph.state
    assert "command_id" not in graph.state
    assert "command_id" not in graph.config["metadata"]


@pytest.mark.asyncio
async def test_invoke_graph_requires_run_id_for_checkpointing():
    with pytest.raises(ValueError, match="run_id is required"):
        await invoke_graph(
            _RecordingGraph(),
            session_id="session-1",
            user_message="hello",
        )


def test_build_system_prompt_uses_builtin_prompt_and_appends_memory():
    prompt = LangGraphRuntime.build_system_prompt("memory context")

    assert prompt == f"{DEFAULT_SYSTEM_PROMPT}\n\nmemory context"


def test_build_system_prompt_falls_back_to_default():
    assert LangGraphRuntime.build_system_prompt("") == DEFAULT_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_prepare_and_persist_request_merges_prepared_and_persisted_state():
    calls = []

    class Runtime:
        async def load_banded_attachments(self, session_id, attachment_ids):
            calls.append(("load_attachments", session_id, attachment_ids))
            return [
                Attachment(
                    id="file-1",
                    session_id=session_id,
                    filename="notes.txt",
                    mime_type="text/plain",
                    size_bytes=10,
                    sha256="a" * 64,
                    storage_key="blob",
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            ]

        async def load_history(self, session_id):
            calls.append(("load_history", session_id))
            return []

        async def persist_message_and_attachments(self, state):
            calls.append(("persist", state["message_id"], state["attachment_ids"]))
            return {"message_id": "message-1", "user_message_id": "message-1"}

    result = await prepare_and_persist_request(
        {
            "session_id": "session-1",
            "run_id": "run-1",
            "message_id": "message-1",
            "user_message": "read this",
            "attachment_ids": ["file-1", "file-1"],
        },
        runtime=Runtime(),
    )

    assert calls == [
        ("load_attachments", "session-1", ["file-1"]),
        ("load_history", "session-1"),
        ("persist", "message-1", ["file-1"]),
    ]
    assert result["history"] == []
    assert result["message_id"] == "message-1"
    assert result["requested_attachment_refs"][0]["id"] == "file-1"


def test_build_harness_messages_adds_attachment_context_without_mutating_message():
    attachment = AttachmentRef(
        id="file-1",
        filename="notes.txt",
        mime_type="text/plain",
        size_bytes=10,
        status=AttachmentStatus.READY,
    )
    persisted_message = Message(
        id="message-1",
        session_id="session-1",
        role=MessageRole.USER,
        content="read this",
        attachments=[attachment],
        timestamp=datetime.now(),
    )

    messages = LangGraphRuntime._build_harness_messages([], persisted_message)

    assert persisted_message.content == "read this"
    assert messages[0] is not persisted_message
    assert "file_id=file-1" in messages[0].content
    assert "name=notes.txt" in messages[0].content
    assert "status=ready" in messages[0].content
    assert "mime=text/plain" in messages[0].content
    assert "size=10" in messages[0].content


def test_build_harness_messages_does_not_change_non_user_messages():
    system_message = Message(
        id="message-1",
        session_id="session-1",
        role=MessageRole.SYSTEM,
        content="summary",
        attachments=[
            AttachmentRef(
                id="file-1",
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=10,
                status=AttachmentStatus.READY,
            )
        ],
        timestamp=datetime.now(),
    )

    messages = LangGraphRuntime._build_harness_messages(
        [system_message], system_message
    )

    assert messages == [system_message]
    assert messages[0] is system_message


def test_deserialize_messages_restores_flat_checkpoint_field():
    message = Message(
        id="message-1",
        session_id="session-1",
        role=MessageRole.USER,
        content="hello",
        timestamp=datetime.now(),
    )

    restored = LangGraphRuntime.deserialize_messages(
        [message.model_dump(mode="json")]
    )

    assert restored == [message]
