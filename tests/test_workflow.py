"""Focused tests for the agent workflow orchestration helpers."""

from datetime import datetime

from athena.core.agent.workflow import AgentWorkflow, DEFAULT_SYSTEM_PROMPT
from athena.models import Message, MessageRole
from athena.models.file import AttachmentRef, AttachmentStatus


def test_normalize_request_uses_continuation_values_and_deduplicates_files():
    user_message, attachment_ids = AgentWorkflow._normalize_request(
        "new message",
        ["new-file"],
        {
            "user_message": "original message",
            "attachment_ids": ["file-2", "file-1", "file-2"],
        },
    )

    assert user_message == "original message"
    assert attachment_ids == ["file-2", "file-1"]


def test_build_system_prompt_uses_custom_prompt_and_appends_memory():
    prompt = AgentWorkflow._build_system_prompt("custom prompt", "memory context")

    assert prompt == "custom prompt\n\nmemory context"


def test_build_system_prompt_falls_back_to_default():
    assert AgentWorkflow._build_system_prompt(None, "") == DEFAULT_SYSTEM_PROMPT


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

    messages = AgentWorkflow._build_harness_messages(
        [], persisted_message, continuation=None
    )

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

    messages = AgentWorkflow._build_harness_messages(
        [system_message],
        system_message,
        continuation={"message_id": system_message.id},
    )

    assert messages == [system_message]
    assert messages[0] is system_message
