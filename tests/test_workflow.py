"""Focused tests for LangGraph runtime request helpers."""

from datetime import datetime
import asyncio

import pytest

from athena.runtime.graph_runtime import (
    DEFAULT_SYSTEM_PROMPT,
    LangGraphRuntime,
)
from athena.runtime.services.memory_service import (
    _recent_history_by_turns,
)
from athena.runtime.langgraph_graph import invoke_graph
from athena.runtime.nodes.prepare_request import prepare_and_persist_request
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


@pytest.mark.asyncio
async def test_simple_memory_request_does_not_call_llm():
    """Memory requests now go through MemoryService, not directly on LangGraphRuntime."""
    from athena.runtime.services.memory_service import MemoryService

    class LLM:
        async def ainvoke(self, messages):
            raise AssertionError("simple requests must stay deterministic")

    class RetrievalStub:
        async def get_context(self, request):
            return ""

    service = MemoryService(llm=LLM(), memory_retrieval=RetrievalStub())
    request = await service.build_memory_request(
        "session-1", "请检查 Athena Candidate Resolver 的关系决策是否完整"
    )

    assert request is not None
    assert request.query == "请检查 Athena Candidate Resolver 的关系决策是否完整"
    assert request.reason == "substantive_task"


@pytest.mark.asyncio
async def test_complex_memory_request_uses_validated_llm_plan():
    """Memory requests now go through MemoryService."""
    from athena.runtime.services.memory_service import MemoryService

    class LLM:
        async def ainvoke(self, messages):
            return type(
                "Response",
                (),
                {
                    "content": (
                        '{"query":"Athena 记忆系统候选解析和检索共享设计",'
                        '"task":"继续实现记忆系统", "limit":12}'
                    )
                },
            )()

    class RetrievalStub:
        async def get_context(self, request):
            return ""

    service = MemoryService(llm=LLM(), memory_retrieval=RetrievalStub())
    message = "继续完善 Athena"
    request = await service.build_memory_request(
        "session-1",
        message,
        [{"role": "user", "content": "我们正在设计 Athena 的记忆系统"}],
    )

    assert request is not None
    assert request.session_id == "session-1"
    assert request.query == "Athena 记忆系统候选解析和检索共享设计"
    assert request.limit == 12
    assert request.reason == "llm_complex_request"


def test_recent_history_keeps_six_complete_turns():
    history = [{"role": "system", "content": "summary"}]
    for index in range(1, 8):
        history.extend(
            [
                {"role": "user", "content": f"user-{index}"},
                {"role": "assistant", "content": f"assistant-{index}"},
                {"role": "tool", "content": f"tool-{index}"},
            ]
        )

    recent = _recent_history_by_turns(history)

    assert recent[0] == {"role": "system", "content": "summary"}
    assert [item["content"] for item in recent if item["role"] == "user"] == [
        "user-2",
        "user-3",
        "user-4",
        "user-5",
        "user-6",
        "user-7",
    ]
    assert [item["content"] for item in recent] == [
        "summary",
        "user-2",
        "assistant-2",
        "tool-2",
        "user-3",
        "assistant-3",
        "tool-3",
        "user-4",
        "assistant-4",
        "tool-4",
        "user-5",
        "assistant-5",
        "tool-5",
        "user-6",
        "assistant-6",
        "tool-6",
        "user-7",
        "assistant-7",
        "tool-7",
    ]


@pytest.mark.asyncio
async def test_complex_memory_request_falls_back_on_invalid_llm_output():
    """Memory requests now go through MemoryService."""
    from athena.runtime.services.memory_service import MemoryService

    class LLM:
        async def ainvoke(self, messages):
            return type("Response", (), {"content": '{"query":"ok","extra":true}'})()

    class RetrievalStub:
        async def get_context(self, request):
            return ""

    service = MemoryService(llm=LLM(), memory_retrieval=RetrievalStub())
    message = "继续分析之前的系统设计和实现细节，并逐项对照尚未完成的工作、风险、测试覆盖和兼容性问题。" * 3
    request = await service.build_memory_request("session-1", message)

    assert request is not None
    assert request.query == message
    assert request.reason == "context_reference"


@pytest.mark.asyncio
async def test_prepare_and_persist_request_merges_prepared_and_persisted_state():
    """Tests now use RequestService directly instead of LangGraphRuntime."""
    calls = []

    class RequestService:
        def __init__(self):
            pass

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
        request_service=RequestService(),
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
    """_build_harness_messages moved to RequestService."""
    from athena.runtime.services.request_service import RequestService

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

    messages = RequestService._build_harness_messages([], persisted_message)

    assert persisted_message.content == "read this"
    assert messages[0] is not persisted_message
    assert "file_id=file-1" in messages[0].content
    assert "name=notes.txt" in messages[0].content
    assert "status=ready" in messages[0].content
    assert "mime=text/plain" in messages[0].content
    assert "size=10" in messages[0].content


def test_build_harness_messages_does_not_change_non_user_messages():
    """_build_harness_messages moved to RequestService."""
    from athena.runtime.services.request_service import RequestService

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

    messages = RequestService._build_harness_messages(
        [system_message], system_message
    )

    assert messages == [system_message]
    assert messages[0] is system_message


def test_deserialize_messages_restores_flat_checkpoint_field():
    """deserialize_messages moved to ExecutionService."""
    from athena.runtime.services.execution_service import ExecutionService

    message = Message(
        id="message-1",
        session_id="session-1",
        role=MessageRole.USER,
        content="hello",
        timestamp=datetime.now(),
    )

    restored = ExecutionService.deserialize_messages(
        [message.model_dump(mode="json")]
    )

    assert restored == [message]
